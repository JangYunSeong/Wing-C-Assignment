"""체크포인터 팩토리 (pipeline-graph 스킬 "체크포인터").

체크포인트에는 State 가 들어간다 = 지원자 파생물. State 에 원문·매핑을 넣지 않는 규칙이 여기서 중요하다.
역직렬화는 이 프로젝트 스키마 타입만 허용한다 (LANGGRAPH_STRICT_MSGPACK 를 켜도 동작하도록).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from task_spec import schemas
from task_spec.config import Settings


def make_serializer() -> JsonPlusSerializer:
    allowed = [
        getattr(schemas, name)
        for name in schemas.__all__
        if isinstance(getattr(schemas, name), type)
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


@asynccontextmanager
async def make_checkpointer(
    settings: Settings | None = None,
    *,
    kind: str | None = None,
    conn_string: str | None = None,
) -> AsyncIterator[BaseCheckpointSaver]:
    """settings 기준으로 만들되, 테스트는 kind/conn_string 으로 덮어쓸 수 있다."""
    kind = kind or (settings.checkpointer if settings else "memory")
    serde = make_serializer()
    if kind == "memory":
        yield InMemorySaver(serde=serde)
        return
    if conn_string is None:
        if settings is None:
            raise ValueError("postgres 체크포인터에는 settings 또는 conn_string 이 필요합니다")
        conn_string = settings.database_url.get_secret_value()
    # 연결 하나(from_conn_string)가 아니라 풀: SSE 연결이 끊기면 sse-starlette 가 anyio 취소 범위로 조회를 끊는데,
    # 그때 psycopg 의 정리 대기까지 취소돼 연결이 "another command is already in progress" 로 남는다
    # (2026-11-05 운영 — 재시작 전까지 실행 조회·헬스체크 전부 실패). 풀은 그런 연결을 돌려받을 때 버린다.
    pool = AsyncConnectionPool(
        conn_string,
        connection_class=AsyncConnection[DictRow],
        min_size=1,
        max_size=4,
        open=False,
        # from_conn_string 과 같은 연결 설정 (AsyncPostgresSaver 요구 사항)
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=AsyncConnectionPool.check_connection,
    )
    await pool.open(wait=True)
    try:
        saver = AsyncPostgresSaver(pool, serde=serde)
        await saver.setup()  # 체크포인트 테이블 생성 (멱등)
        yield saver
    finally:
        await pool.close()
