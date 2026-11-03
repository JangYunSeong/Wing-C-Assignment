"""공고 단위 공통 과제 저장·재사용 (task_db public.common_task) [확정: 2026-11-03].

같은 공고 지원자는 같은 공통 과제를 받아야 한다 (context.md §3 비교 가능성).
키 = (posting_id, ontology_version, prompt_ref) — prompt_ref 는 draft_task@N, LLM 없으면 'skeleton'.
- 재사용: 승인된 최신 판 → 없으면 최신 초안. 지원자 데이터는 담지 않는다 (공고·온톨로지에서 만든 과제만).
  다른 지원자에게 재사용되므로 지원자 실행과 연결되는 값(run_id 등)도 저장하지 않는다 (privacy-auditor W3).
- revise: 같은 키에 새 판(revision+1)을 초안으로 저장.
- 승인(finalize): 그 판을 approved 로, 같은 키의 이전 approved 는 superseded 로.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from task_spec.schemas import Provenance, TaskItem

CommonStatus = Literal["draft", "approved", "superseded"]

DDL = """
CREATE TABLE IF NOT EXISTS public.common_task (
    id text PRIMARY KEY,
    posting_id text NOT NULL,
    ontology_version text NOT NULL,
    prompt_ref text NOT NULL,
    revision integer NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'superseded')),
    task jsonb NOT NULL,
    provenance jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    approved_by text,
    approved_at timestamptz,
    UNIQUE (posting_id, ontology_version, prompt_ref, revision)
);
-- 초기 개발판(2026-11-03)에 있던 지원자 실행 연결 칸 제거 (privacy-auditor W3)
ALTER TABLE public.common_task DROP COLUMN IF EXISTS created_by_run;
"""


@dataclass(frozen=True)
class CommonKey:
    posting_id: str
    ontology_version: str
    prompt_ref: str


class StoredCommon(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    revision: int = Field(ge=1)
    status: CommonStatus
    task: TaskItem
    provenance: list[Provenance] = Field(default_factory=list)
    approved_by: str | None = None


class CommonTaskRepo(Protocol):
    async def reusable(self, key: CommonKey) -> StoredCommon | None: ...

    async def save_draft(
        self, key: CommonKey, task: TaskItem, provenance: list[Provenance]
    ) -> StoredCommon: ...

    async def approve(self, common_id: str, reviewer: str, at: datetime) -> None: ...


def _new_id() -> str:
    return f"ct-{uuid.uuid4().hex[:12]}"


def _with_id(task: TaskItem, common_id: str) -> TaskItem:
    # 지원자마다 같은 과제임을 id 로도 드러낸다 (실행 id 가 아니라 공통 과제 id). 슬롯도 새 id 를 가리키게
    slots = [x.model_copy(update={"parent_task_id": common_id}) for x in task.slots]
    return task.model_copy(update={"task_id": common_id, "slots": slots})


class InMemoryCommonStore:
    """단위 테스트·DB 없는 그래프용."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def reusable(self, key: CommonKey) -> StoredCommon | None:
        rows = [r for r in self.rows if r["key"] == key and r["status"] != "superseded"]
        rows.sort(key=lambda r: (r["status"] == "approved", r["revision"]), reverse=True)
        return StoredCommon(**{k: v for k, v in rows[0].items() if k != "key"}) if rows else None

    async def save_draft(
        self, key: CommonKey, task: TaskItem, provenance: list[Provenance]
    ) -> StoredCommon:
        rev = 1 + max((r["revision"] for r in self.rows if r["key"] == key), default=0)
        cid = _new_id()
        row = {
            "key": key,
            "id": cid,
            "revision": rev,
            "status": "draft",
            "task": _with_id(task, cid),
            "provenance": list(provenance),
            "approved_by": None,
        }
        self.rows.append(row)
        return StoredCommon(**{k: v for k, v in row.items() if k != "key"})

    async def approve(self, common_id: str, reviewer: str, at: datetime) -> None:
        row = next((r for r in self.rows if r["id"] == common_id), None)
        if row is None:
            raise KeyError(common_id)
        for r in self.rows:
            if r["key"] == row["key"] and r["status"] == "approved" and r is not row:
                r["status"] = "superseded"
        row["status"], row["approved_by"] = "approved", reviewer


class CommonTaskStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, conninfo: str) -> CommonTaskStore:
        pool = AsyncConnectionPool(
            conninfo, min_size=1, max_size=4, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True)
        async with pool.connection() as conn:
            await conn.execute(DDL)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    @staticmethod
    def _row(r: dict[str, Any]) -> StoredCommon:
        return StoredCommon(
            id=r["id"],
            revision=r["revision"],
            status=r["status"],
            task=TaskItem.model_validate(r["task"]),
            provenance=[Provenance.model_validate(p) for p in r["provenance"]],
            approved_by=r["approved_by"],
        )

    async def reusable(self, key: CommonKey) -> StoredCommon | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.common_task WHERE posting_id = %s AND ontology_version = %s "
                "AND prompt_ref = %s AND status <> 'superseded' "
                "ORDER BY (status = 'approved') DESC, revision DESC LIMIT 1",
                (key.posting_id, key.ontology_version, key.prompt_ref),
            )
            r = await cur.fetchone()
        return self._row(r) if r else None

    async def save_draft(
        self, key: CommonKey, task: TaskItem, provenance: list[Provenance]
    ) -> StoredCommon:
        cid = _new_id()
        task = _with_id(task, cid)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 같은 키 동시 저장: UNIQUE 위반이면 다시 번호를 매긴다 (드묾 — 3회까지)
            for _ in range(3):
                try:
                    await cur.execute(
                        "INSERT INTO public.common_task (id, posting_id, ontology_version, prompt_ref, revision, "
                        "task, provenance) "
                        "SELECT %s, %s, %s, %s, COALESCE(MAX(revision), 0) + 1, %s, %s "
                        "FROM public.common_task WHERE posting_id = %s AND ontology_version = %s AND prompt_ref = %s "
                        "RETURNING *",
                        (
                            cid,
                            key.posting_id,
                            key.ontology_version,
                            key.prompt_ref,
                            Jsonb(task.model_dump(mode="json")),
                            Jsonb([p.model_dump(mode="json", by_alias=True) for p in provenance]),
                            key.posting_id,
                            key.ontology_version,
                            key.prompt_ref,
                        ),
                    )
                    r = await cur.fetchone()
                    assert r is not None
                    return self._row(r)
                except Exception as exc:  # psycopg.errors.UniqueViolation
                    if type(exc).__name__ != "UniqueViolation":
                        raise
        raise RuntimeError("common_task: 판 번호 충돌이 계속됩니다")

    async def approve(self, common_id: str, reviewer: str, at: datetime) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT posting_id, ontology_version, prompt_ref FROM public.common_task WHERE id = %s",
                (common_id,),
            )
            r = await cur.fetchone()
            if r is None:
                raise KeyError(common_id)
            await conn.execute(
                "UPDATE public.common_task SET status = 'superseded' "
                "WHERE posting_id = %s AND ontology_version = %s AND prompt_ref = %s "
                "AND status = 'approved' AND id <> %s",
                (*r, common_id),
            )
            await conn.execute(
                "UPDATE public.common_task SET status = 'approved', approved_by = %s, approved_at = %s "
                "WHERE id = %s",
                (reviewer, at, common_id),
            )
