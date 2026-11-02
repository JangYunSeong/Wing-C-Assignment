"""③ retrieve T1(주장 → 역량 후보) · T2(이력서 ↔ 자기소개서) · 평가 근거.

T1 과 T2 는 병렬로 돈 뒤 evidence 에서 합류한다. T2 는 두 종류 문서가 모두 있을 때만 일한다.
공고 역량은 가산점만 (불변식 5).
TODO(competency-retrieve / cross-doc-link / ontology-artifact): 실제 구현.
"""

from __future__ import annotations

from typing import Any

from task_spec.schemas import RunState


async def retrieve_t1(state: RunState) -> dict[str, Any]:
    return {"candidates": []}


async def retrieve_t2(state: RunState) -> dict[str, Any]:
    if not state.has_both_kinds():
        return {}
    return {"links": []}


async def evidence(state: RunState) -> dict[str, Any]:
    return {"evidence": []}
