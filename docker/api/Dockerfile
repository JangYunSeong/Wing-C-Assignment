# P1 과제 명세 API — FastAPI + LangGraph (deployment 스킬)
#
# 넣는 것: src/ (설치된 패키지로), prompts/, incidents/   — 넣지 않는 것: data/, .env, tests/, eval/ (.containerignore)
# 비밀값은 이미지에 넣지 않는다 — 실행 때 --env-file 로 주입.
#
# 빌드: podman build -f docker/api/Dockerfile -t task-api:<버전> .   (레포 루트에서)

# ---------------------------------------------------------------- build
FROM docker.io/library/python:3.11-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM docker.io/library/python:3.11-slim
# 스캔 PDF OCR (nodes/ingest/extractors.py — tesseract 직접 호출, kor+eng)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown app:app /data && chmod 700 /data

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY prompts ./prompts
COPY incidents ./incidents

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PROMPTS_DIR=/app/prompts \
    INCIDENTS_DIR=/app/incidents/public_v1 \
    DATA_DIR=/data \
    ONTOLOGY_ARTIFACT_DIR=/data/ontology \
    CHECKPOINTER=postgres

# 지원자 원본·가명화본·실행 결과 (불변식 1 — 볼륨, 권한 700)
VOLUME ["/data"]
USER 10001
EXPOSE 8000
# 실행 상태·SSE 를 프로세스 안에서 관리한다 (api/launcher.py) → 워커 1개
CMD ["uvicorn", "task_spec.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
