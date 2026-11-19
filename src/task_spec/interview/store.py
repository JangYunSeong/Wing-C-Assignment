"""인터뷰 저장소 — 판본(revision) 단위 낙관적 갱신. 포크 interview_store.py 에서 D1 부분만 옮김 (#262).

- state: 가명화한 글만 담은 InterviewState (결정 4).
- ui_drafts: 화면의 단계별 임시 저장. 담당자가 친 원문이므로 최대 하루만 두고, 그 단계를 제출하면 바로 지운다
  (결정 4 [확정: 2026-11-19 사용자]). 기준 시각은 지금 남아 있는 임시 저장을 **처음 쓴 때**(drafts_started_at) —
  다른 단계를 저장해도 연장되지 않고, 하루가 지난 뒤 저장하면 남은 것을 버리고 새로 시작한다 (privacy-auditor M1).
  draft_revision 은 임시 저장끼리의 충돌 검사용. 임시 저장은 30일 방치 판정(updated_at)의 활동으로 세지 않는다 —
  열어 둔 화면의 자동 저장이 대응표를 무기한 살리지 않게 (M4).
- 승인 기록(evaluation_spec)·성공 기준 추적은 D2·D3.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from task_spec.schemas.interview_state import InterviewState

DDL = """
CREATE TABLE IF NOT EXISTS public.recruiter_interview (
    id text PRIMARY KEY,
    revision integer NOT NULL DEFAULT 1,
    state jsonb NOT NULL,
    ui_drafts jsonb NOT NULL DEFAULT '{}',
    draft_revision integer NOT NULL DEFAULT 0,
    drafts_started_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS recruiter_interview_updated ON public.recruiter_interview (updated_at);
"""
CLOSED_STAGES = ("discarded",)
# 상태 저장 — 제출한 단계의 임시 저장을 함께 지운다 (지운 뒤 비면 drafts_started_at 도 비운다)
_SAVE = (
    "UPDATE public.recruiter_interview SET state = %(state)s, revision = revision + 1, "
    "ui_drafts = ui_drafts - %(steps)s::text[], draft_revision = draft_revision + %(bump)s, "
    "updated_at = now(), drafts_started_at = CASE WHEN (ui_drafts - %(steps)s::text[]) = '{}'::jsonb "
    "THEN NULL ELSE drafts_started_at END WHERE id = %(id)s AND revision = %(revision)s"
)
_SAVE_CLEAR_ALL = (
    "UPDATE public.recruiter_interview SET state = %(state)s, revision = revision + 1, "
    "ui_drafts = '{}'::jsonb, draft_revision = draft_revision + %(bump)s, updated_at = now(), "
    "drafts_started_at = NULL WHERE id = %(id)s AND revision = %(revision)s"
)
# 임시 저장 — 처음 쓴 때가 하루(cutoff)를 넘었으면 남은 것을 버리고 새로 시작, 아니면 합치고 시각은 그대로
_SAVE_DRAFTS = (
    "UPDATE public.recruiter_interview SET "
    "ui_drafts = CASE WHEN drafts_started_at IS NULL OR drafts_started_at < %(cutoff)s "
    "THEN %(steps)s ELSE ui_drafts || %(steps)s END, "
    "drafts_started_at = CASE WHEN drafts_started_at IS NULL OR drafts_started_at < %(cutoff)s "
    "THEN now() ELSE drafts_started_at END, draft_revision = draft_revision + 1 "
    "WHERE id = %(id)s AND draft_revision = %(draft_revision)s AND NOT (state->>'stage' = ANY(%(closed)s))"
)


@dataclass(frozen=True)
class InterviewRow:
    id: str
    revision: int
    state: InterviewState
    ui_drafts: dict[str, Any]
    draft_revision: int
    drafts_started_at: datetime | None
    updated_at: datetime


class InterviewRepo(Protocol):
    async def create(self, interview_id: str, state: InterviewState) -> InterviewRow: ...

    async def get(self, interview_id: str) -> InterviewRow | None: ...

    async def save(
        self,
        interview_id: str,
        revision: int,
        state: InterviewState,
        *,
        clear_steps: Sequence[str] = (),
        clear_all_drafts: bool = False,
    ) -> InterviewRow | None:
        """revision 이 맞을 때만 저장하고 revision+1. 제출한 단계의 임시 저장(clear_steps)을 함께 지운다."""
        ...

    async def save_drafts(
        self, interview_id: str, draft_revision: int, steps: dict[str, Any], *, cutoff: datetime
    ) -> InterviewRow | None:
        """cutoff(지금 - 보관 기간)보다 먼저 쓰기 시작한 임시 저장은 버리고 새로 시작한다."""
        ...

    async def purge_drafts(self, before: datetime) -> int:
        """처음 쓴 때가 before 보다 오래된 원문 임시 저장을 지운다."""
        ...

    async def stale(self, before: datetime) -> list[str]:
        """닫히지 않았고 마지막 수정이 before 보다 오래된 인터뷰 id."""
        ...

    async def close(self) -> None: ...


def _row(r: dict[str, Any]) -> InterviewRow:
    return InterviewRow(
        id=r["id"],
        revision=r["revision"],
        state=InterviewState.model_validate(r["state"]),
        ui_drafts=r["ui_drafts"] or {},
        draft_revision=r["draft_revision"],
        drafts_started_at=r["drafts_started_at"],
        updated_at=r["updated_at"],
    )


class PgInterviewStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, conninfo: str) -> PgInterviewStore:
        pool = AsyncConnectionPool(
            conninfo, min_size=1, max_size=4, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True)
        async with pool.connection() as conn:
            await conn.execute(DDL)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def create(self, interview_id: str, state: InterviewState) -> InterviewRow:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO public.recruiter_interview (id, state) VALUES (%s, %s)",
                (interview_id, Jsonb(state.model_dump(mode="json"))),
            )
        row = await self.get(interview_id)
        assert row is not None
        return row

    async def get(self, interview_id: str) -> InterviewRow | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, revision, state, ui_drafts, draft_revision, drafts_started_at, updated_at "
                "FROM public.recruiter_interview WHERE id = %s",
                (interview_id,),
            )
            r = await cur.fetchone()
        return _row(r) if r else None

    async def save(
        self,
        interview_id: str,
        revision: int,
        state: InterviewState,
        *,
        clear_steps: Sequence[str] = (),
        clear_all_drafts: bool = False,
    ) -> InterviewRow | None:
        params = {
            "state": Jsonb(state.model_dump(mode="json")),
            "steps": list(clear_steps),
            "bump": 1 if (clear_steps or clear_all_drafts) else 0,
            "id": interview_id,
            "revision": revision,
        }
        async with self._pool.connection() as conn:
            if clear_all_drafts:
                cur = await conn.execute(_SAVE_CLEAR_ALL, params)
            else:
                cur = await conn.execute(_SAVE, params)
            if cur.rowcount != 1:
                return None
        return await self.get(interview_id)

    async def save_drafts(
        self, interview_id: str, draft_revision: int, steps: dict[str, Any], *, cutoff: datetime
    ) -> InterviewRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _SAVE_DRAFTS,
                {
                    "steps": Jsonb(steps),
                    "cutoff": cutoff,
                    "id": interview_id,
                    "draft_revision": draft_revision,
                    "closed": list(CLOSED_STAGES),
                },
            )
            if cur.rowcount != 1:
                return None
        return await self.get(interview_id)

    async def purge_drafts(self, before: datetime) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE public.recruiter_interview SET ui_drafts = '{}'::jsonb, drafts_started_at = NULL, "
                "draft_revision = draft_revision + 1 WHERE drafts_started_at < %s",
                (before,),
            )
            return cur.rowcount

    async def stale(self, before: datetime) -> list[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM public.recruiter_interview "
                "WHERE updated_at < %s AND NOT (state->>'stage' = ANY(%s)) ORDER BY id",
                (before, list(CLOSED_STAGES)),
            )
            return [r[0] for r in await cur.fetchall()]


class MemoryInterviewStore:
    """테스트·단독 실행용 (PgInterviewStore 와 같은 규칙)."""

    def __init__(self, now: Any = None) -> None:
        self.rows: dict[str, InterviewRow] = {}
        self._now = now or (lambda: datetime.now(UTC))

    async def close(self) -> None:
        return None

    async def create(self, interview_id: str, state: InterviewState) -> InterviewRow:
        if interview_id in self.rows:
            raise ValueError("이미 있는 인터뷰 id")
        row = InterviewRow(interview_id, 1, state.model_copy(deep=True), {}, 0, None, self._now())
        self.rows[interview_id] = row
        return row

    async def get(self, interview_id: str) -> InterviewRow | None:
        row = self.rows.get(interview_id)
        return (
            replace(
                row, state=row.state.model_copy(deep=True), ui_drafts=copy.deepcopy(row.ui_drafts)
            )
            if row
            else None
        )

    async def save(
        self,
        interview_id: str,
        revision: int,
        state: InterviewState,
        *,
        clear_steps: Sequence[str] = (),
        clear_all_drafts: bool = False,
    ) -> InterviewRow | None:
        row = self.rows.get(interview_id)
        if row is None or row.revision != revision:
            return None
        drafts = (
            {}
            if clear_all_drafts
            else {k: v for k, v in row.ui_drafts.items() if k not in clear_steps}
        )
        bump = 1 if (clear_steps or clear_all_drafts) else 0
        self.rows[interview_id] = replace(
            row,
            revision=revision + 1,
            state=state.model_copy(deep=True),
            ui_drafts=drafts,
            draft_revision=row.draft_revision + bump,
            drafts_started_at=row.drafts_started_at if drafts else None,
            updated_at=self._now(),
        )
        return await self.get(interview_id)

    async def save_drafts(
        self, interview_id: str, draft_revision: int, steps: dict[str, Any], *, cutoff: datetime
    ) -> InterviewRow | None:
        row = self.rows.get(interview_id)
        if row is None or row.draft_revision != draft_revision or row.state.stage in CLOSED_STAGES:
            return None
        fresh = row.drafts_started_at is None or row.drafts_started_at < cutoff
        self.rows[interview_id] = replace(
            row,
            ui_drafts={**({} if fresh else row.ui_drafts), **copy.deepcopy(steps)},
            draft_revision=draft_revision + 1,
            drafts_started_at=self._now() if fresh else row.drafts_started_at,
        )
        return await self.get(interview_id)

    async def purge_drafts(self, before: datetime) -> int:
        n = 0
        for key, row in list(self.rows.items()):
            if row.drafts_started_at is not None and row.drafts_started_at < before:
                self.rows[key] = replace(
                    row, ui_drafts={}, drafts_started_at=None, draft_revision=row.draft_revision + 1
                )
                n += 1
        return n

    async def stale(self, before: datetime) -> list[str]:
        return sorted(
            k
            for k, r in self.rows.items()
            if r.updated_at < before and r.state.stage not in CLOSED_STAGES
        )
