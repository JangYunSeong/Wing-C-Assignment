"""인터뷰가 읽는 공고 — 프로필·공고 줄·회사. 회사는 사람이 입력한 값이 공고 데이터보다 먼저다 (#260 [확정: 2026-11-19 사용자])."""

from __future__ import annotations

from dataclasses import dataclass

from task_spec.ontology.queries import posting_profile
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.company import posting_companies
from task_spec.postings.store import PostingStore, is_provisional, provisional_profile
from task_spec.postings.suggest import OntologyLists
from task_spec.schemas import PostingProfile


@dataclass(frozen=True)
class PostingSource:
    ontology: OntologyRepository | None
    store: PostingStore | None
    lists: OntologyLists | None

    async def profile(self, posting_id: str) -> PostingProfile | None:
        if is_provisional(posting_id):
            if self.store is None or self.lists is None:
                return None
            row = await self.store.get_provisional(posting_id)
            return provisional_profile(row, self.lists) if row else None
        if self.ontology is None:
            return None
        return await posting_profile(self.ontology, posting_id)

    async def lines(self, posting_id: str) -> list[tuple[str, str]]:
        """(절, 줄) — 인터뷰 맥락에 넣기 전에 가명화한다."""
        if is_provisional(posting_id):
            row = await self.store.get_provisional(posting_id) if self.store else None
            return [
                (str(x.get("section", "")), str(x["text"])) for x in (row or {}).get("lines", [])
            ]
        if self.ontology is None:
            return []
        rows = await self.ontology.fetch(
            "SELECT section, text FROM posting_line WHERE position_id = %s ORDER BY id",
            (posting_id,),
        )
        return [(str(r["section"] or ""), str(r["text"])) for r in rows]

    async def company(self, posting_id: str) -> str | None:
        found = await posting_companies([posting_id], ontology=self.ontology, store=self.store)
        c = found.get(posting_id)
        return c.name if c is not None else None
