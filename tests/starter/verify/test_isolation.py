"""검증 격리 (#127) — 생성 코드는 `--network none` 자식 컨테이너에서만. 명령줄·모드 전환·검증용 층 재사용 (podman 없이).

실제 podman 으로 네트워크 차단·시간 초과 정리를 보는 시험은 맨 아래 (`-m podman`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from task_spec.starter import layers
from task_spec.starter.templates import load_template
from task_spec.starter.verify import gradle, workspace
from task_spec.starter.verify import python as pyv
from task_spec.starter.verify.gradle import GradleVerifier
from task_spec.starter.verify.python import PythonVerifier


def test_container_argv_has_no_network_limits_and_same_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VERIFY_CPUS", raising=False)
    monkeypatch.delenv("VERIFY_MEMORY", raising=False)
    work, extra = tmp_path / "w", tmp_path / "nm"
    argv = workspace.container_argv(
        "img:1", ["sh", "-c", "x"], work, "vf-1", [extra], env={"A": "1"}
    )
    assert argv[:2] == ["podman", "run"] and argv[-4:] == ["img:1", "sh", "-c", "x"]
    s = " ".join(argv)
    assert "--network none" in s and "--cpus 2" in s and "--memory 4g" in s
    assert f"-w {work.resolve()}" in s and "--security-opt label=disable" in s
    assert (
        f"-v {work.resolve()}:{work.resolve()}" in s
        and f"-v {extra.resolve()}:{extra.resolve()}" in s
    )
    assert "-e A=1" in s and "--rm" in s and "--name vf-1" in s
    install = workspace.container_argv("img:1", ["npm", "ci"], work, "vf-2", network=True)
    assert "--network" not in install  # 의존성 설치만 — 생성 코드가 없을 때
    monkeypatch.setenv("VERIFY_CPUS", "0")
    monkeypatch.setenv("VERIFY_MEMORY", "0")
    assert "--cpus" not in workspace.container_argv("i", ["x"], work, "n")
    assert "--memory" not in workspace.container_argv("i", ["x"], work, "n")


def test_isolation_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERIFY_ISOLATION", raising=False)
    assert workspace.isolated()
    monkeypatch.setenv("VERIFY_ISOLATION", "host")
    assert not workspace.isolated()


def _capture(calls: list[dict[str, Any]]) -> Any:
    def fake(
        image: str, cmd: list[str], cwd: Path, log: Path, timeout: int, *a: Any, **k: Any
    ) -> tuple[int, float]:
        calls.append(
            {
                "image": image,
                "cmd": list(cmd),
                "cwd": cwd,
                "network": k.get("network", a[1] if len(a) > 1 else False),
                "env": k.get("env") or {},
            }
        )
        return 0, 1.0

    return fake


def test_gradle_runs_the_wrapper_offline_on_the_verify_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VERIFY_ISOLATION", raising=False)
    calls: list[dict[str, Any]] = []
    layered: list[str] = []
    monkeypatch.setattr(
        gradle,
        "ensure_verify_layer",
        lambda t, work: layered.append(t.manifest.ref) or "localhost/task-verify/x",
    )
    monkeypatch.setattr(gradle, "run_in_container", _capture(calls))
    monkeypatch.setattr(
        gradle, "run_gradle", lambda *a, **k: pytest.fail("호스트 Gradle 을 부르면 안 된다")
    )
    v = GradleVerifier(load_template("backend/java-spring-boot3-jpa", 2), tmp_path, timeout=30)
    dest = tmp_path / "d"
    dest.mkdir()
    v._gradle(dest, tmp_path / "a.log")
    v._gradle(dest, tmp_path / "b.log", "compileTestJava")
    assert len(layered) == 1  # 층은 처음 한 번만 확인
    assert calls[0]["image"] == "localhost/task-verify/x" and calls[0]["cwd"] == dest
    assert calls[0]["cmd"][:4] == ["sh", "./gradlew", "test", "--offline"]
    assert calls[1]["cmd"][2] == "compileTestJava" and not calls[0]["network"]
    assert calls[0]["env"].get("CI") == "1"  # 호스트 실행과 같은 환경 변수


def test_gradle_host_mode_keeps_the_old_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERIFY_ISOLATION", "host")
    seen: list[str] = []
    monkeypatch.setattr(
        gradle, "run_gradle", lambda dest, log, timeout, task="test": seen.append(task) or (0, 1.0)
    )
    monkeypatch.setattr(
        gradle, "run_in_container", lambda *a, **k: pytest.fail("컨테이너를 부르면 안 된다")
    )
    v = GradleVerifier(load_template("backend/java-spring-boot3-jpa", 2), tmp_path, timeout=30)
    v._gradle(tmp_path, tmp_path / "a.log")
    assert seen == ["test"]


def test_python_uses_the_layer_venv_in_a_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VERIFY_ISOLATION", raising=False)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(pyv, "ensure_verify_layer", lambda t, work: "localhost/task-verify/py")
    monkeypatch.setattr(pyv, "run_in_container", _capture(calls))
    monkeypatch.setattr(
        pyv, "python_for", lambda *a, **k: pytest.fail("호스트 가상 환경을 만들면 안 된다")
    )
    v = PythonVerifier(load_template("data/python-duckdb", 1), tmp_path, timeout=30)
    assert v._py() == "/opt/venv/bin/python"
    assert v._run([v._py(), "-m", "pytest"], tmp_path, tmp_path / "a.log") == 0
    assert (
        calls[0]["image"] == "localhost/task-verify/py"
        and calls[0]["cmd"][0] == "/opt/venv/bin/python"
    )
    # 2026-11-12 격리 비교: CI 가 없으면 pytest 가 단언 diff 를 "Use -v" 로 줄여 수리 근거 메시지가 호스트 실행과 달랐다
    assert calls[0]["env"].get("CI") == "1"


class _FakePodman:
    def __init__(self, label: str) -> None:
        self.label, self.calls = label, []  # type: ignore[var-annotated]

    def __call__(self, *args: str, log: Path | None = None, check: bool = True, **_: Any) -> str:
        self.calls.append(args)
        return self.label + "\n" if args[:2] == ("image", "inspect") else ""


def test_verify_layer_is_reused_until_the_template_changes(tmp_path: Path) -> None:
    t = load_template("data/python-duckdb", 1)
    digest = layers.project_sha256(t)
    same = _FakePodman(digest)
    assert layers.ensure_verify_layer(t, tmp_path, same) == layers.verify_layer_name(t)  # type: ignore[arg-type]
    assert [c[0] for c in same.calls] == ["image"]  # 확인만, 빌드 없음
    stale = _FakePodman("다른 해시")
    layers.ensure_verify_layer(t, tmp_path, stale)  # type: ignore[arg-type]
    assert stale.calls[-1][:3] == ("build", "-t", layers.verify_layer_name(t))
    cf = (
        tmp_path
        / "verify-layers"
        / layers.verify_layer_name(t).rsplit("/", 1)[-1]
        / "Containerfile"
    ).read_text()
    assert cf.startswith(f"FROM {layers.PY_IMAGE}") and f'project_sha256="{digest}"' in cf
    assert "uv sync --frozen" in cf  # 채점 이미지 템플릿 층과 같은 설치


def test_project_hash_ignores_host_build_leftovers(tmp_path: Path) -> None:
    """호스트 실행이 남긴 .gradle·build 는 해시에서 뺀다 — 남은 캐시 때문에 층을 다시 만들지 않게."""
    import shutil

    from task_spec.starter.templates import Template

    t = load_template("backend/java-spring-boot3-jpa", 2)
    root = tmp_path / "tpl"
    shutil.copytree(t.root, root, ignore=shutil.ignore_patterns(".gradle", "build"))
    copy = Template(root=root, manifest=t.manifest)
    before = layers.project_sha256(copy)
    (copy.project / ".gradle" / "x").mkdir(parents=True)
    (copy.project / ".gradle" / "x" / "lock").write_text("가상", "utf-8")
    assert layers.project_sha256(copy) == before
    (copy.project / "settings.gradle").write_text("rootProject.name = '가상'", "utf-8")
    assert layers.project_sha256(copy) != before


# ── 실제 podman (-m podman) ──────────────────────────────────────────


@pytest.mark.podman
def test_generated_code_cannot_reach_the_network(tmp_path: Path) -> None:
    code, _ = workspace.run_in_container(
        layers.NODE_IMAGE,
        [
            "node",
            "-e",
            "fetch('http://example.com').then(()=>process.exit(0)).catch(()=>process.exit(3))",
        ],
        tmp_path,
        tmp_path / "net.log",
        timeout=60,
    )
    assert code == 3, (tmp_path / "net.log").read_text()


@pytest.mark.podman
def test_timeout_leaves_no_container(tmp_path: Path) -> None:
    code, _ = workspace.run_in_container(
        layers.NODE_IMAGE, ["sleep", "30"], tmp_path, tmp_path / "t.log", timeout=3
    )
    assert code is None
    left = subprocess.run(
        ["podman", "ps", "-a", "--filter", "name=^vf-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert left == []
