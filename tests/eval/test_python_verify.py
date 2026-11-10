"""데이터(Python) 템플릿 판정 — pytest JUnit(xunit1)·컴파일 출력을 결과로, 이미지 grade 의 제출물 정리 (python·podman 없이, 가상 데이터)."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.starter import image_grade_py as g

LAYOUT = {"cases": "grading/cases", "selftest": "grading/selftest", "public": "public_tests"}
XML = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">
<testcase classname="test_grader_selftest" name="test_exit" file="../selftest/test_grader_selftest.py" line="3"/>
<testcase classname="test_hc1_late" name="test_hc1_late_payment" file="../cases/test_hc1_late.py" line="8">
  <failure message="AssertionError: [reason:late_data_missed] 9/1 빠짐&#10;assert {} == {}">long</failure></testcase>
<testcase classname="test_hc2_crash" name="test_hc2_crash" file="../cases/test_hc2_crash.py" line="8">
  <failure message="assert False&#10; +  where False = JobResult(code=1).ok">long</failure></testcase>
<testcase classname="test_misc" name="test_x" file="../cases/test_misc.py" line="5">
  <failure message="NameError: name 'ws' is not defined">long</failure></testcase>
<testcase classname="test_pt1_summary" name="test_pt1_daily" file="../../public_tests/test_pt1_summary.py" line="9"/>
</testsuite></testsuites>"""


def test_junit_xunit1_files_become_groups_case_ids_and_reasons(tmp_path: Path) -> None:
    xml = tmp_path / "g.xml"
    xml.write_text(XML, "utf-8")
    (tmp_path / "grading/cases").mkdir(parents=True)
    (tmp_path / "grading/cases/test_misc.py").write_text('"""hc3 사본"""\n', "utf-8")
    got = {
        (r["group"], r["name"]): r
        for r in g.junit_results(xml, "grading/harness", LAYOUT, r"\bhc\d{1,2}\b", tmp_path)
    }
    assert got[("selftest", "grading/selftest/test_grader_selftest.py")]["result"] == "PASS"
    assert (
        got[("hidden", "hc1")]["result"] == "ASSERT"
        and got[("hidden", "hc1")]["reason"] == "late_data_missed"
    )
    assert (
        got[("hidden", "hc2")]["result"] == "ASSERT" and got[("hidden", "hc2")]["reason"] is None
    )  # 맨 assert 도 단언
    assert got[("hidden", "hc3")]["result"] == "HARNESS_ERR"  # 예외 · 이름은 파일 본문에서
    assert got[("public", "pt1")]["result"] == "PASS"


def test_compile_errors_from_compileall_and_collect_only() -> None:
    log = (
        "*** Error compiling './pipeline/run.py'...\n"
        '  File "./pipeline/run.py", line 4\n'
        "    def x(:\n"
        "SyntaxError: invalid syntax\n"
        "_____ ERROR collecting test_hc9.py _____\n"
        "grading/cases/test_hc9.py:1: in <module>\n"
        "    from cases.contract import run_nope\n"
        "E   ImportError: cannot import name 'run_nope' from 'cases.contract' (/x/grading/cases/contract.py)\n"
    )
    assert g.compile_errors(log) == [
        {
            "path": "pipeline/run.py",
            "line": 4,
            "message": "SyntaxError: invalid syntax",
            "detail": "",
        },
        {
            "path": "grading/cases/test_hc9.py",
            "line": 1,
            "message": "ImportError: cannot import name 'run_nope' from 'cases.contract'",
            "detail": "",
        },
    ]


def test_prepare_drops_candidate_venv_and_reserved_and_restores_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub, tpl, ov, work = (tmp_path / n for n in ("submission", "template", "overlay", "work"))
    for path, text in (
        (sub / "pipeline/run.py", "job"),
        (sub / "pyproject.toml", "바꾼 의존성"),
        (sub / ".venv/lib/x.py", "지원자 가상 환경"),
        (sub / "grading/cases/test_hc1.py", "지원자가 둔 가짜 숨은 테스트"),
        (tpl / "pyproject.toml", "템플릿 원본"),
        (ov / "grading/cases/test_hc1.py", "진짜 숨은 테스트"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
    for name, val in (("SUBMISSION", sub), ("OVERLAY", ov), ("WORK", work)):
        monkeypatch.setattr(g, name, val)
    g.prepare({"reserved": ["grading/**"], "locked": ["pyproject.toml"], "template_dir": str(tpl)})
    assert (work / "pipeline/run.py").read_text() == "job" and not (work / ".venv").exists()
    assert (work / "pyproject.toml").read_text() == "템플릿 원본"
    assert (work / "grading/cases/test_hc1.py").read_text() == "진짜 숨은 테스트"


def test_mock_image_builder_python_stack_and_grade_config(tmp_path: Path) -> None:
    """데이터 직군 → Python 스택(python 3.11 slim-bookworm, /opt/venv), grade 설정은 개발 서버 검증기와 같은 대상·설정 파일."""
    import json

    from eval.starter import build_mock_images as b
    from task_spec.starter.templates import load_template
    from tests.starter.test_data_template import _contract, _plan

    run = tmp_path / "de"
    run.mkdir()
    (run / "report.json").write_text(json.dumps({"sub": "data.eng"}), "utf-8")
    t = b.template_of(run)
    assert t.manifest.ref == "data/python-duckdb@v1"
    assert b.stack_for(t) is b.PY and "3.11-slim-bookworm" in b.PY.base
    assert b.PY.grader_setup == () and b.NODE.grader_setup == (
        b.PYTHON3,
    )  # Python 런타임엔 python3 설치 생략
    assert b.stack_for(load_template("frontend/react-vite-ts")) is b.NODE
    cfg = b.py_config(
        t,
        {
            "common_id": "c",
            "revision": 1,
            "plan": _plan(),
            "contract": _contract(),
            "public": object(),
        },
    )
    assert cfg["python"] == "/opt/venv/bin/python" and cfg["template_dir"] == "/workspace"
    assert cfg["grading_paths"] == ["grading/selftest", "grading/cases", "public_tests"]
    assert cfg["grading_ini"] == "grading/harness/grader-pytest.ini"
    assert cfg["layout"] == LAYOUT and "pipeline" in cfg["compile_paths"]
    assert "uv.lock" in cfg["locked"] and "grading/**" in cfg["reserved"]
    assert [(c["id"], c["kind"]) for c in cfg["cases"]] == [("hc1", "hidden"), ("pt1", "public")]


def test_pytest_collection_syntax_error_is_located_relative_to_the_assembly() -> None:
    """2026-11-10 qwen 알파: 공개 테스트 문법 오류가 pytest 수집에서 `E     File "<절대 경로>", line N` 로 나와 못 읽고 재요청 0회로 끝났다."""
    log = (
        "_____________ ERROR collecting test_pt1_x.py ______________\n"
        "/usr/lib64/python3.11/ast.py:50: in parse\n"
        "    return compile(source, filename, mode, flags,\n"
        'E     File "/work/r1/public_tests/test_pt1_x.py", line 31\n'
        "E       actual = {(x['a']: 1 for x in rows)}\n"
        "E                        ^\n"
        "E   SyntaxError: invalid syntax\n"
    )
    assert g.compile_errors(log, root="/work/r1") == [
        {
            "path": "public_tests/test_pt1_x.py",
            "line": 31,
            "message": "SyntaxError: invalid syntax",
            "detail": "",
        }
    ]
