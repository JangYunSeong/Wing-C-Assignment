"""온톨로지 읽기 전용 접근 (CLAUDE.md 불변식 4).

- 버전 고정: `Settings.ontology_version` 과 같은 버전을 `public.ontology_registry` 에서 찾는다.
  `dev` 는 개발용 별칭 — 가장 최근에 적재한 버전 (실제 버전 문자열을 명세에 적는다).
- 모든 연결은 `default_transaction_read_only=on` + 해당 버전 스키마 search_path. 쓰기는 DB 가 거부한다.
- 적재는 load_pg.py 만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, LiteralString

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DEV_ALIAS = "dev"


class OntologyNotLoaded(RuntimeError):
    """설정한 버전이 적재되어 있지 않다 — 앱 기동 실패 사유."""


@dataclass(frozen=True)
class OntologyVersion:
    requested: str
    version: str  # 실제 버전 문자열 (명세 ontology_version 에 적는 값)
    schema: str
    source_sha256: str


async def resolve_version(conninfo: str, requested: str) -> OntologyVersion:
    async with (
        await AsyncConnection.connect(conninfo, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT to_regclass('public.ontology_registry') IS NOT NULL")
        row = await cur.fetchone()
        if not row or not row[0]:
            raise OntologyNotLoaded("ontology_registry 가 없습니다 — load_pg 로 적재하세요")
        if requested == DEV_ALIAS:
            await cur.execute(
                "SELECT version, schema_name, source_sha256 FROM public.ontology_registry ORDER BY loaded_at DESC LIMIT 1"
            )
        else:
            await cur.execute(
                "SELECT version, schema_name, source_sha256 FROM public.ontology_registry WHERE version = %s",
                (requested,),
            )
        row = await cur.fetchone()
    if row is None:
        raise OntologyNotLoaded(f"적재되지 않은 온톨로지 버전: {requested}")
    return OntologyVersion(requested=requested, version=row[0], schema=row[1], source_sha256=row[2])


class OntologyRepository:
    """버전 하나에 묶인 읽기 전용 조회기. `async with OntologyRepository.open(...)` 로 쓴다."""

    def __init__(self, pool: AsyncConnectionPool, version: OntologyVersion) -> None:
        self._pool = pool
        self.version = version

    @classmethod
    async def open(cls, conninfo: str, requested: str, *, max_size: int = 4) -> OntologyRepository:
        ver = await resolve_version(conninfo, requested)

        async def configure(conn: AsyncConnection) -> None:
            # 자동 커밋에선 set_read_only 가 적용되지 않는다 — 세션 기본값으로 강제 (문장 단위 트랜잭션에도 적용)
            await conn.set_autocommit(True)
            await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(ver.schema)))
            await conn.execute("SET default_transaction_read_only = on")

        pool = AsyncConnectionPool(
            conninfo, min_size=1, max_size=max_size, configure=configure, open=False
        )
        await pool.open(wait=True)
        return cls(pool, ver)

    async def close(self) -> None:
        await self._pool.close()

    async def __aenter__(self) -> OntologyRepository:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def fetch(
        self, query: LiteralString, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        """고정 SQL 만 (LiteralString) — 사용자 입력은 params 로만."""
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return await cur.fetchall()

    async def built_info(self) -> dict[str, Any]:
        rows = await self.fetch(
            "SELECT version, git_commit, train_version, built_at FROM ontology_version"
        )
        return rows[0] if rows else {}
