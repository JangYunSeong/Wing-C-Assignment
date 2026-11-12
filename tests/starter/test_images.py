"""이미지 만들기(`starter.images`) — 스택 선택·grade 설정. 가상 데이터만, podman 없이."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import tarfile
from pathlib import Path

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


def _digest(base: Path, files: list[Path]) -> str:
    """해시 공식을 따로 적어 고정한다 — 저장소가 빌드 결과와 묶음의 tree·grader 해시가 같은지 본다 (바뀌면 기존 빌드와 안 맞음)."""
    h = hashlib.sha256()
    for p in files:
        h.update(
            str(p.relative_to(base)).encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest()
        )
    return h.hexdigest()


def test_tree_and_grader_hashes_keep_their_formula(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "README.md").write_text("가상", "utf-8")
    (ws / "src" / "a.py").write_text("x = 1", "utf-8")
    assert b.tree_sha256(ws) == _digest(ws, [ws / "README.md", ws / "src" / "a.py"])
    k = tmp_path / "grader"
    (k / "overlay" / "t").mkdir(parents=True)
    (k / "overlay" / "t" / "b.java").write_text("class B {}", "utf-8")
    (k / "overlay" / "a.txt").write_text("가상", "utf-8")
    (k / "config.json").write_text("{}", "utf-8")
    (k / "Containerfile").write_text("FROM x", "utf-8")  # 해시에 넣지 않음
    want = _digest(k, [k / "overlay" / "a.txt", k / "overlay" / "t" / "b.java", k / "config.json"])
    assert b.grader_sha256(k) == want


def test_java_grader_overlay_writes_layers_and_config(tmp_path: Path) -> None:
    """Java 채점 설정 — 숨은 케이스는 계획에서, 클래스 이름 → 케이스 id 는 테스트 본문에서, 공개 테스트는 pt id(없으면 클래스 이름)."""
    from tests.starter.test_data_template import _contract, _plan

    t = load_template("backend/java-spring-boot3-jpa", 2)
    hidden = Overlay(
        kind="hidden",
        files=(
            GeneratedFile(
                path="src/test/java/x/HiddenOneTest.java", content='@DisplayName("hc1 가상")'
            ),
        ),
    )
    public = Overlay(
        kind="public_test",
        files=(
            GeneratedFile(
                path="src/test/java/x/PubOneTest.java", content='@DisplayName("pt1 가상")'
            ),
            GeneratedFile(path="src/test/java/x/SaveCheckTest.java", content="// id 없음"),
        ),
    )
    item = dataclasses.replace(_material(_plan(), _contract()), hidden=hidden, public=public)
    dest = tmp_path / "overlay"
    cfg = b.grader_overlay(t, item, dest)
    assert (dest / "src/test/java/x/HiddenOneTest.java").is_file()
    assert (dest / "src/test/java/x/PubOneTest.java").is_file()
    hl = t.manifest.hidden_layout
    assert hl is not None and any((dest / hl.harness).rglob("*.java"))  # 잠긴 하네스
    assert cfg["test_command"] == b.TEST_COMMAND and cfg["template_ref"] == t.manifest.ref
    assert cfg["classes"] == {"HiddenOneTest": "hc1", "PubOneTest": "pt1"}
    assert [(c["id"], c["kind"]) for c in cfg["cases"]] == [
        ("hc1", "hidden"),
        ("pt1", "public"),
        ("SaveCheckTest", "public"),
    ]


def _layer(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_scan_candidates_finds_forbidden_paths_in_any_layer(tmp_path: Path) -> None:
    """위 레이어에서 지워도 아래 레이어에 남은 금지 경로를 찾는다. 런타임 경로(scan_skip)와 다른 이미지는 보지 않는다."""
    from task_spec.starter.templates import candidate_layer_rules

    t = load_template("data/python-duckdb", 1)
    rules = candidate_layer_rules(t)
    bad = "workspace/grading/cases/test_hc1.py"
    assert b.forbidden_in([bad], rules), "가상 금지 경로가 규칙에 걸려야 시험이 뜻이 있다"
    archive = tmp_path / "images.tar"
    manifest = [
        {"RepoTags": ["localhost/task/c-r1-candidate:latest"], "Layers": ["l1.tar", "l2.tar"]},
        {"RepoTags": ["localhost/task/c-r1-grader:latest"], "Layers": ["l3.tar"]},
    ]
    with tarfile.open(archive, "w") as tar:
        for name, data in {
            "manifest.json": json.dumps(manifest).encode(),
            "l1.tar": _layer({bad: b"x", "usr/lib/grading/cases/x.py": b"x"}),
            "l2.tar": _layer({"workspace/pipeline/run.py": b"x"}),
            "l3.tar": _layer({"workspace/grading/cases/test_hc2.py": b"x"}),
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    got = b.scan_candidates(archive, {"localhost/task/c-r1-candidate": t})
    assert got == {"localhost/task/c-r1-candidate": [bad]}
