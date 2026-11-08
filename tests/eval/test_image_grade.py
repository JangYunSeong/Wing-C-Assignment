"""채점용 이미지의 grade 스크립트 (eval/starter/image_grade.py) — 제출물 정리·판정. 가상 데이터만, Gradle 없이."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.starter import image_grade

XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="{cls}" tests="1">
  <testcase name="t" classname="{cls}">{body}</testcase>
</testsuite>
"""


def _xml(results: Path, cls: str, body: str = "") -> None:
    results.mkdir(parents=True, exist_ok=True)
    (results / f"TEST-{cls}.xml").write_text(XML.format(cls=cls, body=body), "utf-8")


def test_junit_groups_and_reasons(tmp_path: Path) -> None:
    r = tmp_path / "r"
    _xml(
        r,
        "com.example.service.grading.cases.Hc1Test",
        '<failure type="org.opentest4j.AssertionFailedError" message="[[reason:pool_acquire_timeout] x] expected 0"/>',
    )
    _xml(r, "com.example.service.publictests.Pt1Test")
    _xml(
        r,
        "com.example.service.grading.selftest.HarnessSelfTest",
        '<error type="java.lang.IllegalStateException" message="boom"/>',
    )
    _xml(r, "com.example.service.order.OrderTest")
    out = {
        (t["group"], t["name"]): t
        for t in image_grade.junit(r, {"Hc1Test": "hc1", "Pt1Test": "pt1"})
    }
    assert (
        out[("hidden", "hc1")]["result"] == "ASSERT"
        and out[("hidden", "hc1")]["reason"] == "pool_acquire_timeout"
    )
    assert out[("public", "pt1")]["result"] == "PASS"
    assert out[("selftest", "HarnessSelfTest")]["result"] == "HARNESS_ERR"
    assert out[("existing", "OrderTest")]["result"] == "PASS"


def test_prepare_restores_locked_files_and_clears_grading_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """지원자가 잠긴 파일을 바꾸거나 채점 자리에 가짜 테스트를 둬도 채점 전에 되돌린다."""
    template, sub, overlay, work = (tmp_path / x for x in ("template", "sub", "overlay", "work"))
    (template / "gradlew").parent.mkdir(parents=True)
    (template / "gradlew").write_text("#!/bin/sh\n")
    (template / "build.gradle").write_text("// 템플릿 원본\n")
    (sub / "src/test/java/com/example/service/grading/cases").mkdir(parents=True)
    (sub / "src/test/java/com/example/service/grading/cases/Hc1Test.java").write_text(
        "// 지원자가 둔 가짜\n"
    )
    (sub / "build.gradle").write_text("// 지원자가 바꾼 것\n")
    (sub / "gradlew").write_text("#!/bin/sh\n")
    (sub / "src/main/App.java").parent.mkdir(parents=True)
    (sub / "src/main/App.java").write_text("class App {}\n")
    (overlay / "src/test/java/com/example/service/grading/cases").mkdir(parents=True)
    (overlay / "src/test/java/com/example/service/grading/cases/Hc1Test.java").write_text(
        "// 진짜 숨은 테스트\n"
    )
    for name, value in (("SUBMISSION", sub), ("OVERLAY", overlay), ("WORK", work)):
        monkeypatch.setattr(image_grade, name, value)
    image_grade.prepare(
        {
            "template_dir": str(template),
            "locked": ["build.gradle", "gradlew"],
            "reserved": [
                "src/test/java/com/example/service/grading/**",
                "src/test/resources/grader-params.properties",
            ],
        }
    )
    assert (work / "build.gradle").read_text() == "// 템플릿 원본\n"
    assert (
        work / "src/test/java/com/example/service/grading/cases/Hc1Test.java"
    ).read_text() == "// 진짜 숨은 테스트\n"
    assert (work / "src/main/App.java").is_file()


def test_main_reports_setup_failure_for_every_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기동 실패면 케이스마다 그 사유 — 결과 파일 모양은 그대로."""
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "common_id": "c",
                "revision": 1,
                "template_dir": str(tmp_path),
                "locked": [],
                "reserved": [],
                "test_command": ["true"],
                "timeout_s": 10,
                "classes": {"Hc1Test": "hc1"},
                "cases": [
                    {"id": "hc1", "trap_id": "t1", "kind": "hidden"},
                    {"id": "pt1", "kind": "public"},
                ],
            }
        )
    )
    (tmp_path / "sub").mkdir()
    (tmp_path / "overlay").mkdir()
    (tmp_path / "sub" / "gradlew").write_text("#!/bin/sh\n")
    monkeypatch.setattr(image_grade, "CONFIG", cfg)
    monkeypatch.setattr(image_grade, "SUBMISSION", tmp_path / "sub")
    monkeypatch.setattr(image_grade, "OVERLAY", tmp_path / "overlay")
    monkeypatch.setattr(image_grade, "WORK", tmp_path / "work")
    monkeypatch.setattr(image_grade, "OUT", tmp_path / "out")
    _xml(
        tmp_path / "work-results",
        "com.example.service.grading.cases.Hc1Test",
        '<error type="java.lang.IllegalStateException" message="Failed to load ApplicationContext"/>',
    )

    def fake_run(c: dict, log: Path) -> tuple[int | None, float]:
        log.write_text("BUILD FAILED\n")
        dest = image_grade.WORK / "build" / "test-results" / "test"
        dest.mkdir(parents=True)
        for x in (tmp_path / "work-results").iterdir():
            (dest / x.name).write_text(x.read_text())
        return 1, 0.1

    monkeypatch.setattr(image_grade, "run_tests", fake_run)
    assert image_grade.main() == 0
    result = json.loads((tmp_path / "out" / "result.json").read_text())
    assert result["setup"] == "boot_error"
    assert [(c["id"], c["passed"], c["reason"]) for c in result["cases"]] == [
        ("hc1", False, "boot_error"),  # 사유 표시가 없는 실패는 기동 단계 사유로
        ("pt1", False, "boot_error"),
    ]
