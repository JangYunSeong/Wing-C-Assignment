# 과제 생성 builder (#129) — 큐에서 생성 요청을 가져가 공통 과제 하나를 review 까지 (deployment 스킬, docs/deploy.md 2-8)
#
# 넣는 것: src/ (설치된 패키지로), prompts/, incidents/, templates/, podman-remote(호스트 podman 소켓으로 부른다)·zstd
# 넣지 않는 것: JDK·Gradle·Node·uv·가상 환경 — 생성 코드 검증은 호스트 podman 의 검증용 템플릿 층 위 --network none 자식 컨테이너 (#127)
#               data/, .env, tests/, eval/ (.containerignore). 비밀값은 실행 때 --env-file 로.
# 빌드: podman build -f docker/builder/Dockerfile -t task-builder:<버전> .   (레포 루트에서)

# ---------------------------------------------------------------- build
FROM docker.io/library/python:3.11-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime
# trixie: podman-remote 5.x (호스트 podman 5.x 와 같은 계열 — 소켓 API 호환), Python 3.11
FROM docker.io/library/python:3.11-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends podman-remote zstd ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s /usr/bin/podman-remote /usr/local/bin/podman

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY prompts ./prompts
COPY incidents ./incidents
COPY templates ./templates

# CONTAINER_HOST: 호스트 사용자의 rootless podman 소켓을 run.sh 가 이 경로에 붙인다
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PROMPTS_DIR=/app/prompts \
    INCIDENTS_DIR=/app/incidents/public_v1 \
    TEMPLATES_DIR=/app/templates \
    CONTAINER_HOST=unix:///run/podman/podman.sock

CMD ["python", "-m", "task_spec.builder"]
