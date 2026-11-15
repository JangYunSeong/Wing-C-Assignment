"""검증기 공통 — 대상 조립(템플릿 project/ + 오버레이), 로그를 남기는 명령 실행, 격리 실행(`run_in_container`).

조립 순서는 채점용 이미지와 같다 (`task_spec.starter.images.grader_overlay`). 세 검증기(gradle·node·python)와 이미지 만들기가 같이 쓴다.

격리 (#127): LLM 이 만든 코드(시작 코드·숨은 테스트·참조 구현)는 `--network none` 자식 컨테이너에서만 돈다 — CPU·메모리 상한,
조립 폴더는 호스트와 같은 절대 경로로 붙인다 (podman 소켓을 거쳐 불러도 `-v`·`-w` 가 호스트 기준으로 맞게, #129).

| 환경 변수 | 뜻 | 기본 |
|---|---|---|
| `VERIFY_ISOLATION` | `container` · `host` (호스트에서 바로 — 개발용 탈출구, 격리 없음) | `container` |
| `VERIFY_CPUS` | 검증 컨테이너 CPU 상한 (`0` 이면 상한 없음) | `2` |
| `VERIFY_MEMORY` | 검증 컨테이너 메모리 상한 (`0` 이면 상한 없음) | `4g` |
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from task_spec.starter.mounts import volume
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template

# 테스트 도구(vitest·pytest)를 비대화·색 없이 — 로그에서 판정 문구를 읽는다
CI_ENV = {"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"}


def write_overlay(overlay: Overlay, root: Path) -> None:
    for f in overlay.files:
        p = root / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.content, "utf-8")


def assemble(t: Template, work: Path, *overlays: Overlay) -> None:
    """템플릿 project/ + 오버레이 (+ 숨은·공개 테스트가 있으면 잠긴 하네스·자체 점검) — 채점용 이미지 조립과 같은 순서.
    공개 테스트(PublicCase)도 하네스(GraderSupport)를 상속한다 — 2026-11-08: 숨은 테스트일 때만 하네스를 넣어 시작 코드 + 공개 테스트가
    컴파일되지 않았다 (실제 Gradle 로 확인)."""
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(t.project, work)
    hl = t.manifest.hidden_layout
    if hl is not None and any(o.kind in ("hidden", "public_test") for o in overlays):
        for sub, dest in (("hidden/harness", hl.harness), ("hidden/selftest", hl.selftest)):
            for rel in t.files(sub):
                target = work / dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(t.root / sub / rel, target)
    for o in overlays:
        write_overlay(o, work)


def run_logged(
    cmd: Sequence[str],
    cwd: Path,
    log: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> tuple[int | None, float]:
    """명령 하나 — 출력은 log 에 덧붙인다. 시간 초과면 프로세스 묶음(vitest 일꾼 포함)을 끊고 None.
    환경은 지금 프로세스 환경 + env (주지 않으면 `CI_ENV`) — 테스트 실행은 스택의 채점 스크립트 TEST_ENV 를 준다 (#174)."""
    start = time.monotonic()
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"$ {shlex.join(cmd)}\n")
        fh.flush()
        p = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=os.environ | dict(env if env is not None else CI_ENV),
            start_new_session=True,
        )
        try:
            code: int | None = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            code = None
    return code, time.monotonic() - start


def isolated() -> bool:
    """생성 코드를 컨테이너에서 돌리는가 — `VERIFY_ISOLATION=host` 일 때만 아니다."""
    return os.environ.get("VERIFY_ISOLATION", "container") != "host"


def limits() -> list[str]:
    out: list[str] = []
    cpus = os.environ.get("VERIFY_CPUS", "2")
    memory = os.environ.get("VERIFY_MEMORY", "4g")
    if cpus not in ("", "0"):
        out += ["--cpus", cpus]
    if memory not in ("", "0"):
        out += ["--memory", memory]
    return out


def container_argv(
    image: str,
    cmd: Sequence[str],
    cwd: Path,
    name: str,
    mounts: Sequence[Path] = (),
    network: bool = False,
    env: dict[str, str] | None = None,
    cwd_shared: bool = False,
) -> list[str]:
    """`podman run` 명령줄 — cwd·mounts 를 같은 절대 경로로 붙이고 그 cwd 에서 cmd 를 돈다.
    마운트 정책(starter/mounts.py): cwd 는 이번 실행 전용 조립 폴더라 `:Z`, mounts 는 여러 실행이 함께 쓰는 캐시라 `:z`.
    cwd_shared — cwd 자체가 함께 쓰는 캐시일 때 (Node 의존성 설치)."""
    shared = {str(p.resolve()) for p in mounts} | ({str(cwd.resolve())} if cwd_shared else set())
    paths = sorted({str(p.resolve()) for p in (cwd, *mounts)})
    full = ["podman", "run", "--rm", "--name", name, "-w", str(cwd.resolve())]
    full += limits()  # SELinux 격리는 켜 둔다 — 라벨은 마운트마다 (starter/mounts.py)
    full += [] if network else ["--network", "none"]
    full += [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
    full += [x for p in paths for x in ("-v", volume(p, p, "shared" if p in shared else "run"))]
    return [*full, image, *cmd]


def run_in_container(
    image: str,
    cmd: Sequence[str],
    cwd: Path,
    log: Path,
    timeout: int,
    mounts: Sequence[Path] = (),
    network: bool = False,
    env: dict[str, str] | None = None,
    cwd_shared: bool = False,
) -> tuple[int | None, float]:
    """명령 하나를 자식 컨테이너에서 (`run_logged` 와 같은 반환). 네트워크는 의존성 설치처럼 생성 코드가 없을 때만 켠다."""
    name = f"vf-{uuid.uuid4().hex[:12]}"
    code, seconds = run_logged(
        container_argv(image, cmd, cwd, name, mounts, network, env, cwd_shared), cwd, log, timeout
    )
    if code is None:  # 시간 초과 — 클라이언트만 끊겨 컨테이너가 남지 않게
        subprocess.run(["podman", "rm", "-f", name], capture_output=True, check=False)
    return code, seconds
