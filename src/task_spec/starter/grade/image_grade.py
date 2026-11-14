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
    (WORK / "gradlew").chmod(0o755)


def run_tests(cfg: dict, log: Path) -> tuple[int | None, float]:
    t0 = time.monotonic()
    with log.open("w") as fh:
        try:
            proc = subprocess.run(
                cfg["test_command"],
                cwd=WORK,
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=cfg["timeout_s"],
                check=False,
            )
            code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            code = None
    return code, round(time.monotonic() - t0, 1)


def junit(results: Path, classes: dict[str, str]) -> list[dict]:
    """클래스마다 결과 하나 (첫 실패를 대표로) — gradle_verify.junit_results 와 같은 판정."""
    out: list[dict[str, str | None]] = []
    for xml in sorted(results.glob("TEST-*.xml")):
        root = ET.parse(xml).getroot()
        classname = root.get("name", xml.stem)
        first = None
        skipped = None
        for tc in root.iter("testcase"):
            classname = tc.get("classname", classname)
            bad = tc.find("failure")
            if bad is None:
                bad = tc.find("error")
            if bad is not None and first is None:
                first = (bad.get("type", ""), bad.get("message", "") or (bad.text or ""))
            if skipped is None and (sk := tc.find("skipped")) is not None:  # @Disabled·assume…
                skipped = f"{SKIPPED}: {tc.get('name', '')} {sk.get('message', '')}".strip()
        if first is None and skipped is not None:
            first = ("skipped", skipped)
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
        kind, message = first
        reason = REASON.search(message)
        row: dict[str, str | None] = {
            "group": group,
            "name": name,
            "result": "ASSERT" if kind.rsplit(".", 1)[-1] in ASSERTION else "HARNESS_ERR",
            "reason": reason.group(1) if reason else None,
            "message": " ".join(message.split())[:400],
        }
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
