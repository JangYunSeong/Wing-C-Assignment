"""③ retrieve T1(주장 → 역량 후보) · T2(이력서 ↔ 자기소개서) · 평가 근거.

T1 과 T2 는 병렬로 돈 뒤 evidence 에서 합류한다. T2 는 두 종류 문서가 모두 있을 때만 일한다.
공고 역량은 가산점만 (불변식 5).
TODO(competency-retrieve / cross-doc-link / ontology-artifact): 실제 구현.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from task_spec.ontology.queries import evidence as query_evidence
from task_spec.ontology.repository import OntologyRepository
from task_spec.schemas import RunState


async def retrieve_t1(state: RunState) -> dict[str, Any]:
    return {"candidates": []}


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
