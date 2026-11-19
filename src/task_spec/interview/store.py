"""인터뷰 저장소 — 판본(revision) 단위 낙관적 갱신. 포크 interview_store.py 에서 D1 부분만 옮김 (#262).

- state: 가명화한 글만 담은 InterviewState (결정 4).
- ui_drafts: 화면의 단계별 임시 저장. 담당자가 친 원문이므로 최대 하루만 두고, 그 단계를 제출하면 바로 지운다
  (결정 4 [확정: 2026-11-19 사용자]). 기준 시각은 지금 남아 있는 임시 저장을 **처음 쓴 때**(drafts_started_at) —
  다른 단계를 저장해도 연장되지 않고, 하루가 지난 뒤 저장하면 남은 것을 버리고 새로 시작한다 (privacy-auditor M1).
  draft_revision 은 임시 저장끼리의 충돌 검사용. 임시 저장은 30일 방치 판정(updated_at)의 활동으로 세지 않는다 —
  열어 둔 화면의 자동 저장이 대응표를 무기한 살리지 않게 (M4).
- 승인(D2b)은 인터뷰 판본 확정·회사 사례 등록·공통 과제 승인을 한 트랜잭션으로 — interview/approval.py.
  평가팀 전달 기록(evaluation_spec, 결정 3)·성공 기준 추적은 D3.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from task_spec.postings.company import company_id
from task_spec.postings.search import COMPANY_NOISE
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
# 닫힌 단계 — 더 고치지 않고 대응표가 없다 (승인·폐기, 결정 4). 임시 저장·방치 정리 대상이 아니다
CLOSED_STAGES = ("discarded", "approved")
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


async def save_in(
    conn: AsyncConnection[Any], interview_id: str, revision: int, state: InterviewState
) -> bool:
    """닫는 저장 — 호출하는 쪽 트랜잭션 안에서 (승인: 회사 사례 등록·과제 승인과 함께, interview/approval.py).
    판본이 맞을 때만 상태를 바꾸고 revision+1, 원문 임시 저장을 모두 지운다."""
    cur = await conn.execute(
        _SAVE_CLEAR_ALL,
        {
            "state": Jsonb(state.model_dump(mode="json")),
            "bump": 1,
            "id": interview_id,
            "revision": revision,
        },
    )
    return cur.rowcount == 1


@dataclass(frozen=True)
class InterviewRow:
    id: str
    revision: int
    state: InterviewState
    ui_drafts: dict[str, Any]
    draft_revision: int
    drafts_started_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True)
class InterviewSummary:
    """목록 한 줄 — 공고 메타데이터·단계·시각만. 대화·사례·답안은 읽지 않는다 (목록 SQL 이 state 의 이 칸만 꺼낸다).
    stage 는 읽은 때의 단계 — 닫히지 않은 채 방치 기간(stale_before)을 넘었으면 정리 작업 전이라도 discarded."""

    id: str
    revision: int
    stage: str
    company: str
    title: str
    posting_id: str
    created_at: datetime
    updated_at: datetime
    reviewer: str | None
    approved_at: str | None
    discarded_at: str | None
    discard_reason: str | None
    stale: bool  # 읽을 때 방치로 본 것 (아직 정리 작업이 닫지 않음)


# 목록 — 최근 진행 순. 회사는 postings/company.company_id 와 같은 규칙(법인 표기·공백 빼고 소문자)으로 맞춘다
_LIST = (
    "SELECT id, revision, created_at, updated_at, stage, stale, company, title, posting_id, reviewer, approved_at, "
    "discarded_at, discard_reason FROM (SELECT id, revision, created_at, updated_at, "
    "state->>'company' AS company, state->>'title' AS title, state->>'posting_id' AS posting_id, "
    "state->'approval'->>'reviewer' AS reviewer, state->'approval'->>'at' AS approved_at, "
    "state->>'discarded_at' AS discarded_at, state->>'discard_reason' AS discard_reason, "
    "(NOT (state->>'stage' = ANY(%(closed)s)) AND updated_at < %(stale_before)s) AS stale, "
    "CASE WHEN NOT (state->>'stage' = ANY(%(closed)s)) AND updated_at < %(stale_before)s THEN 'discarded' "
    "ELSE state->>'stage' END AS stage FROM public.recruiter_interview "
    "WHERE (%(company)s::text IS NULL OR lower(regexp_replace(coalesce(state->>'company', ''), %(noise)s, '', 'g')) "
    "= %(company)s)) AS t WHERE (%(stages)s::text[] IS NULL OR stage = ANY(%(stages)s)) "
    "ORDER BY updated_at DESC, id LIMIT %(limit)s"
)


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

    async def summaries(
        self,
        *,
        company: str | None,
        stages: Sequence[str] | None,
        stale_before: datetime,
        limit: int,
    ) -> list[InterviewSummary]:
        """최근 진행 순 목록. company 는 회사 id(postings/company.company_id), stages 는 읽은 때의 단계로 거른다."""
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

    @property
    def pool(self) -> AsyncConnectionPool:
        """승인 확정(PgApprovalCommitter)이 같은 연결 풀에서 세 표를 한 트랜잭션으로 묶는다."""
        return self._pool

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

    async def summaries(
        self,
        *,
        company: str | None,
        stages: Sequence[str] | None,
        stale_before: datetime,
        limit: int,
    ) -> list[InterviewSummary]:
        params = {
            "closed": list(CLOSED_STAGES),
            "stale_before": stale_before,
            "company": company,
            "noise": COMPANY_NOISE,
            "stages": list(stages) if stages is not None else None,
            "limit": limit,
        }
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_LIST, params)
            return [InterviewSummary(**r) for r in await cur.fetchall()]


class MemoryInterviewStore:
    """테스트·단독 실행용 (PgInterviewStore 와 같은 규칙)."""

    def __init__(self, now: Any = None) -> None:
        self.rows: dict[str, InterviewRow] = {}
        self.created: dict[str, datetime] = {}
        self._now = now or (lambda: datetime.now(UTC))

    async def close(self) -> None:
        return None

    async def create(self, interview_id: str, state: InterviewState) -> InterviewRow:
        if interview_id in self.rows:
            raise ValueError("이미 있는 인터뷰 id")
        row = InterviewRow(interview_id, 1, state.model_copy(deep=True), {}, 0, None, self._now())
        self.rows[interview_id] = row
        self.created[interview_id] = row.updated_at
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

    async def summaries(
        self,
        *,
        company: str | None,
        stages: Sequence[str] | None,
        stale_before: datetime,
        limit: int,
    ) -> list[InterviewSummary]:
        out: list[InterviewSummary] = []
        for r in sorted(self.rows.values(), key=lambda r: (-r.updated_at.timestamp(), r.id)):
            s = r.state
            if company is not None and company_id(s.company) != company:
                continue
            stale = s.stage not in CLOSED_STAGES and r.updated_at < stale_before
            stage = "discarded" if stale else s.stage
            if stages is not None and stage not in stages:
                continue
            out.append(
                InterviewSummary(
                    id=r.id,
                    revision=r.revision,
                    stage=stage,
                    company=s.company,
                    title=s.title,
                    posting_id=s.posting_id,
                    created_at=self.created[r.id],
                    updated_at=r.updated_at,
                    reviewer=s.approval.reviewer if s.approval else None,
                    approved_at=s.approval.at if s.approval else None,
                    discarded_at=s.discarded_at,
                    discard_reason=s.discard_reason,
                    stale=stale,
                )
            )
        return out[:limit]
