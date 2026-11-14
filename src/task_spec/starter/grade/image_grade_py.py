#!/usr/bin/env python3
"""채점용 이미지의 `grade` 명령 — 데이터(Python) 템플릿판 (docs/design/starter-templates.md §6.1 의 플랫폼 계약).

    podman run --rm --network none -v <제출물>:/submission:ro -v <결과>:/out <grader 이미지> grade

/submission(응시 작업 경로 사본)을 복사해(지원자 가상 환경·캐시는 빼고 이미지의 가상 환경을 쓴다) 잠긴 파일은 템플릿 원본으로 되돌리고,
채점 자리(reserved)에 지원자가 둔 파일은 지운 뒤 숨은·공개 테스트와 하네스를 얹어 컴파일 확인 → pytest(기존) → pytest(채점)를 돌린다.
결과는 /out/result.json + JUnit 보고서·로그.

판정 코드는 이 파일 하나다 — Python 검증기(task_spec/starter/verify/python.py)가 여기의 판정 함수를 import 해 쓴다.
그래서 **표준 라이브러리만** 쓴다 (이미지 안에서 돈다). 2026-11-09 데이터 템플릿.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CONFIG = Path(os.environ.get("GRADE_CONFIG", "/grader/config.json"))
OVERLAY = Path(os.environ.get("GRADE_OVERLAY", "/grader/overlay"))
SUBMISSION = Path(os.environ.get("GRADE_SUBMISSION", "/submission"))
OUT = Path(os.environ.get("GRADE_OUT", "/out"))
WORK = Path(os.environ.get("GRADE_WORK", "/tmp/grade-work"))

_REASON = re.compile(r"\[reason:([a-z0-9_]+)\]")
# 건너뛴 테스트 — 판정하지 않은 것이라 통과로 세지 않는다 (#173: 조건부 건너뛰기가 검증을 지나 채점에서 통과로 기록될 수 있었다)
SKIPPED = "테스트를 건너뜀 — 판정하지 않았으므로 통과가 아니다"
_PT = re.compile(r"\bpt\d{1,2}\b")
# 제출물에서 가져오지 않는 것 — 가상 환경·캐시는 이미지의 것을 쓴다
SKIP = (".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".git")
# compileall: `*** Error compiling './pipeline/run.py'...` 뒤 `  File "...", line N` · `SyntaxError: …`
_COMPILE_FILE = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+)')
# pytest 수집 중 문법 오류: `E     File "<절대 경로>", line N` 뒤에 `E   SyntaxError: …` (2026-11-10 qwen 알파: 공개 테스트 문법 오류를 못 읽어 재요청 0회로 끝남)
_E_FILE = re.compile(r'^E\s+File "(?P<path>[^"]+)", line (?P<line>\d+)')
_E_ERR = re.compile(r"^E\s+(?P<msg>\w*Error: .+)$")
_COMPILE_ERR = re.compile(r"^(?P<kind>\w*Error): (?P<msg>.+)$")


# ── 판정 (python_verify 와 같이 쓴다) ──────────────────────────────────


def clean_message(text: str) -> str:
    return " ".join((text or "").split())[:400]


def is_assertion(message: str) -> bool:
    """단언 실패인가 — pytest 의 `AssertionError: …`·`assert …`, 또는 사유 태그. 그 밖(예외·import 실패)은 아님."""
    head = (message or "").lstrip()
    return head.startswith(("AssertionError", "assert ")) or "[reason:" in head.split("\n", 1)[0]


def group_of(rel: str, layout: Mapping[str, str]) -> str:
    for group, key in (("hidden", "cases"), ("selftest", "selftest"), ("public", "public")):
        folder = layout.get(key, "").rstrip("/")
        if folder and rel.startswith(folder + "/"):
            return group
    return "existing"


def _name(
    group: str, rel: str, titles: Sequence[str], case_rx: re.Pattern[str], text: str | None
) -> str:
    """숨은 케이스는 케이스 id (파일 이름·테스트 이름 → 파일 본문), 공개 테스트는 pt id, 그 밖은 파일 경로."""
    # 파일·함수 이름의 밑줄은 낱말 경계를 막는다 (test_hc1_late → "test hc1 late")
    words = [re.sub(r"[_.]", " ", t) for t in (posixpath.basename(rel), *titles)]
    if group == "hidden":
        for title in words:
            if m := case_rx.search(title):
                return m.group(0)
        if text and (m := case_rx.search(text)):
            return m.group(0)
    if group == "public":
        for title in words:
            if m := _PT.search(title):
                return m.group(0)
    return rel


def junit_results(
    xml: Path,
    rootdir: str,
    layout: Mapping[str, str],
    case_pattern: str,
    work: Path | None = None,
    texts: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """pytest JUnit(xunit1) → 파일마다 결과 하나 (첫 실패를 대표로): {group, name, result, reason, message}.
    rootdir: pytest rootdir 의 저장소 루트 기준 경로 (채점 설정이면 grading/harness, 지원자 설정이면 "") — testcase file 이 그 기준이다."""
    case_rx = re.compile(case_pattern)
    files: dict[str, dict[str, Any]] = {}
    for tc in ET.parse(xml).getroot().iter("testcase"):
        raw = tc.get("file") or (tc.get("classname", "").replace(".", "/") + ".py")
        rel = (
            posixpath.normpath(posixpath.join(rootdir, raw)) if rootdir else posixpath.normpath(raw)
        )
        entry = files.setdefault(rel, {"titles": [], "first": None, "skipped": None})
        entry["titles"].append(tc.get("name", ""))
        bad = tc.find("failure")
        if bad is None:
            bad = tc.find("error")
        if bad is not None and entry["first"] is None:
            entry["first"] = (bad.tag, bad.get("message") or "", bad.text or "", tc.get("name", ""))
        # pytest.skip·skipif·xfail — 건너뜀은 통과가 아니다 (#173: 건너뜀을 빼려던 분기가 늘 거짓이라 통과로 셌다)
        if entry["skipped"] is None and (sk := tc.find("skipped")) is not None:
            entry["skipped"] = (sk.get("message") or "", tc.get("name", ""))
    out: list[dict[str, Any]] = []
    for rel in sorted(files):
        group = group_of(rel, layout)
        text = (texts or {}).get(rel)
        if text is None and group == "hidden" and work is not None and (work / rel).is_file():
            text = (work / rel).read_text("utf-8", errors="ignore")
        name = _name(group, rel, files[rel]["titles"], case_rx, text)
        first = files[rel]["first"]
        if first is None and files[rel]["skipped"] is not None:
            why, title = files[rel]["skipped"]
            out.append(
                {
                    "group": group,
                    "name": name,
                    "result": "HARNESS_ERR",
                    "reason": None,
                    "message": clean_message(f"{title} — {SKIPPED} {why}".strip()),
                }
            )
            continue
        if first is None:
            out.append(
                {"group": group, "name": name, "result": "PASS", "reason": None, "message": ""}
            )
            continue
        tag, message, body, title = first
        reason = _REASON.search(message) or _REASON.search(body)
        result = "ASSERT" if tag == "failure" and is_assertion(message) else "HARNESS_ERR"
        out.append(
            {
                "group": group,
                "name": name,
                "result": result,
                "reason": reason.group(1) if reason else None,
                "message": clean_message(f"{title} — {message}"),
            }
        )
    return out


_FRAME = re.compile(r"^(?P<path>[^\s:]+\.py):(?P<line>\d+): in ")
_E_LINE = re.compile(r"^E\s+(?P<msg>\w*(?:Error|Exception)\b.*)$")


def _relative(path: str, root: str | None) -> str:
    if root and path.startswith(root.rstrip("/") + "/"):
        return path[len(root.rstrip("/")) + 1 :]
    return path.lstrip("./")


def compile_errors(log_text: str, limit: int = 20, root: str | None = None) -> list[dict[str, Any]]:
    """compileall·pytest --collect-only 출력 → [{path, line, message, detail}].
    수집 오류는 traceback 의 `<경로>:<줄>: in …` 프레임(저장소 루트 기준) 중 마지막 것과 그 뒤 `E   …Error: …` 줄로.
    테스트 파일의 문법 오류는 pytest 가 `E     File "<절대 경로>", line N` 로 보이므로 root(조립 폴더) 기준 상대 경로로 바꾼다."""
    out: list[dict[str, Any]] = []
    lines = log_text.splitlines()
    frame: tuple[str, int] | None = None
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        ef = _E_FILE.match(line)
        if ef:
            msg = next(
                (m.group("msg") for m in map(_E_ERR.match, lines[i + 1 : i + 6]) if m),
                "SyntaxError",
            )
            err = {
                "path": _relative(ef.group("path"), root),
                "line": int(ef.group("line")),
                "message": msg[:300],
                "detail": "",
            }
            if err not in out:
                out.append(err)
            frame = None
            continue
        fm = _FRAME.match(line.strip())
        if fm and "site-packages" not in fm.group("path") and not fm.group("path").startswith("/"):
            frame = (fm.group("path").lstrip("./"), int(fm.group("line")))
            continue
        em = _E_LINE.match(line)
        if em and frame is not None:
            msg = re.sub(r"\s\(/[^)]*\)$", "", em.group("msg"))  # 끝의 절대 경로 괄호는 뺀다
            err = {"path": frame[0], "line": frame[1], "message": msg[:300], "detail": ""}
            if err not in out:
                out.append(err)
            frame = None
            continue
        f = _COMPILE_FILE.match(line)
        if f and "Error compiling" in " ".join(lines[max(0, i - 2) : i]):
            msg = next(
                (c.group(0) for c in map(_COMPILE_ERR.match, lines[i + 1 : i + 5]) if c),
                "SyntaxError",
            )
            err = {
                "path": f.group("path").lstrip("./"),
                "line": int(f.group("line")),
                "message": msg[:300],
                "detail": "",
            }
            if err not in out:
                out.append(err)
    return out[:limit]


# ── grade 명령 ───────────────────────────────────────────────────────


def prepare(cfg: Mapping[str, Any]) -> None:
    """제출물 → 작업 폴더: 가상 환경·캐시 빼고 복사, 채점 자리 비우기, 잠긴 파일 원복, 채점 오버레이 얹기."""
    if WORK.exists():
        shutil.rmtree(WORK)
    shutil.copytree(SUBMISSION, WORK, ignore=shutil.ignore_patterns(*SKIP), symlinks=False)
    for pattern in cfg["reserved"]:
        target = WORK / pattern.removesuffix("/**")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    template = Path(cfg["template_dir"])
    for rel in cfg["locked"]:
        src = template / rel
        if src.is_file():
            (WORK / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, WORK / rel)
    shutil.copytree(OVERLAY, WORK, dirs_exist_ok=True)


def run(cmd: Sequence[str], log: Path, timeout: int) -> int | None:
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"$ {' '.join(cmd)}\n")
        fh.flush()
        env = os.environ | {"PYTHONDONTWRITEBYTECODE": "1", "TZ": "Asia/Seoul"}
        try:
            return subprocess.run(
                list(cmd),
                cwd=WORK,
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=env,
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            return None


def existing(paths: Sequence[str]) -> list[str]:
    return [p for p in paths if (WORK / p).exists()]


def run_all(
    cfg: Mapping[str, Any], log: Path
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], float]:
    """컴파일 확인 → pytest(기존) → pytest(채점). → (setup, tests, compile_errors, seconds)."""
    t0 = time.monotonic()
    timeout = int(cfg["timeout_s"])
    py = cfg.get("python") or sys.executable
    start = log.stat().st_size if log.exists() else 0
    codes = [run([py, "-m", "compileall", "-q", *existing(cfg["compile_paths"])], log, timeout)]
    targets = existing(cfg["grading_paths"])
    collect = [t for t in targets if not t.endswith("selftest")]
    if collect:
        codes.append(
            run(
                [py, "-m", "pytest", "-c", cfg["grading_ini"], "--collect-only", "-q", *collect],
                log,
                timeout,
            )
        )
    if any(c is None for c in codes):
        return "timeout", [], [], round(time.monotonic() - t0, 1)
    if any(c not in (0, 5) for c in codes):  # 5 = 수집된 테스트 없음
        with log.open("r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(start)
            return (
                "compile_error",
                [],
                compile_errors(fh.read(), root=str(WORK)),
                round(time.monotonic() - t0, 1),
            )
    tests: list[dict[str, Any]] = []
    runs = [("existing.xml", [py, "-m", "pytest", "-o", "junit_family=xunit1"], "")]
    if targets:
        runs.append(
            (
                "grading.xml",
                [py, "-m", "pytest", "-c", cfg["grading_ini"], *targets],
                cfg["grading_rootdir"],
            )
        )
    for name, cmd, rootdir in runs:
        report = OUT / name
        code = run([*cmd, f"--junitxml={report}"], log, timeout)
        if code is None:
            return "timeout", tests, [], round(time.monotonic() - t0, 1)
        if not report.is_file():
            return "boot_error", tests, [], round(time.monotonic() - t0, 1)
        tests += junit_results(report, rootdir, cfg["layout"], cfg["case_id_pattern"], WORK)
    return "ok", tests, [], round(time.monotonic() - t0, 1)


def result_json(
    cfg: Mapping[str, Any],
    setup: str,
    tests: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    seconds: float,
) -> dict[str, Any]:
    """케이스 목록(config)마다 통과 여부·사유 — Java·Node grade 와 같은 모양."""
    by_name = {(t["group"], t["name"]): t for t in tests}
    cases = []
    for c in cfg["cases"]:
        t = by_name.get((c["kind"], c["id"]))
        passed = t is not None and t["result"] == "PASS"
        reason = (
            None
            if passed
            else (t or {}).get("reason")
            or (setup if setup != "ok" else ("not_run" if t is None else t["result"].lower()))
        )
        cases.append(
            {
                "id": c["id"],
                "trap_id": c.get("trap_id"),
                "kind": c["kind"],
                "passed": passed,
                "reason": reason,
                **({"intended_reason": c["intended_reason"]} if c.get("intended_reason") else {}),
                **({"message": t["message"]} if t and not passed and t.get("message") else {}),
            }
        )
    others = [t for t in tests if t["group"] in ("existing", "selftest")]
    return {
        "common_id": cfg["common_id"],
        "revision": cfg["revision"],
        "setup": setup,
        "seconds": seconds,
        "compile_errors": errors,
        "cases": cases,
        "summary": {
            "hidden_passed": sum(1 for c in cases if c["kind"] == "hidden" and c["passed"]),
            "hidden_total": sum(1 for c in cases if c["kind"] == "hidden"),
            "public_passed": sum(1 for c in cases if c["kind"] == "public" and c["passed"]),
            "public_total": sum(1 for c in cases if c["kind"] == "public"),
            "existing_failed": [
                t["name"] for t in others if t["group"] == "existing" and t["result"] != "PASS"
            ],
            "selftest_failed": [
                t["name"] for t in others if t["group"] == "selftest" and t["result"] != "PASS"
            ],
        },
        "report_path": str(OUT),
    }


def main() -> int:
    cfg = json.loads(CONFIG.read_text("utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    if not SUBMISSION.is_dir():
        print(f"제출물이 없음: {SUBMISSION}", file=sys.stderr)
        return 2
    prepare(cfg)
    log = OUT / "grade.log"
    log.write_text("", "utf-8")
    setup, tests, errors, seconds = run_all(cfg, log)
    result = result_json(cfg, setup, tests, errors, seconds)
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
