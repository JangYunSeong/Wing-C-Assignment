#!/usr/bin/env python3
"""채점용 이미지의 `grade` 명령 — 프론트엔드(TypeScript) 템플릿판 (docs/design/starter-templates.md §6.1 의 플랫폼 계약).

    podman run --rm --network none -v <제출물>:/submission:ro -v <결과>:/out <grader 이미지> grade

/submission(응시 작업 경로 사본)을 복사해(지원자 node_modules·빌드 산출물은 빼고 이미지의 node_modules 를 링크) 잠긴 파일은 템플릿 원본으로
되돌리고, 채점 자리(reserved)에 지원자가 둔 파일은 지운 뒤 숨은·공개 테스트와 하네스를 얹어 tsc → vitest(기존) → vitest(채점)를 돌린다.
결과는 /out/result.json + vitest JSON 보고서·로그.

판정 코드는 이 파일 하나다 — 개발 서버 검증기(eval/starter/node_verify.py)가 여기의 판정 함수를 import 해 쓴다 (Java 는 gradle_verify 와
image_grade 두 벌). 그래서 **표준 라이브러리만** 쓴다 (이미지 안에서 돈다). 2026-11-09 React 템플릿 이미지.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CONFIG = Path(os.environ.get("GRADE_CONFIG", "/grader/config.json"))
OVERLAY = Path(os.environ.get("GRADE_OVERLAY", "/grader/overlay"))
SUBMISSION = Path(os.environ.get("GRADE_SUBMISSION", "/submission"))
OUT = Path(os.environ.get("GRADE_OUT", "/out"))
WORK = Path(os.environ.get("GRADE_WORK", "/tmp/grade-work"))

# tsc --pretty false: `src/App.tsx(3,7): error TS2322: Type 'number' is not assignable to type 'string'.` (+ 들여쓴 이어지는 줄)
_TSC = re.compile(r"^(?P<path>[^\s(][^(]*)\((?P<line>\d+),\d+\): error (?P<msg>TS\d+: .+)$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_REASON = re.compile(r"\[reason:([a-z0-9_]+)\]")
_PT = re.compile(r"\bpt\d{1,2}\b")
# 실패 메시지 뒤에 붙는 DOM 덤프·스택 — 앞부분만 남긴다
_TAIL = re.compile(r"\n\s*\n\s*Ignored nodes:|\n\s+at |\n\s*\n\s*<")
# 제출물에서 가져오지 않는 것 — 의존성·빌드 산출물은 이미지의 것을 쓴다
SKIP = ("node_modules", "dist", ".vite", "coverage", ".git")


# ── 판정 (node_verify 와 같이 쓴다) ─────────────────────────────────────


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text or "")


def tsc_errors(log_text: str, limit: int = 20) -> list[dict[str, Any]]:
    """tsc --pretty false 출력 → [{path, line, message, detail}] (이어지는 들여쓴 줄은 detail)."""
    out: list[dict[str, Any]] = []
    lines = strip_ansi(log_text).splitlines()
    for i, raw in enumerate(lines):
        m = _TSC.match(raw.strip())
        if m is None:
            continue
        more: list[str] = []
        for nxt in lines[i + 1 : i + 4]:
            if not nxt.startswith("  ") or _TSC.match(nxt.strip()):
                break
            more.append(nxt.strip())
        err = {
            "path": m.group("path").strip(),
            "line": int(m.group("line")),
            "message": m.group("msg")[:300],
            "detail": " ".join(more)[:300],
        }
        if err not in out:
            out.append(err)
    return out[:limit]


def clean_message(text: str) -> str:
    """실패 메시지 앞부분 — 색 코드·DOM 덤프·스택을 뗀다."""
    text = strip_ansi(text)
    m = _TAIL.search(text)
    if m is not None:
        text = text[: m.start()]
    return " ".join(text.split())[:400]


def is_assertion(message: str) -> bool:
    """단언 실패인가 — AssertionError(기본 매처)·jest-dom 매처(`Error: expect(…)`)·사유 태그. 그 밖(요소 못 찾음·요청 안 옴·예외)은 아님."""
    head = strip_ansi(message).lstrip()
    return (
        head.startswith("AssertionError")
        or head.startswith(
            "Error: expect("
        )  # jest-dom 매처 (사유 메시지는 버려진다 — manifest reason_not_on)
        or "[reason:" in head.split("\n", 1)[0]
    )


def _rel(name: str, work: Path) -> str:
    try:
        return Path(name).resolve().relative_to(work.resolve()).as_posix()
    except ValueError:
        return Path(name).as_posix()


def group_of(rel: str, layout: Mapping[str, str]) -> str:
    """파일 경로 → hidden·selftest·public·existing (layout: cases·selftest·public 폴더)."""
    for group, key in (("hidden", "cases"), ("selftest", "selftest"), ("public", "public")):
        folder = layout.get(key, "").rstrip("/")
        if folder and rel.startswith(folder + "/"):
            return group
    return "existing"


def _name(
    group: str, rel: str, titles: Sequence[str], case_rx: re.Pattern[str], text: str | None
) -> str:
    """숨은 케이스는 케이스 id (describe 제목 → 파일 본문), 공개 테스트는 pt id, 그 밖은 src 기준 파일 경로."""
    if group == "hidden":
        for title in titles:
            if m := case_rx.search(title):
                return m.group(0)
        if text and (m := case_rx.search(text)):
            return m.group(0)
    if group == "public":
        for title in titles:
            if m := _PT.search(title):
                return m.group(0)
    return rel.removeprefix("src/")


def vitest_results(
    report: Mapping[str, Any],
    work: Path,
    layout: Mapping[str, str],
    case_pattern: str,
    texts: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """vitest JSON 보고서 → 파일마다 결과 하나 (첫 실패를 대표로): {group, name, result, reason, message}.
    texts: 경로 → 파일 본문 (describe 제목에 케이스 id 가 없을 때) — 없으면 work 아래 파일을 읽는다."""
    case_rx = re.compile(case_pattern)
    out: list[dict[str, Any]] = []
    for f in sorted(report.get("testResults", []), key=lambda x: str(x.get("name", ""))):
        rel = _rel(str(f.get("name", "")), work)
        group = group_of(rel, layout)
        tests = f.get("assertionResults") or []
        titles = [x for a in tests for x in (a.get("ancestorTitles") or [])] + [
            str(a.get("title", "")) for a in tests
        ]
        text = (texts or {}).get(rel)
        if text is None and group == "hidden" and (work / rel).is_file():
            text = (work / rel).read_text("utf-8", errors="ignore")
        name = _name(group, rel, titles, case_rx, text)
        failed = [a for a in tests if a.get("status") == "failed"]
        if (
            not failed and f.get("status") == "failed"
        ):  # 파일을 못 읽음 (import·변환 실패) 또는 훅 실패
            msg = clean_message(str(f.get("message") or "테스트 파일 실행 실패"))
            out.append(
                {
                    "group": group,
                    "name": name,
                    "result": "HARNESS_ERR",
                    "reason": None,
                    "message": msg,
                }
            )
            continue
        if not failed:
            out.append(
                {"group": group, "name": name, "result": "PASS", "reason": None, "message": ""}
            )
            continue
        first = failed[0]
        raw = str((first.get("failureMessages") or [""])[0])
        msg = clean_message(raw)
        reason = _REASON.search(strip_ansi(raw))
        out.append(
            {
                "group": group,
                "name": name,
                "result": "ASSERT" if is_assertion(raw) else "HARNESS_ERR",
                "reason": reason.group(1) if reason else None,
                "message": clean_message(f"{first.get('title', '')} — {msg}") if msg else "",
            }
        )
    return out


# ── grade 명령 ───────────────────────────────────────────────────────


def prepare(cfg: Mapping[str, Any]) -> None:
    """제출물 → 작업 폴더: 의존성·빌드 산출물 빼고 복사, 채점 자리 비우기, 잠긴 파일 원복, 채점 오버레이 얹기, node_modules 링크."""
    if WORK.exists():
        shutil.rmtree(WORK)
    shutil.copytree(SUBMISSION, WORK, ignore=shutil.ignore_patterns(*SKIP), symlinks=False)
    for pattern in cfg["reserved"]:  # 채점 자리 — 지원자가 둔 파일은 지운다
        target = WORK / pattern.removesuffix("/**")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    template = Path(cfg["template_dir"])
    for rel in cfg["locked"]:  # 잠긴 파일 — 템플릿 원본으로 되돌린다
        src = template / rel
        if src.is_file():
            (WORK / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, WORK / rel)
    shutil.copytree(OVERLAY, WORK, dirs_exist_ok=True)  # 숨은·공개 테스트·하네스·조절값·계약 도우미
    (WORK / "node_modules").symlink_to(template / "node_modules", target_is_directory=True)


def run(cmd: Sequence[str], log: Path, timeout: int) -> int | None:
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"$ {' '.join(cmd)}\n")
        fh.flush()
        env = os.environ | {"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"}
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


def run_all(
    cfg: Mapping[str, Any], log: Path
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], float]:
    """tsc → vitest(기존) → vitest(채점). → (setup, tests, compile_errors, seconds)."""
    t0 = time.monotonic()
    timeout = int(cfg["timeout_s"])
    start = log.stat().st_size if log.exists() else 0
    code = run([*cfg["compile_command"], "--pretty", "false"], log, timeout)
    if code is None:
        return "timeout", [], [], round(time.monotonic() - t0, 1)
    if code != 0:
        with log.open("r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(start)
            return "compile_error", [], tsc_errors(fh.read()), round(time.monotonic() - t0, 1)
    tests: list[dict[str, Any]] = []
    for key, report_name in (
        ("existing_command", "existing-report.json"),
        ("grading_command", "grading-report.json"),
    ):
        report = OUT / report_name
        code = run([*cfg[key], "--reporter=json", f"--outputFile={report}"], log, timeout)
        if code is None:
            return "timeout", tests, [], round(time.monotonic() - t0, 1)
        if not report.is_file():  # vitest 자체가 못 떴다 (설정·의존성)
            return "boot_error", tests, [], round(time.monotonic() - t0, 1)
        tests += vitest_results(
            json.loads(report.read_text("utf-8")), WORK, cfg["layout"], cfg["case_id_pattern"]
        )
    return "ok", tests, [], round(time.monotonic() - t0, 1)


def result_json(
    cfg: Mapping[str, Any],
    setup: str,
    tests: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    seconds: float,
) -> dict[str, Any]:
    """케이스 목록(config)마다 통과 여부·사유 — Java grade(image_grade.py)와 같은 모양."""
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
