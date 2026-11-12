"""이미지 만들기(`starter.images`) — 스택 선택·grade 설정. 가상 데이터만, podman 없이."""

from __future__ import annotations

from task_spec.schemas import GeneratedFile, HiddenPlan, TaskContract
from task_spec.starter import images as b
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import load_template
from tests.starter.test_kit import _task

PUBLIC = Overlay(kind="public_test", files=(GeneratedFile(path="x", content="가상"),))


def _material(plan: HiddenPlan, contract: TaskContract) -> b.TaskMaterial:
    return b.TaskMaterial(
        source="가상-빌드",
        task=_task(),
        contract=contract,
        plan=plan,
        common_id="c",
        revision=1,
        ontology_version="onto-v2026.01.01",
        readme="# 가상",
        starter=None,
        hidden=None,
        public=PUBLIC,
        refs={},
    )


def test_python_stack_and_grade_config() -> None:
    """데이터 직군 → Python 스택(python 3.11 slim-bookworm, /opt/venv), grade 설정은 Python 검증기와 같은 대상·설정 파일."""
    from tests.starter.test_data_template import _contract, _plan

    t = load_template("data/python-duckdb", 1)
    assert b.stack_for(t) is b.PY and "3.11-slim-bookworm" in b.PY.base
    assert b.PY.grader_setup == () and b.NODE.grader_setup == (
        b.PYTHON3,
    )  # Python 런타임엔 python3 설치 생략
    cfg = b.py_config(t, _material(_plan(), _contract()))
    assert cfg["python"] == "/opt/venv/bin/python" and cfg["template_dir"] == "/workspace"
    assert cfg["grading_paths"] == ["grading/selftest", "grading/cases", "public_tests"]
    assert cfg["grading_ini"] == "grading/harness/grader-pytest.ini"
    assert cfg["layout"] == {
        "cases": "grading/cases",
        "selftest": "grading/selftest",
        "public": "public_tests",
    }
    assert "pipeline" in cfg["compile_paths"]
    assert "uv.lock" in cfg["locked"] and "grading/**" in cfg["reserved"]
    assert [(c["id"], c["kind"]) for c in cfg["cases"]] == [("hc1", "hidden"), ("pt1", "public")]


def test_node_stack_and_grade_config() -> None:
    """Node grade 설정은 판정에 필요한 명령·배치·케이스를 담는다."""
    from tests.starter.test_frontend_template import _contract, _plan

    fe = load_template("frontend/react-vite-ts")
    assert b.stack_for(fe) is b.NODE and "bookworm" in b.NODE.base
    cfg = b.node_config(fe, _material(_plan(), _contract()))
    assert cfg["compile_command"] == ["npx", "tsc", "--noEmit"]
    assert cfg["grading_command"][-1].endswith("vitest.grading.config.ts")
    assert (
        cfg["layout"]["cases"] == "src/__grading__/cases"
        and cfg["layout"]["public"] == "src/__public__"
    )
    assert [(c["id"], c["kind"]) for c in cfg["cases"]] == [("hc1", "hidden"), ("pt1", "public")]


def test_grade_scripts_are_the_moved_standalone_files() -> None:
    """채점 기반 층에 복사하는 grade 스크립트 — starter/grade/ 의 단독 실행 파일 (task_spec 을 import 하지 않음)."""
    for st in (b.JAVA, b.NODE, b.PY):
        assert st.grade_script.is_file() and st.grade_script.parent.name == "grade"
        assert "task_spec" not in st.grade_script.read_text("utf-8").split('"""', 2)[2]
