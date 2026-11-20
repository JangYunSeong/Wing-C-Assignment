"""새 공고 저장소 (task_db public — 앱 쓰기 데이터. 온톨로지 스키마와 분리).

posting_suggestion : 추천 결과 (절 분리 문장·후보). 확인 전 상태.
provisional_posting: 확인된 임시 공고 — 직군·업종·확인자·문장별 매칭·규칙 버전. 상태 pending_review (환류 대기).
posting_meta       : 사람이 정한 공고 속성 — 신입·경력 (postings/career.py). 온톨로지 공고·임시 공고 모두 (온톨로지는 읽기만).
공고 원문은 지원자 개인정보가 아니다. 내보내기(온톨로지 레포 검수용)는 보류 [미정].
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from task_spec.postings.search import COMPANY_NOISE, CompanyFilter, SearchTerm
from task_spec.postings.suggest import OntologyLists
from task_spec.schemas import (
    AnalyzedLine,
    Candidate,
    CareerLevel,
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
CREATE TABLE IF NOT EXISTS public.posting_meta (
    posting_id text PRIMARY KEY,
    career_level text NOT NULL CHECK (career_level IN ('신입', '경력')),
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now()
);
-- 사람이 정한 공고의 회사 [확정: 2026-11-19 사용자 — 인증 전에는 입력한 회사] — 공고 데이터(온톨로지·임시 공고 칸)보다 먼저.
-- 회사 결정적 사건을 고를 때 쓴다 (spec/company_incidents.py). 온톨로지는 읽기만 하므로(불변식 4) task_db 에 둔다
CREATE TABLE IF NOT EXISTS public.posting_company (
    posting_id text PRIMARY KEY,
    company text NOT NULL,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now()
);
"""

PROVISIONAL_PREFIX = "prov-"


def is_provisional(posting_id: str) -> bool:
    return posting_id.startswith(PROVISIONAL_PREFIX)


def text_sha256(text: str) -> str:
    """공고 원문 해시 — 추천 저장(posting_suggestion.text_sha256)과 같은 공고 찾기가 같은 값을 쓴다."""
    return hashlib.sha256(text.encode()).hexdigest()


def rules_fingerprint(lists: OntologyLists) -> str:
    joined = "\n".join(f"{p}:{h}" for p, h in sorted(lists.rules_sha.items()))
    return hashlib.sha256(joined.encode()).hexdigest()


class SamePostingExists(Exception):
    """같은 회사·같은 원문의 임시 공고가 이미 있다 (create_provisional 의 unique)."""

    def __init__(self, posting_ids: list[str]) -> None:
        super().__init__(", ".join(posting_ids))
        self.posting_ids = posting_ids


async def _same_postings(conn: AsyncConnection[Any], company_key: str, digest: str) -> list[str]:
    cur = await conn.execute(
        "SELECT p.id FROM public.provisional_posting p "
        "JOIN public.posting_suggestion s ON s.id = p.suggestion_id "
        "LEFT JOIN public.posting_company pc ON pc.posting_id = p.id WHERE s.text_sha256 = %s "
        "AND lower(regexp_replace(coalesce(pc.company, p.company, ''), %s, '', 'g')) = %s "
        "ORDER BY p.created_at, p.id",
        (digest, COMPANY_NOISE, company_key),
    )
    return [r[0] for r in await cur.fetchall()]


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
                text_sha256(text),
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
        unique: tuple[str, str] | None = None,
    ) -> str:
        """unique=(회사 키, 원문 해시)면 같은 회사·같은 원문 공고가 있을 때 만들지 않고 SamePostingExists — 검사와 저장을 한
        트랜잭션·잠금으로 묶어, 확정을 두 번 눌러도(동시 요청) 하나만 생긴다 (2026-11-20 리뷰)."""
        pid = f"{PROVISIONAL_PREFIX}{uuid.uuid4().hex[:12]}"
        async with self._pool.connection() as conn, conn.transaction():
            if unique is not None:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"provisional_posting:{unique[0]}:{unique[1]}",),
                )
                if same := await _same_postings(conn, *unique):
                    raise SamePostingExists(same)
            await conn.execute(
                "INSERT INTO public.provisional_posting (id, suggestion_id, company, title, sub, secondary_sub, industry_id, "
                "confirmed_by, ontology_version, rules_sha256, lines, n_unmatched) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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

    async def career_levels(self, pids: list[str]) -> dict[str, CareerLevel]:
        """공고 id → 사람이 정한 신입·경력 (정하지 않은 공고는 빠진다)."""
        if not pids:
            return {}
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT posting_id, career_level FROM public.posting_meta WHERE posting_id = ANY(%s)",
                (pids,),
            )
            return {r["posting_id"]: r["career_level"] for r in await cur.fetchall()}

    async def set_career_level(self, pid: str, level: CareerLevel | None, set_by: str) -> None:
        """None 이면 정한 값을 지운다 (다시 공고 문구 추정)."""
        if level is None:
            await self._exec("DELETE FROM public.posting_meta WHERE posting_id = %s", (pid,))
            return
        await self._exec(
            "INSERT INTO public.posting_meta (posting_id, career_level, set_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (posting_id) DO UPDATE SET career_level = EXCLUDED.career_level, "
            "set_by = EXCLUDED.set_by, set_at = now()",
            (pid, level, set_by),
        )

    async def init_career_level(self, pid: str, level: CareerLevel, set_by: str) -> None:
        """정한 값이 없을 때만 넣는다 (실행 만들기 — 그 사이 누가 정했으면 그 값을 덮지 않는다)."""
        await self._exec(
            "INSERT INTO public.posting_meta (posting_id, career_level, set_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (posting_id) DO NOTHING",
            (pid, level, set_by),
        )

    async def entered_companies(self, pids: list[str]) -> dict[str, str]:
        """공고 id → 사람이 정한 회사 표기 (정하지 않은 공고는 빠진다)."""
        if not pids:
            return {}
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT posting_id, company FROM public.posting_company WHERE posting_id = ANY(%s)",
                (pids,),
            )
            return {r["posting_id"]: r["company"] for r in await cur.fetchall()}

    async def set_company(self, pid: str, company: str | None, set_by: str) -> None:
        """None 이면 정한 값을 지운다 (다시 공고의 회사 칸)."""
        if company is None:
            await self._exec("DELETE FROM public.posting_company WHERE posting_id = %s", (pid,))
            return
        await self._exec(
            "INSERT INTO public.posting_company (posting_id, company, set_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (posting_id) DO UPDATE SET company = EXCLUDED.company, "
            "set_by = EXCLUDED.set_by, set_at = now()",
            (pid, company, set_by),
        )

    async def init_company(self, pid: str, company: str, set_by: str) -> None:
        """정한 값이 없을 때만 넣는다 (실행 만들기 — 그 사이 누가 정했으면 그 값을 덮지 않는다)."""
        await self._exec(
            "INSERT INTO public.posting_company (posting_id, company, set_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (posting_id) DO NOTHING",
            (pid, company, set_by),
        )

    async def _exec(self, query: str, params: tuple[Any, ...]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(query, params)  # type: ignore[arg-type]

    async def all_entered_companies(self) -> dict[str, str]:
        """사람이 정한 회사 전부 — 공고 id → 회사 표기 (한 회사 공고 찾기, GET /postings?company=)."""
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT posting_id, company FROM public.posting_company")
            return {r["posting_id"]: r["company"] for r in await cur.fetchall()}

    async def same_postings(self, company_key: str, digest: str) -> list[str]:
        """같은 회사·같은 원문의 임시 공고 id — 먼저 만든 것부터. 회사는 사람이 정한 회사(posting_company)가 먼저이고,
        company_key 는 회사 id(postings/company.company_id — 회사가 없으면 '')로, 공고 쪽도 같은 규칙으로 맞춘다.
        같은 공고로 다른 문제를 만들려고 따로 등록하는 일도 있어 막지 않고 알린다 (POST /postings 의 allow_duplicate)."""
        async with self._pool.connection() as conn:
            return await _same_postings(conn, company_key, digest)

    async def similar_postings(self, company_key: str, title: str, digest: str) -> list[str]:
        """같은 회사·같은 제목(공백·대소문자 무시)인데 원문이 다른 임시 공고 id — 막지 않고 확인 화면에 알린다."""
        key = "".join(title.split()).casefold()
        if not key:
            return []
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT p.id FROM public.provisional_posting p "
                "JOIN public.posting_suggestion s ON s.id = p.suggestion_id "
                "LEFT JOIN public.posting_company pc ON pc.posting_id = p.id WHERE s.text_sha256 <> %s "
                "AND lower(regexp_replace(coalesce(pc.company, p.company, ''), %s, '', 'g')) = %s "
                "AND lower(regexp_replace(coalesce(p.title, ''), '[\\s\\u00a0\\u3000]', '', 'g')) = %s "
                "ORDER BY p.created_at, p.id",
                (digest, COMPANY_NOISE, company_key, key),
            )
            return [r[0] for r in await cur.fetchall()]

    async def search_provisional(
        self, terms: list[SearchTerm], limit: int | None, company: CompanyFilter | None = None
    ) -> list[dict[str, Any]]:
        """임시 공고를 회사·직무명·id·직군으로 (postings/search.search_terms), 최근 것부터. limit None = 전부."""
        where = ["TRUE"]
        params: list[Any] = []
        if company is not None:
            clause, extra = company.sql()
            where.append(clause)
            params += extra
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
