"""/healthz — 프로세스·DB·온톨로지 버전·LLM 키 설정 여부(bool). 키 값은 노출하지 않는다."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from task_spec.api.deps import GraphDep, SettingsDep
from task_spec.api.schemas import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


async def _ping(saver: AsyncPostgresSaver) -> bool:
    try:
        conn = saver.conn
        if isinstance(conn, AsyncConnectionPool):
            async with conn.connection() as c:
                await c.execute("SELECT 1")
        else:
            await conn.execute("SELECT 1")
    except Exception as exc:
        logger.error("healthz db ping failed exc=%s", type(exc).__name__)
        return False
    return True


@router.get("/healthz")
async def healthz(request: Request, graph: GraphDep, settings: SettingsDep) -> JSONResponse:
    saver = graph.checkpointer
    if isinstance(saver, AsyncPostgresSaver):
        database = "ok" if await _ping(saver) else "error"
    else:
        database = "not_applicable"
    body = HealthResponse(
        status="ok" if database != "error" else "degraded",
        database=database,
        ontology_version=settings.ontology_version,
        ontology_resolved=(
            o.version.version if (o := getattr(request.app.state, "ontology", None)) else None
        ),
        llm_keys={
            "openai": bool(settings.openai_api_key.get_secret_value()),
            "opencode": bool(settings.opencode_api_key.get_secret_value()),
        },
    )
    return JSONResponse(status_code=200 if database != "error" else 503, content=body.model_dump())
