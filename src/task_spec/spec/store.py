"""공고 단위 공통 과제 저장·재사용 (task_db public.common_task) [확정: 2026-11-03].

같은 공고 지원자는 같은 공통 과제를 받아야 한다 (context.md §3 비교 가능성).
키 = (posting_id, ontology_version, prompt_ref) — prompt_ref 는 draft_task@N, LLM 없으면 'skeleton'.
- 재사용: 승인된 최신 판 → 없으면 최신 초안. 지원자 데이터는 담지 않는다 (공고·온톨로지에서 만든 과제만).
  다른 지원자에게 재사용되므로 지원자 실행과 연결되는 값(run_id 등)도 저장하지 않는다 (privacy-auditor W3).
- revise: 같은 키에 새 판(revision+1)을 초안으로 저장.
- 승인(finalize): 그 판을 approved 로, 같은 키의 이전 approved 는 superseded 로.
- 유형(task_kind) [2026-11-06]: 그 공고(같은 온톨로지 버전)에서 가장 최근에 승인된 판의 유형을 다음 실행이 따른다
  (spec/kind.py 우선순위 2). 예전 판(유형 필드 없음)은 incident.
- 인터뷰 과제 [확정: 2026-11-19 사용자]: 채용담당자 인터뷰가 만든 과제는 prompt_ref = `interview:{인터뷰 id}`.
  공고(같은 온톨로지 판)마다 승인된 인터뷰 과제는 하나 — 그 공고 지원자 실행은 새로 만들지 않고 이 과제를 쓴다 (nodes/spec.py).
  승인은 채용담당자만(`approve_interview` — 이미 있으면 `replaces` 로 교체를 명시), 지원자 명세 승인은 다시 승인하지 않는다.
  다른 온톨로지 판에만 있으면 그 공고 실행을 멈춘다 (과제 안의 역량·수행준거 id 가 그 판 것 — 불변식 4).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from task_spec.schemas import FramedMaterial, Provenance, TaskItem, TaskKind, TaskSpec

CommonStatus = Literal["draft", "approved", "superseded"]
INTERVIEW_REF_PREFIX = "interview:"  # 채용담당자 인터뷰가 만든 공통 과제의 저장 키 머리
_SUPERSEDED = (
    "교체된 인터뷰 과제는 다시 승인하지 않습니다 — 되돌리려면 새 판으로 저장해 승인하세요: {}"
)

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
-- 함정 틀 (#318 D1 [확정: 2026-11-25 사용자]) — 그 공고의 틀 적용 재료와 틀 (FramedMaterial). 빌드 계약 단계가 있으면 원 재료 대신 쓴다.
-- 공용 회사 사례 재료는 바꾸지 않는다 — 공고마다 자기 틀. 틀 프롬프트가 없거나 실패하면 NULL (원 재료 그대로)
ALTER TABLE public.common_task ADD COLUMN IF NOT EXISTS framed jsonb;
-- 공고(같은 온톨로지 판)마다 승인된 인터뷰 과제는 하나 [확정: 2026-11-19 사용자] — 바꾸려면 approve_interview(replaces=…)
CREATE UNIQUE INDEX IF NOT EXISTS common_task_one_interview ON public.common_task (posting_id, ontology_version)
    WHERE status = 'approved' AND prompt_ref LIKE 'interview:%';
"""


def is_interview_ref(prompt_ref: str) -> bool:
    """채용담당자 인터뷰가 만든 공통 과제인가 (저장 키 prompt_ref = interview:{인터뷰 id})."""
    return prompt_ref.startswith(INTERVIEW_REF_PREFIX)


class InterviewTaskConflict(RuntimeError):
    """인터뷰 과제 승인이 그 공고의 지금 인터뷰 과제와 맞지 않는다 — 교체를 명시하지 않았거나(replaces 없음),
    짚은 과제가 지금 과제가 아니다 (그 사이 다른 승인). 화면을 다시 읽고 교체할지 정한다."""

    def __init__(self, posting_id: str, current: str | None, replaces: str | None) -> None:
        self.posting_id, self.current, self.replaces = posting_id, current, replaces
        if current is not None and replaces is None:
            msg = f"이 공고에는 이미 승인된 인터뷰 과제({current})가 있습니다 — 바꾸려면 교체를 명시하세요"
        else:
            msg = (
                f"교체하려는 과제({replaces or '없음'})가 이 공고의 지금 인터뷰 과제({current or '없음'})가 아닙니다"
                " — 화면을 다시 불러오세요"
            )
        super().__init__(msg)


@dataclass(frozen=True)
class CommonKey:
    posting_id: str
    ontology_version: str
    prompt_ref: str


class StoredCommon(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    prompt_ref: str  # 저장 키의 프롬프트 판 (draft_task@N… · skeleton · interview:{인터뷰 id})
    revision: int = Field(ge=1)
    status: CommonStatus
    task: TaskItem
    provenance: list[Provenance] = Field(default_factory=list)
    approved_by: str | None = None
    approved_at: datetime | None = None


class CommonTaskRepo(Protocol):
    async def reusable(self, key: CommonKey) -> StoredCommon | None: ...

    async def save_draft(
        self,
        key: CommonKey,
        task: TaskItem,
        provenance: list[Provenance],
        framed: FramedMaterial | None = None,
    ) -> StoredCommon: ...

    async def framed(self, common_id: str) -> FramedMaterial | None:
        """그 공통 과제의 틀 적용 재료와 틀 (#318) — 없으면 None."""
        ...

    async def approve(self, common_id: str, reviewer: str, at: datetime) -> None: ...

    async def missing(self, common_ids: list[str]) -> list[str]:
        """저장소에 없는 공통 과제 id (승인 전 확인 — 명세가 가리키는 판이 지워졌으면 확정할 수 없다)."""
        ...

    async def approved_kind(self, posting_id: str, ontology_version: str) -> TaskKind | None: ...

    async def interview_task(self, posting_id: str, ontology_version: str) -> StoredCommon | None:
        """그 공고(같은 온톨로지 판)에 승인된 인터뷰 과제 — 지원자 실행이 새로 만들지 않고 쓴다. 없으면 None."""
        ...

    async def interview_versions(self, posting_id: str) -> list[str]:
        """그 공고에 승인된 인터뷰 과제가 있는 온톨로지 판 (실행 판이 여기 없으면 그 공고 실행을 멈춘다)."""
        ...

    async def approve_interview(
        self, common_id: str, reviewer: str, at: datetime, *, replaces: str | None = None
    ) -> None:
        """채용담당자 승인 — 이미 승인된 인터뷰 과제가 있으면 `replaces` 로 그 id 를 짚어야 바꾼다 (InterviewTaskConflict)."""
        ...

    async def drop_interview_drafts(self, prompt_ref: str, *, keep: str | None = None) -> int:
        """인터뷰(interview:{id})의 초안 판을 지운다 — 폐기한 인터뷰, 승인한 인터뷰의 다른 판 (keep 은 남김).
        승인·교체된 판은 지우지 않는다. 빌드는 승인된 과제만 받으므로 초안에 걸린 빌드는 없다 (#266 D2b)."""
        ...

    async def incident_usage(
        self, incident_ids: list[str], *, exclude_posting: str
    ) -> dict[str, int]:
        """사례마다 그 사례로 공통 과제가 승인된 다른 공고 수 (task.source_incident) — 회사 사례 고르기용."""
        ...

    async def posting_incidents(self, posting_id: str) -> list[str]:
        """그 공고의 공통 과제(superseded 제외, 모든 온톨로지 판)가 쓴 사례 id. 사례 없는 과제는 "".
        순서: 승인된 과제가 있는 사례(최근 승인 순) → 초안만 있는 사례(최근 순) → 같으면 id 순.
        공고가 쓰던 사례를 계속 쓰게 한다 (spec/company_incidents.py)."""
        ...

    async def get(self, common_id: str) -> StoredCommon | None:
        """id 로 — 실행 만들 때 담당자가 고른 문제 (상태는 부르는 쪽이 본다)."""
        ...

    async def approved_tasks(
        self, posting_ids: Sequence[str], ontology_version: str
    ) -> list[tuple[str, StoredCommon]]:
        """그 공고들(이어진 공고 묶음)에 그 온톨로지 판으로 승인된 공통 과제 — (공고 id, 과제), 최근 승인 순.
        인터뷰 과제·공고 문구로 만든 과제 모두. 같은 공고에 문제가 여럿이면 실행 만들 때 담당자가 고른다
        [확정: 2026-11-20 사용자]."""
        ...


def _new_id() -> str:
    return f"ct-{uuid.uuid4().hex[:12]}"


def _with_id(task: TaskItem, common_id: str) -> TaskItem:
    # 지원자마다 같은 과제임을 id 로도 드러낸다 (실행 id 가 아니라 공통 과제 id). 슬롯도 새 id 를 가리키게
    slots = [x.model_copy(update={"parent_task_id": common_id}) for x in task.slots]
    return task.model_copy(update={"task_id": common_id, "slots": slots})


def _normalized(stored: StoredCommon) -> StoredCommon:
    # 인터뷰 과제는 다른 계층이 저장한다 — 과제 id·슬롯 parent 가 저장 id 와 같다는 전제(CommonRef.common_id = task_id)를 읽을 때 맞춘다
    return stored.model_copy(update={"task": _with_id(stored.task, stored.id)})


def _stored(row: dict[str, Any]) -> StoredCommon:
    return StoredCommon(
        prompt_ref=row["key"].prompt_ref,
        **{k: v for k, v in row.items() if k != "key"},
    )


def _not_interview(common_id: str, prompt_ref: str) -> None:
    # 인터뷰 과제는 채용담당자 승인(approve_interview)으로만 바뀐다 — 지원자 명세 승인이 다시 승인하면 승인자 기록이 덮이고
    # 교체한 과제가 되살아난다
    if is_interview_ref(prompt_ref):
        raise ValueError(f"인터뷰 과제는 approve_interview 로 승인합니다: {common_id}")


class InMemoryCommonStore:
    """단위 테스트·DB 없는 그래프용."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def reusable(self, key: CommonKey) -> StoredCommon | None:
        rows = [r for r in self.rows if r["key"] == key and r["status"] != "superseded"]
        rows.sort(key=lambda r: (r["status"] == "approved", r["revision"]), reverse=True)
        return _stored(rows[0]) if rows else None

    async def save_draft(
        self,
        key: CommonKey,
        task: TaskItem,
        provenance: list[Provenance],
        framed: FramedMaterial | None = None,
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
            "approved_at": None,
            "framed": framed,
        }
        self.rows.append(row)
        return _stored(row)

    async def framed(self, common_id: str) -> FramedMaterial | None:
        row = next((r for r in self.rows if r["id"] == common_id), None)
        return None if row is None else row.get("framed")

    async def approve(self, common_id: str, reviewer: str, at: datetime) -> None:
        row = next((r for r in self.rows if r["id"] == common_id), None)
        if row is None:
            raise KeyError(common_id)
        _not_interview(common_id, row["key"].prompt_ref)
        for r in self.rows:
            if r["key"] == row["key"] and r["status"] == "approved" and r is not row:
                r["status"] = "superseded"
        row["status"], row["approved_by"], row["approved_at"] = "approved", reviewer, at

    async def missing(self, common_ids: list[str]) -> list[str]:
        have = {r["id"] for r in self.rows}
        return [c for c in common_ids if c not in have]

    async def approved_kind(self, posting_id: str, ontology_version: str) -> TaskKind | None:
        rows = [
            r
            for r in self.rows
            if r["key"].posting_id == posting_id
            and r["key"].ontology_version == ontology_version
            and r["status"] == "approved"
        ]
        if not rows:
            return None
        latest = max(rows, key=lambda r: r["approved_at"])
        return latest["task"].task_kind

    def _interviews(self, posting_id: str) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r["key"].posting_id == posting_id
            and r["status"] == "approved"
            and is_interview_ref(r["key"].prompt_ref)
        ]

    async def interview_task(self, posting_id: str, ontology_version: str) -> StoredCommon | None:
        rows = [
            r for r in self._interviews(posting_id) if r["key"].ontology_version == ontology_version
        ]
        return _normalized(_stored(rows[0])) if rows else None

    async def interview_versions(self, posting_id: str) -> list[str]:
        return sorted({r["key"].ontology_version for r in self._interviews(posting_id)})

    async def approve_interview(
        self, common_id: str, reviewer: str, at: datetime, *, replaces: str | None = None
    ) -> None:
        row = next((r for r in self.rows if r["id"] == common_id), None)
        if row is None:
            raise KeyError(common_id)
        key = row["key"]
        if not is_interview_ref(key.prompt_ref):
            raise ValueError(f"인터뷰 과제가 아닙니다: {common_id}")
        if row["status"] == "superseded":
            raise ValueError(_SUPERSEDED.format(common_id))
        current = next(
            (
                r
                for r in self._interviews(key.posting_id)
                if r["key"].ontology_version == key.ontology_version
            ),
            None,
        )
        if current is row:
            return  # 이미 지금 과제 — 승인 기록을 덮지 않는다
        if (current["id"] if current else None) != replaces:
            raise InterviewTaskConflict(
                key.posting_id, current["id"] if current else None, replaces
            )
        if current is not None:
            current["status"] = "superseded"
        row["status"], row["approved_by"], row["approved_at"] = "approved", reviewer, at

    async def drop_interview_drafts(self, prompt_ref: str, *, keep: str | None = None) -> int:
        if not is_interview_ref(prompt_ref):
            raise ValueError(f"인터뷰 과제 키가 아닙니다: {prompt_ref}")
        gone = [
            r
            for r in self.rows
            if r["key"].prompt_ref == prompt_ref and r["status"] == "draft" and r["id"] != keep
        ]
        self.rows = [r for r in self.rows if not any(r is g for g in gone)]
        return len(gone)

    async def incident_usage(
        self, incident_ids: list[str], *, exclude_posting: str
    ) -> dict[str, int]:
        want = set(incident_ids)
        seen: dict[str, set[str]] = {}
        for r in self.rows:
            inc = r["task"].source_incident
            if r["status"] == "approved" and inc in want and r["key"].posting_id != exclude_posting:
                seen.setdefault(inc, set()).add(r["key"].posting_id)
        return {k: len(v) for k, v in seen.items()}

    async def posting_incidents(self, posting_id: str) -> list[str]:
        approved: dict[str, datetime] = {}
        created: dict[str, int] = {}
        for i, r in enumerate(self.rows):
            if r["key"].posting_id != posting_id or r["status"] == "superseded":
                continue
            inc = r["task"].source_incident or ""
            created[inc] = i  # 행 순서 = 만든 순서
            if r["status"] == "approved" and r["approved_at"] is not None:
                approved[inc] = max(approved.get(inc, r["approved_at"]), r["approved_at"])
        approved_first = sorted(approved, key=lambda k: (-approved[k].timestamp(), k))
        drafts = sorted((k for k in created if k not in approved), key=lambda k: (-created[k], k))
        return [*approved_first, *drafts]

    async def get(self, common_id: str) -> StoredCommon | None:
        row = next((r for r in self.rows if r["id"] == common_id), None)
        return _normalized(_stored(row)) if row else None

    async def approved_tasks(
        self, posting_ids: Sequence[str], ontology_version: str
    ) -> list[tuple[str, StoredCommon]]:
        rows = [
            r
            for r in self.rows
            if r["key"].posting_id in posting_ids
            and r["key"].ontology_version == ontology_version
            and r["status"] == "approved"
        ]
        rows.sort(key=lambda r: (r["approved_at"], r["id"]), reverse=True)
        return [(r["key"].posting_id, _normalized(_stored(r))) for r in rows]


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
            prompt_ref=r["prompt_ref"],
            revision=r["revision"],
            status=r["status"],
            task=TaskItem.model_validate(r["task"]),
            provenance=[Provenance.model_validate(p) for p in r["provenance"]],
            approved_by=r["approved_by"],
            approved_at=r.get("approved_at"),
        )

    async def framed(self, common_id: str) -> FramedMaterial | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT framed FROM public.common_task WHERE id = %s", (common_id,))
            r = await cur.fetchone()
        return (
            None if r is None or r["framed"] is None else FramedMaterial.model_validate(r["framed"])
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
        self,
        key: CommonKey,
        task: TaskItem,
        provenance: list[Provenance],
        framed: FramedMaterial | None = None,
    ) -> StoredCommon:
        cid = _new_id()
        task = _with_id(task, cid)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 같은 키 동시 저장: UNIQUE 위반이면 다시 번호를 매긴다 (드묾 — 3회까지)
            for _ in range(3):
                try:
                    await cur.execute(
                        "INSERT INTO public.common_task (id, posting_id, ontology_version, prompt_ref, revision, "
                        "task, provenance, framed) "
                        "SELECT %s, %s, %s, %s, COALESCE(MAX(revision), 0) + 1, %s, %s, %s "
                        "FROM public.common_task WHERE posting_id = %s AND ontology_version = %s AND prompt_ref = %s "
                        "RETURNING *",
                        (
                            cid,
                            key.posting_id,
                            key.ontology_version,
                            key.prompt_ref,
                            Jsonb(task.model_dump(mode="json")),
                            Jsonb([p.model_dump(mode="json", by_alias=True) for p in provenance]),
                            Jsonb(framed.model_dump(mode="json")) if framed is not None else None,
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
            _not_interview(common_id, r[2])
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

    async def missing(self, common_ids: list[str]) -> list[str]:
        if not common_ids:
            return []
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id FROM public.common_task WHERE id = ANY(%s)", (list(common_ids),)
            )
            have = {r[0] for r in await cur.fetchall()}
        return [c for c in common_ids if c not in have]

    async def approved_kind(self, posting_id: str, ontology_version: str) -> TaskKind | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT COALESCE(task->>'task_kind', 'incident') AS kind FROM public.common_task "
                "WHERE posting_id = %s AND ontology_version = %s AND status = 'approved' "
                "ORDER BY approved_at DESC NULLS LAST, revision DESC LIMIT 1",
                (posting_id, ontology_version),
            )
            r = await cur.fetchone()
        if r is None:
            return None
        kind = r["kind"]
        return kind if kind in ("incident", "feature") else "incident"

    async def interview_task(self, posting_id: str, ontology_version: str) -> StoredCommon | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.common_task WHERE posting_id = %s AND ontology_version = %s "
                "AND status = 'approved' AND prompt_ref LIKE %s "
                "ORDER BY approved_at DESC NULLS LAST LIMIT 1",
                (posting_id, ontology_version, f"{INTERVIEW_REF_PREFIX}%"),
            )
            r = await cur.fetchone()
        return _normalized(self._row(r)) if r else None

    async def interview_versions(self, posting_id: str) -> list[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT ontology_version FROM public.common_task WHERE posting_id = %s "
                "AND status = 'approved' AND prompt_ref LIKE %s ORDER BY 1",
                (posting_id, f"{INTERVIEW_REF_PREFIX}%"),
            )
            return [r[0] for r in await cur.fetchall()]

    async def approve_interview(
        self, common_id: str, reviewer: str, at: datetime, *, replaces: str | None = None
    ) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await approve_interview_task(conn, common_id, reviewer, at, replaces=replaces)

    async def drop_interview_drafts(self, prompt_ref: str, *, keep: str | None = None) -> int:
        async with self._pool.connection() as conn:
            return await drop_interview_drafts(conn, prompt_ref, keep=keep)

    async def incident_usage(
        self, incident_ids: list[str], *, exclude_posting: str
    ) -> dict[str, int]:
        if not incident_ids:
            return {}
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT task->>'source_incident', COUNT(DISTINCT posting_id) FROM public.common_task "
                "WHERE status = 'approved' AND task->>'source_incident' = ANY(%s) AND posting_id <> %s "
                "GROUP BY 1",
                (list(incident_ids), exclude_posting),
            )
            return {r[0]: int(r[1]) for r in await cur.fetchall()}

    async def posting_incidents(self, posting_id: str) -> list[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(task->>'source_incident', '') AS inc, "
                "MAX(approved_at) FILTER (WHERE status = 'approved') AS approved_last, "
                "MAX(created_at) AS created_last "
                "FROM public.common_task WHERE posting_id = %s AND status <> 'superseded' "
                "GROUP BY 1 ORDER BY approved_last DESC NULLS LAST, created_last DESC, inc",
                (posting_id,),
            )
            return [r[0] for r in await cur.fetchall()]

    async def get(self, common_id: str) -> StoredCommon | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM public.common_task WHERE id = %s", (common_id,))
            r = await cur.fetchone()
        return _normalized(self._row(r)) if r else None

    async def approved_tasks(
        self, posting_ids: Sequence[str], ontology_version: str
    ) -> list[tuple[str, StoredCommon]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.common_task WHERE posting_id = ANY(%s) AND ontology_version = %s "
                "AND status = 'approved' ORDER BY approved_at DESC NULLS LAST, id DESC",
                (list(posting_ids), ontology_version),
            )
            rows = await cur.fetchall()
        return [(r["posting_id"], _normalized(self._row(r))) for r in rows]


async def replaced_common(
    store: CommonTaskRepo, spec: TaskSpec, chosen: str | None = None
) -> list[str]:
    """명세의 공통 과제 중 이 공고가 이제 쓰지 않는 것 — 공고(같은 온톨로지 판)에 승인된 인터뷰 과제가 있으면 그 과제만 쓴다.

    인터뷰 과제를 가리키는데 그 과제가 교체됐거나, 인터뷰 과제가 승인되기 전에 만든 명세(P1 과제)면 여기 든다.
    chosen(실행 만들 때 담당자가 고른 문제)이 있으면 그 과제만 쓴다 — 공고의 인터뷰 과제와 달라도 된다.
    명세 승인 전(API 409)과 확정(finalize) 때 본다.
    """
    if chosen is not None:
        # 고른 과제가 교체(superseded)·삭제됐거나, 명세가 그 과제를 가리키지 않으면(사라져서 공통 과제 없이 만든 명세 포함) 막는다
        got = await store.get(chosen)
        stale = [r.common_id for r in spec.common_refs if r.common_id != chosen]
        usable = got is not None and got.status == "approved"
        if not usable or chosen not in {r.common_id for r in spec.common_refs}:
            stale = [chosen, *stale]
        return stale
    if not spec.common_refs:
        return []
    current = await store.interview_task(spec.posting_id, spec.ontology_version)
    if current is None:
        return [r.common_id for r in spec.common_refs if is_interview_ref(r.prompt_ref)]
    return [r.common_id for r in spec.common_refs if r.common_id != current.id]


async def interview_lock(
    store: CommonTaskRepo, spec: TaskSpec, chosen: str | None = None
) -> str | None:
    """이 명세의 공통 과제를 지원자 명세의 수정 요청으로 고칠 수 없으면 그 인터뷰 과제 id — 명세가 인터뷰 과제를 가리키거나
    그 공고에 지금 승인된 인터뷰 과제가 있으면 (다시 만들어도 그 과제를 쓴다). 고칠 곳은 인터뷰 화면.
    chosen(실행 만들 때 고른 문제)이면 그 과제 — 다시 만들어도 고른 과제를 쓴다.
    공통 과제가 없는 명세는 막지 않는다 — 다시 만들면 인터뷰 과제가 있을 때 그 과제를 그대로 쓴다 (nodes/spec.py)."""
    if not spec.common_refs:
        return None
    if chosen is not None:
        return chosen
    for r in spec.common_refs:
        if is_interview_ref(r.prompt_ref):
            return r.common_id
    current = await store.interview_task(spec.posting_id, spec.ontology_version)
    return current.id if current is not None else None


async def approve_interview_task(
    conn: AsyncConnection[Any],
    common_id: str,
    reviewer: str,
    at: datetime,
    *,
    replaces: str | None = None,
) -> None:
    """인터뷰 과제 승인 (호출하는 쪽 트랜잭션 안에서 — 인터뷰 판본 확정과 함께 묶을 수 있게).

    공고(같은 온톨로지 판)에 이미 승인된 인터뷰 과제가 있으면(같은 인터뷰의 이전 판 포함) `replaces` 가 그 id 여야 바꾼다 —
    모집 중에 과제가 말없이 바뀌지 않게. 이미 지금 과제면 아무것도 하지 않는다 (승인 기록을 덮지 않음).
    교체된(superseded) 과제는 다시 승인하지 않는다 — 되돌리려면 새 판으로 저장해 승인한다.
    충돌(InterviewTaskConflict)이면 이 함수는 아무것도 바꾸지 않고(savepoint) 호출하는 쪽 트랜잭션도 계속 쓸 수 있다 —
    예외를 그대로 올려 인터뷰 판본 확정과 함께 되돌린다.
    """
    cur = await conn.execute(
        "SELECT posting_id, ontology_version, prompt_ref, status FROM public.common_task "
        "WHERE id = %s FOR UPDATE",
        (common_id,),
    )
    r = await cur.fetchone()
    if r is None:
        raise KeyError(common_id)
    posting_id, version, prompt_ref, status = r
    if not is_interview_ref(prompt_ref):
        raise ValueError(f"인터뷰 과제가 아닙니다: {common_id}")
    if status == "superseded":
        raise ValueError(_SUPERSEDED.format(common_id))
    cur = await conn.execute(
        "SELECT id FROM public.common_task WHERE posting_id = %s AND ontology_version = %s "
        "AND status = 'approved' AND prompt_ref LIKE %s FOR UPDATE",
        (posting_id, version, f"{INTERVIEW_REF_PREFIX}%"),
    )
    found = [x[0] for x in await cur.fetchall()]
    current = found[0] if found else None
    if current == common_id:
        return
    if current != replaces:
        # 잠그는 동안 다른 승인이 바꿨으면 위 조회는 그 결과를 못 본다 — 알릴 때는 지금 과제를 다시 읽는다
        now = await _approved_interview_id(conn, posting_id, version)
        raise InterviewTaskConflict(posting_id, now, replaces)
    try:
        async with conn.transaction():  # savepoint — 충돌이면 이 안의 변경만 되돌린다
            if current is not None:
                await conn.execute(
                    "UPDATE public.common_task SET status = 'superseded' WHERE id = %s", (current,)
                )
            await conn.execute(
                "UPDATE public.common_task SET status = 'approved', approved_by = %s, "
                "approved_at = %s WHERE id = %s",
                (reviewer, at, common_id),
            )
    except errors.UniqueViolation:
        # 같은 공고의 다른 인터뷰 승인이 먼저 끝났다 (common_task_one_interview)
        now = await _approved_interview_id(conn, posting_id, version)
        raise InterviewTaskConflict(posting_id, now, replaces) from None


async def drop_interview_drafts(
    conn: AsyncConnection[Any], prompt_ref: str, *, keep: str | None = None
) -> int:
    """인터뷰 초안 판 지우기 (호출하는 쪽 트랜잭션 안에서도 쓴다 — 인터뷰 승인)."""
    if not is_interview_ref(prompt_ref):
        raise ValueError(f"인터뷰 과제 키가 아닙니다: {prompt_ref}")
    cur = await conn.execute(
        "DELETE FROM public.common_task WHERE prompt_ref = %s AND status = 'draft' "
        "AND id IS DISTINCT FROM %s",
        (prompt_ref, keep),
    )
    return cur.rowcount


async def _approved_interview_id(
    conn: AsyncConnection[Any], posting_id: str, ontology_version: str
) -> str | None:
    cur = await conn.execute(
        "SELECT id FROM public.common_task WHERE posting_id = %s AND ontology_version = %s "
        "AND status = 'approved' AND prompt_ref LIKE %s LIMIT 1",
        (posting_id, ontology_version, f"{INTERVIEW_REF_PREFIX}%"),
    )
    r = await cur.fetchone()
    return r[0] if r else None
