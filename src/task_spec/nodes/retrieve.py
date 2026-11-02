"""③ retrieve T1(주장 → 역량 후보) · T2(이력서 ↔ 자기소개서) · 평가 근거.

T1 과 T2 는 병렬로 돈 뒤 evidence 에서 합류한다. T2 는 두 종류 문서가 모두 있을 때만 일한다.
T1: 검색(retrieve/t1.py, 결정적·로컬) → 선택(retrieve/select.py, LLM 0~2개 또는 uncovered).
공고 역량은 가산점만 (불변식 5). LLM 에는 가명화본에서 나온 주장 텍스트만 간다 (불변식 2).
TODO(cross-doc-link): T2 실제 구현.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from task_spec.llm import LLMClient
from task_spec.nodes.pseudonymize import (
    PseudonymizedNotAvailable,
    derive_pseudonymized,
    load_pseudonymized,
)
from task_spec.ontology.queries import evidence as query_evidence
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry
from task_spec.retrieve.select import SelectInputClaim, candidate_refs, select
from task_spec.retrieve.t1 import T1Retriever
from task_spec.schemas import ClaimCandidates, NodeError, PseudonymizedText, RunState

T1 = "retrieve_t1"
TOP_K = 10


def make_retrieve_t1(
    retriever: T1Retriever | None, llm: LLMClient | None, prompts: PromptRegistry | None
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def retrieve_t1(state: RunState) -> dict[str, Any]:
        if not state.claims:
            return {"candidates": []}
        # 온톨로지·규칙 미연결 (단위 테스트 그래프) — evidence 와 같이 건너뛴다
        if retriever is None:
            return {"candidates": []}
        p = state.posting_profile
        sub = p.sub if p else None
        posting = {c.competency_id: c.primary for c in p.competencies} if p else {}

        def search_all() -> list[list[Any]]:
            return [
                retriever.search(
                    f"{c.action}. {c.quote}", sub, scope="soft", posting=posting, k=TOP_K
                )
                for c in state.claims
            ]

        hits = await asyncio.to_thread(search_all)
        if llm is None or prompts is None:
            return {
                "candidates": [
                    ClaimCandidates(
                        claim_id=c.claim_id, candidates=candidate_refs(retriever.cards, h)
                    )
                    for c, h in zip(state.claims, hits, strict=True)
                ],
                "errors": [
                    NodeError(node=T1, code="llm_not_configured", message="LLM 없음 — 선택 생략")
                ],
            }

        errors: list[NodeError] = []
        sources: dict[str, PseudonymizedText | None] = {}
        inputs: list[SelectInputClaim] = []
        for c, h in zip(state.claims, hits, strict=True):
            if c.doc_id not in sources:
                ref = next((d for d in state.pseudo_docs if d.doc_id == c.doc_id), None)
                try:
                    sources[c.doc_id] = (
                        await asyncio.to_thread(load_pseudonymized, ref) if ref else None
                    )
                except PseudonymizedNotAvailable:
                    sources[c.doc_id] = None
            src = sources[c.doc_id]
            try:
                if src is None:
                    raise ValueError("가명화본 없음")
                text = derive_pseudonymized(src, quote=c.quote, summary=c.action)
            except ValueError:
                errors.append(
                    NodeError(
                        node=T1,
                        code="claim_not_pseudonymized",
                        message="주장 텍스트 검사 실패",
                        ref_id=c.claim_id,
                    )
                )
                continue
            inputs.append(SelectInputClaim(claim_id=c.claim_id, text=text, hits=h))

        label = f"{sub}: {retriever.cards.sub_labels.get(sub, sub)}" if sub else "(미상)"
        res = await select(inputs, sub=label, cards=retriever.cards, llm=llm, prompts=prompts)
        return {
            "candidates": res.items,
            "provenance": res.provenance,
            "errors": errors + res.errors,
        }

    return retrieve_t1


async def retrieve_t2(state: RunState) -> dict[str, Any]:
    if not state.has_both_kinds():
        return {}
    return {"links": []}


def make_evidence(
    ontology: OntologyRepository | None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    """③ 평가 근거: 공고 역량(+ 후보 역량, retrieve_t1 구현 후) → 셀 범위 NCS·대표 ESCO (결정적)."""

    async def evidence(state: RunState) -> dict[str, Any]:
        p = state.posting_profile
        if ontology is None or p is None or not (p.sub and p.industry_id):
            return {"evidence": []}
        ids = list(
            dict.fromkeys(
                p.competency_ids + [c for cand in state.candidates for c in cand.competency_ids]
            )
        )
        rows = await query_evidence(
            ontology,
            ids,
            sub=p.sub,
            industry_id=p.industry_id,
            secondary_sub=p.secondary_sub,
            in_pilot=p.in_pilot,
        )
        return {"evidence": rows}

    return evidence
