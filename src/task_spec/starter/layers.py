"""런타임 층 — 스택(Java·Node·Python)마다 기반 이미지·오프라인 의존성 설치·grade 스크립트, podman 호출, 검증용 템플릿 층 (#127).

이미지 만들기(`starter/images.py`)는 이 층 위에 응시용·채점용 과제 층을 얹고, 검증기(`starter/verify/`)는 **같은 템플릿 층**
(`ensure_verify_layer`) 위에서 생성 코드를 `--network none` 으로 돌린다 — 검증 환경이 채점 환경과 같고, builder(#129) 에
JDK·Gradle·uv 를 따로 넣지 않아도 된다. 이 모듈은 `starter/verify` 를 import 하지 않는다 (검증기가 이것을 쓴다).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from task_spec.starter.grade import image_grade, image_grade_node, image_grade_py
from task_spec.starter.templates import Template, is_java, is_python

# 기반 이미지 — 응시용·채점용 이미지와 검증 컨테이너가 같은 것을 쓴다
JDK_IMAGE = "docker.io/library/eclipse-temurin:17-jdk-jammy"
NODE_IMAGE = "docker.io/library/node:22-bookworm-slim"
PY_IMAGE = "docker.io/library/python:3.11-slim-bookworm"

PYTHON3 = "RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*"


@dataclass(frozen=True)
class Stack:
    """스택마다 다른 것 — 런타임 이미지·의존성 설치·grade 스크립트·응시용 오프라인 확인 명령. 층 구조·검증 순서는 같다."""

    base: str
    install: tuple[str, ...]  # 템플릿 층 — WORKDIR(작업 경로)에 project/ 를 복사한 뒤
    grade_script: Path
    candidate_check: tuple[
        str, ...
    ]  # 응시용 이미지에서 네트워크 없이 — 의존성이 이미지 안에 다 있는가
    scan_skip: tuple[str, ...]  # 레이어 검사에서 뺄 런타임 경로 (작업 경로가 아닌 곳)
    grader_setup: tuple[str, ...] = (
        PYTHON3,
    )  # 채점 기반 층 — grade 스크립트를 돌릴 python3 (Python 런타임은 이미 있음)


def _script(module: Any) -> Path:
    return Path(module.__file__)


JAVA = Stack(
    base=JDK_IMAGE,
    install=(
        "ENV GRADLE_USER_HOME=/opt/gradle-home",
        # 뼈대를 한 번 빌드·테스트해 Gradle 배포판·의존성(테스트 런타임 포함)을 캐시에 — 채점·응시 컨테이너는 네트워크 없음
        "RUN chmod +x gradlew && ./gradlew --no-daemon --console=plain build && rm -rf build .gradle",
    ),
    grade_script=_script(image_grade),
    candidate_check=("./gradlew", "--offline", "--no-daemon", "-q", "test"),
    scan_skip=("usr/", "opt/java/", "etc/", "var/", "lib/"),
)
# Debian 12(bookworm) — Playwright 공식 지원 배포판이라 실제 브라우저 층도 같은 계열로 (alpine/musl 은 미지원, 2026-11-09 문서 확인)
NODE = Stack(
    base=NODE_IMAGE,
    install=(
        # lock 그대로 설치하고 뼈대의 타입 검사·테스트를 한 번 — 응시·채점 컨테이너는 네트워크 없음
        "RUN npm ci --no-audit --no-fund && npx tsc --noEmit && npx vitest run && rm -rf node_modules/.vite node_modules/.vitest",
    ),
    grade_script=_script(image_grade_node),
    candidate_check=("sh", "-c", "npx tsc --noEmit && npm test"),
    scan_skip=("usr/", "opt/", "etc/", "var/", "lib/"),
)


# Python 3.11(템플릿 requires-python) Debian 12 slim — uv.lock 그대로 /opt/venv 에 설치, PATH 앞에. 응시용에서 `python -m pytest` 가 그 환경
PY_VENV = "/opt/venv"
UV_VERSION = "0.13.6"  # 개발 서버와 같은 uv (lock 형식이 같게)
PY = Stack(
    base=PY_IMAGE,
    install=(
        f"ENV UV_PROJECT_ENVIRONMENT={PY_VENV} UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PATH={PY_VENV}/bin:$PATH",
        # lock 그대로 설치하고 뼈대 테스트를 한 번 — 응시·채점 컨테이너는 네트워크 없음. 캐시·바이트코드는 지운다
        f"RUN pip install --no-cache-dir uv=={UV_VERSION} && uv sync --frozen --no-install-project && uv cache clean "
        "&& python -m pytest -q -p no:cacheprovider && find . -name __pycache__ -prune -exec rm -rf {} +",
    ),
    grade_script=_script(image_grade_py),
    candidate_check=("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
    scan_skip=("usr/", "opt/", "etc/", "var/", "lib/", "root/"),
    grader_setup=(),
)


def stack_for(t: Template) -> Stack:
    if is_java(t.manifest):
        return JAVA
    return PY if is_python(t.manifest) else NODE


class Podman:
    """전용 저장소로만 podman 을 부른다 (기본 저장소의 이미지·컨테이너는 건드리지 않음)."""

    def __init__(self, tmp: Path, store: Path | None = None) -> None:
        # store 를 주면 그 경로를 podman 저장소로 (SELinux 컨테이너 레이블이 있는 경로여야 컨테이너가 돈다 — 2026-11-08: /ssd 아래
        # 전용 저장소는 unlabeled_t 라 RUN 이 libc 로드에서 막힘). 기본은 시스템 저장소에 이 작업 이름(localhost/task…)으로만 추가.
        self.flags = (
            ["--root", str(store / "root"), "--runroot", str(store / "run")] if store else []
        )
        self.env = os.environ | {"TMPDIR": str(tmp)}  # 빌드 임시 파일은 여유가 큰 쪽에
        tmp.mkdir(parents=True, exist_ok=True)

    def __call__(
        self, *args: str, log: Path | None = None, timeout: int = 3600, check: bool = True
    ) -> str:
        cmd = self.argv(*args)
        if log is None:
            res = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=timeout)
            if check and res.returncode != 0:
                raise RuntimeError(f"{' '.join(args[:3])} 실패: {res.stderr[-2000:]}")
            return res.stdout
        with log.open("a") as fh:
            fh.write(f"\n$ podman {' '.join(args)}\n")
            fh.flush()
            res2 = subprocess.run(
                cmd, env=self.env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout
            )
        if check and res2.returncode != 0:
            raise RuntimeError(f"{' '.join(args[:3])} 실패 (로그 {log})")
        return ""

    def argv(self, *args: str) -> list[str]:
        """이 저장소로 부르는 podman 명령줄."""
        return ["podman", *self.flags, *args]

    def run(self, *args: str, timeout: int) -> subprocess.CompletedProcess[str]:
        """`podman run …` — 결과를 그대로 돌려준다 (실패해도 예외 없음, 검증이 종료 코드를 본다)."""
        return subprocess.run(
            self.argv("run", *args),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def containerfile(base: str, body: list[str]) -> str:
    return "\n".join([f"FROM {base}", *body, ""])


def labels(d: dict[str, Any]) -> list[str]:
    return [f"LABEL {k}={json.dumps(str(v), ensure_ascii=False)}" for k, v in d.items()]


# ── 검증용 템플릿 층 (#127) ────────────────────────────────────────────

_SKIP_DIRS = {
    ".gradle",
    "build",
    "node_modules",
    ".venv",
    "__pycache__",
}  # 호스트 실행이 남긴 것 — 해시에서 뺀다


def project_sha256(t: Template) -> str:
    """템플릿 project/ 내용 해시 — 검증용 층을 다시 만들지 정한다 (호스트 실행이 남긴 캐시 폴더는 뺀다)."""
    h = hashlib.sha256()
    for p in sorted(x for x in t.project.rglob("*") if x.is_file()):
        rel = p.relative_to(t.project)
        if _SKIP_DIRS & set(rel.parts):
            continue
        h.update(str(rel).encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def verify_layer_name(t: Template) -> str:
    stem = t.manifest.ref.replace("@", "-").replace("/", "-")
    return f"localhost/task-verify/{stem}"


def ensure_verify_layer(t: Template, work: Path, pm: Podman | None = None) -> str:
    """검증용 템플릿 층 — 채점 이미지의 템플릿 층과 같은 Containerfile(project/ + 오프라인 의존성). 템플릿 내용이 같으면 있는 것을 쓴다.

    만들 때만 네트워크를 쓴다 (lock 그대로 의존성 설치 — 생성 코드는 아직 없다). work 는 빌드 맥락·로그를 둘 곳."""
    pm = pm or Podman(work / "tmp")
    name = verify_layer_name(t)
    digest = project_sha256(t)
    got = pm(
        "image", "inspect", "--format", '{{index .Labels "project_sha256"}}', name, check=False
    )
    if got.strip() == digest:
        return name
    st = stack_for(t)
    ctx = work / "verify-layers" / name.rsplit("/", 1)[-1]
    if ctx.exists():
        shutil.rmtree(ctx)
    shutil.copytree(t.project, ctx / "project", ignore=shutil.ignore_patterns(*_SKIP_DIRS))
    (ctx / "Containerfile").write_text(
        containerfile(
            st.base,
            [
                f"WORKDIR {t.manifest.workspace}",
                f"COPY project/ {t.manifest.workspace}/",
                *st.install,
                *labels({"template_ref": t.manifest.ref, "project_sha256": digest}),
            ],
        ),
        "utf-8",
    )
    pm("build", "-t", name, str(ctx), log=ctx.parent / f"{ctx.name}.log")
    return name
