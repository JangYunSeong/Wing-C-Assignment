"""③ retrieve — T1(주장 → 역량 후보 검색 → LLM 선택)."""

from __future__ import annotations

import asyncio

from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.suggest import OntologyLists
from task_spec.retrieve.cards import CardSet, CompetencyCard, load_cards
from task_spec.retrieve.t1 import Hit, T1Retriever


async def build_retriever(repo: OntologyRepository, lists: OntologyLists) -> T1Retriever | None:
    """매칭 규칙이 적재돼 있을 때만 (없으면 None — retrieve_t1 이 errors 에 남긴다)."""
    if lists.matcher is None:
        return None
    cards = await load_cards(repo, lists.matcher)
    return await asyncio.to_thread(T1Retriever, cards, lists.matcher)


__all__ = ["CardSet", "CompetencyCard", "Hit", "T1Retriever", "build_retriever", "load_cards"]
