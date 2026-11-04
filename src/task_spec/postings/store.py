"""새 공고 저장소 (task_db public — 앱 쓰기 데이터. 온톨로지 스키마와 분리).

posting_suggestion : 추천 결과 (절 분리 문장·후보). 확인 전 상태.
provisional_posting: 확인된 임시 공고 — 직군·업종·확인자·문장별 매칭·규칙 버전. 상태 pending_review (환류 대기).
공고 원문은 지원자 개인정보가 아니다. 내보내기(온톨로지 레포 검수용)는 보류 [미정].
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from task_spec.postings.search import SearchTerm
from task_spec.postings.suggest import OntologyLists
from task_spec.schemas import (
    AnalyzedLine,
    Candidate,
    PostingCompetency,
    PostingLineIn,
    PostingProfile,
    PostingTool,
)

DDL = """
CREATE TABLE IF NOT EXISTS public.posting_suggestion (
    id text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    text_sha256 text NOT NULL,
    company text,
    title text,
    segment_method text NOT NULL,
    lines jsonb NOT NULL,
    sub_candidates jsonb NOT NULL,
    industry_candidates jsonb NOT NULL,
    meta jsonb NOT NULL,
    ontology_version text NOT NULL
);
CREATE TABLE IF NOT EXISTS public.provisional_posting (
    id text PRIMARY KEY,
    suggestion_id text NOT NULL REFERENCES public.posting_suggestion(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    company text,
    title text,
    sub text NOT NULL,
    secondary_sub text,
    industry_id text NOT NULL,
    confirmed_by text NOT NULL,
    ontology_version text NOT NULL,
    rules_sha256 text NOT NULL,
    lines jsonb NOT NULL,
    n_unmatched integer NOT NULL,
    status text NOT NULL DEFAULT 'pending_review' CHECK (status IN ('pending_review', 'exported', 'reviewed'))
);
"""

PROVISIONAL_PREFIX = "prov-"


def is_provisional(posting_id: str) -> bool:
    return posting_id.startswith(PROVISIONAL_PREFIX)


def rules_fingerprint(lists: OntologyLists) -> str:
    joined = "\n".join(f"{p}:{h}" for p, h in sorted(lists.rules_sha.items()))
    return hashlib.sha256(joined.encode()).hexdigest()


class PostingStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, conninfo: str) -> PostingStore:
        pool = AsyncConnectionPool(
            conninfo, min_size=1, max_size=4, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True)
        async with pool.connection() as conn:
            await conn.execute(DDL)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def _one(self, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)  # type: ignore[arg-type]
            return await cur.fetchone()

    async def create_suggestion(
        self,
        *,
        text: str,
        company: str,
        title: str,
        method: str,
        lines: list[PostingLineIn],
        subs: list[Candidate],
        industries: list[Candidate],
        meta: dict[str, Any],
        ontology_version: str,
    ) -> str:
        sid = f"sug-{uuid.uuid4().hex[:12]}"
        await self._one(
            "INSERT INTO public.posting_suggestion (id, text_sha256, company, title, segment_method, lines, "
            "sub_candidates, industry_candidates, meta, ontology_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                sid,
                hashlib.sha256(text.encode()).hexdigest(),
                company,
                title,
                method,
                Jsonb([x.model_dump() for x in lines]),
                Jsonb([x.model_dump() for x in subs]),
                Jsonb([x.model_dump() for x in industries]),
                Jsonb(meta),
                ontology_version,
            ),
        )
        return sid

    async def get_suggestion(self, sid: str) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM public.posting_suggestion WHERE id = %s", (sid,))

    async def create_provisional(
        self,
        *,
        suggestion_id: str,
        company: str | None,
        title: str | None,
        sub: str,
        secondary_sub: str | None,
        industry_id: str,
        confirmed_by: str,
        ontology_version: str,
        rules_sha256: str,
        lines: list[AnalyzedLine],
    ) -> str:
        pid = f"{PROVISIONAL_PREFIX}{uuid.uuid4().hex[:12]}"
        await self._one(
            "INSERT INTO public.provisional_posting (id, suggestion_id, company, title, sub, secondary_sub, industry_id, "
            "confirmed_by, ontology_version, rules_sha256, lines, n_unmatched) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                pid,
                suggestion_id,
                company,
                title,
                sub,
                secondary_sub,
                industry_id,
                confirmed_by,
                ontology_version,
                rules_sha256,
                Jsonb([x.model_dump() for x in lines]),
                sum(1 for x in lines if x.kind == "unmatched"),
            ),
        )
        return pid

    async def get_provisional(self, pid: str) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM public.provisional_posting WHERE id = %s", (pid,))

    async def search_provisional(self, terms: list[SearchTerm], limit: int) -> list[dict[str, Any]]:
        """임시 공고를 회사·직무명·id·직군으로 (postings/search.search_terms), 최근 것부터."""
        where = ["TRUE"]
        params: list[Any] = []
        for t in terms:
            where.append(
                "(company ILIKE %s ESCAPE '\\' OR title ILIKE %s ESCAPE '\\'"
                " OR id ILIKE %s ESCAPE '\\' OR sub ILIKE %s ESCAPE '\\' OR sub = ANY(%s))"
            )
            params += [t.pattern] * 4 + [list(t.subs)]
        params.append(limit)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, company, title, sub, industry_id FROM public.provisional_posting"  # type: ignore[arg-type]
                f" WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            )
            return await cur.fetchall()


def provisional_profile(row: dict[str, Any], lists: OntologyLists) -> PostingProfile:
    """저장된 임시 공고 → 공고 프로필 (경로 1 과 같은 모양, source=provisional). 주/보조 규칙도 같다."""
    lines = [AnalyzedLine.model_validate(x) for x in row["lines"]]
    comp_sec: dict[str, set[str]] = defaultdict(set)
    comp_lvl: dict[str, set[str]] = defaultdict(set)
    comp_lines: dict[str, list[str]] = defaultdict(list)
    tool_sec: dict[str, set[str]] = defaultdict(set)
    for x in lines:
        for c in x.comps:
            comp_sec[c].add(x.section)
            comp_lines[c].append(x.line_id)
            if x.level:
                comp_lvl[c].add(x.level)
        for t in x.tools:
            tool_sec[t].add(x.section)
    order = {"duties": 0, "required": 1, "preferred": 2}
    return PostingProfile(
        posting_id=row["id"],
        ontology_version=row["ontology_version"],
        source="provisional",
        company=row["company"],
        position_name=row["title"],
        sub=row["sub"],
        secondary_sub=row["secondary_sub"],
        industry_id=row["industry_id"],
        in_pilot=bool(lists.subs.get(row["sub"], {}).get("in_pilot")),
        competencies=[
            PostingCompetency(
                competency_id=c,
                label=lists.competency_label.get(c, c),
                sections=sorted(secs, key=order.__getitem__),  # type: ignore[arg-type]
                primary=bool({"duties", "required"} & secs),
                levels=sorted(comp_lvl[c]),
                line_ids=sorted(comp_lines[c]),
            )
            for c, secs in sorted(comp_sec.items())
        ],
        tools=[
            PostingTool(
                tool_id=t,
                label=lists.tool_info.get(t, {}).get("label", t),
                kind=lists.tool_info.get(t, {}).get("kind"),
                family_id=lists.tool_info.get(t, {}).get("family_id"),
                sections=sorted(secs, key=order.__getitem__),  # type: ignore[arg-type]
            )
            for t, secs in sorted(tool_sec.items())
        ],
    )
