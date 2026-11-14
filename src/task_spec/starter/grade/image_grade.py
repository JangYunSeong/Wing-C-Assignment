#!/usr/bin/env python3
"""채점용 이미지의 `grade` 명령 — 목 데이터용 구현 (docs/design/starter-templates.md §6.1 의 플랫폼 계약).

    podman run --rm --network none -v <제출물>:/submission:ro -v <결과>:/out <grader 이미지> grade

/submission(응시 작업 경로 사본)을 복사해 잠긴 파일은 템플릿 원본으로 되돌리고, 채점 자리(reserved)에 지원자가 둔 파일은 지운 뒤
숨은·공개 테스트와 하네스를 얹어 오프라인 Gradle 로 돌린다. 결과는 /out/result.json + JUnit 보고서·로그.
판정은 Gradle 검증기(task_spec/starter/verify/gradle.py)와 같다. 표준 라이브러리만 쓴다 (이미지 안에서 돈다).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

CONFIG = Path(os.environ.get("GRADE_CONFIG", "/grader/config.json"))
OVERLAY = Path(os.environ.get("GRADE_OVERLAY", "/grader/overlay"))
SUBMISSION = Path(os.environ.get("GRADE_SUBMISSION", "/submission"))
OUT = Path(os.environ.get("GRADE_OUT", "/out"))
WORK = Path(os.environ.get("GRADE_WORK", "/tmp/grade-work"))
REASON = re.compile(r"\[reason:([a-z0-9_]+)\]")
ASSERTION = {"AssertionError", "AssertionFailedError", "MultipleFailuresError"}
# 건너뛴 테스트 — 판정하지 않은 것이라 통과로 세지 않는다 (#173: 조건부 건너뛰기가 검증을 지나 채점에서 통과로 기록될 수 있었다)
SKIPPED = "테스트를 건너뜀 — 판정하지 않았으므로 통과가 아니다"
BOOT = ("Failed to load ApplicationContext", "ApplicationContext failure threshold")
# 실행 정의 — 과제 생성 검증(starter/verify/gradle.py)도 이것을 쓴다 (#174: 검증은 `sh ./gradlew`, 채점은 실행 비트에
# 기대는 `./gradlew` 로 갈려 있었다). 테스트 도구는 비대화·색 없이 — 로그에서 판정 문구를 읽는다.
TEST_ENV = {"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"}
GRADLE_ARGS = ("--offline", "--console=plain", "--no-daemon", "--continue")


def gradle_command(task: str = "test") -> list[str]:
    """`sh ./gradlew` — 실행 비트 없이 (제출물·조립 폴더의 gradlew 권한에 기대지 않는다)."""
    return ["sh", "./gradlew", task, *GRADLE_ARGS]


def prepare(cfg: dict) -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    shutil.copytree(SUBMISSION, WORK, ignore=shutil.ignore_patterns("build", ".gradle", ".git"))
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
    shutil.copytree(OVERLAY, WORK, dirs_exist_ok=True)  # 숨은·공개 테스트·하네스·조절값


def run_tests(cfg: dict, log: Path) -> tuple[int | None, float]:
    t0 = time.monotonic()
    with log.open("w") as fh:
        try:
            proc = subprocess.run(
                cfg["test_command"],
                cwd=WORK,
                env=os.environ | TEST_ENV,
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=cfg["timeout_s"],
                check=False,
            )
            code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            code = None
    return code, round(time.monotonic() - t0, 1)


def failed_line(trace: str, simple: str, sources: Path | None = None) -> str:
    """실패 스택에서 그 테스트 클래스 파일의 첫 프레임 → 'Pt3Test.java:27 `assertThat(found).isTrue();`' (코드는 sources 가 있을 때)."""
    m = re.search(rf"\(({re.escape(simple)}\.java):(\d+)\)", trace)
    if m is None:
        return ""
    where = f"{m.group(1)}:{m.group(2)}"
    if sources is not None:
        for f in sources.rglob(m.group(1)):
            lines = f.read_text("utf-8", errors="ignore").splitlines()
            n = int(m.group(2))
            if 0 < n <= len(lines):
                where += f" `{lines[n - 1].strip()}`"
            break
    return where[:300]


def junit(
    results: Path, classes: dict[str, str], locate: bool = False, sources: Path | None = None
) -> list[dict]:
    """Gradle JUnit XML → 클래스마다 결과 하나 (첫 실패를 대표로): {group, name, result, reason, message[, at]}.
    과제 생성 검증(verify/gradle.junit_results)과 채점이 같은 판정을 쓴다 (#174). classes: 클래스 이름 → 결과 이름 — 채점은 숨은·공개
    모두 케이스 id(hc1·pt1)로, 검증은 숨은 것만 (공개는 클래스 이름 — 수리·관문이 공개 테스트 파일의 클래스 이름과 대조한다).
    locate: 실패한 줄(테스트 파일:줄, sources 가 있으면 그 줄의 코드까지)을 at 에 — 과제 생성 검증의 수리 안내용, 채점은 끈다."""
    out: list[dict[str, str | None]] = []
    for xml in sorted(results.glob("TEST-*.xml")):
        root = ET.parse(xml).getroot()  # Gradle 이 쓴 보고서 (값은 이스케이프됨)
        classname = root.get("name", xml.stem)
        first: tuple[str, str, str] | None = None
        skipped = None
        for tc in root.iter("testcase"):
            classname = tc.get("classname", classname)
            bad = tc.find("failure")
            if bad is None:
                bad = tc.find("error")
            if bad is not None and first is None:
                first = (
                    bad.get("type", ""),
                    bad.get("message", "") or (bad.text or ""),
                    bad.text or "",
                )
            if skipped is None and (sk := tc.find("skipped")) is not None:  # @Disabled·assume…
                skipped = f"{SKIPPED}: {tc.get('name', '')} {sk.get('message', '')}".strip()
        if first is None and skipped is not None:
            first = ("skipped", skipped, "")
        simple = classname.rsplit(".", 1)[-1]
        if ".grading.cases." in classname:
            group, name = "hidden", classes.get(simple, simple)
        elif ".publictests." in classname:
            group, name = "public", classes.get(simple, simple)
        elif ".grading." in classname:
            group, name = "selftest", simple
        else:
            group, name = "existing", simple
        if first is None:
            out.append({"group": group, "name": name, "result": "PASS"})
            continue
        kind, message, trace = first
        reason = REASON.search(message)
        row: dict[str, str | None] = {
            "group": group,
            "name": name,
            "result": "ASSERT" if kind.rsplit(".", 1)[-1] in ASSERTION else "HARNESS_ERR",
            "reason": reason.group(1) if reason else None,
            "message": " ".join(message.split())[:400],
        }
        if locate:
            row["at"] = failed_line(trace, simple, sources)
        out.append(row)
    return out


def main() -> int:
    cfg = json.loads(CONFIG.read_text("utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    if not SUBMISSION.is_dir():
        print(f"제출물이 없음: {SUBMISSION}", file=sys.stderr)
        return 2
    prepare(cfg)
    log = OUT / "grade.log"
    code, seconds = run_tests(cfg, log)
    text = log.read_text("utf-8", errors="ignore")
    results = WORK / "build" / "test-results" / "test"
    tests = junit(results, cfg["classes"]) if results.is_dir() else []
    if code is None:
        setup = "timeout"
    elif re.search(r"Task :compile(Test)?Java FAILED", text):
        setup = "compile_error"
    elif any(b in t.get("message", "") for t in tests for b in BOOT) or (not tests and code != 0):
        setup = "boot_error"
    else:
        setup = "ok"
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
    reports = OUT / "reports"
    if results.is_dir():
        shutil.copytree(results, reports, dirs_exist_ok=True)
    others = [t for t in tests if t["group"] in ("existing", "selftest")]
    result = {
        "common_id": cfg["common_id"],
        "revision": cfg["revision"],
        "setup": setup,
        "seconds": seconds,
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
        "report_path": str(reports),
    }
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
