"""① 공고 프로필 — 온톨로지 조회 (결정적, LLM 없음).

경로 1(온톨로지에 있는 포지션)만. 새 공고(경로 2, provisional)는 별도 작업.
온톨로지가 없으면(테스트·골격) 빈 프로필.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from task_spec.ontology.queries import posting_profile as query_profile
from task_spec.ontology.repository import OntologyRepository
from task_spec.schemas import NodeError, PostingProfile, RunState


def make_posting_profile(
    ontology: OntologyRepository | None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def posting_profile(state: RunState) -> dict[str, Any]:
        if ontology is None:
            return {
                "posting_profile": PostingProfile(
                    posting_id=state.posting_id, ontology_version=state.ontology_version
                )
            }
        profile = await query_profile(ontology, state.posting_id)
        if profile is None:
            return {
                "errors": [
                    NodeError(
                        node="posting_profile",
                        code="posting_not_found",
                        message="온톨로지에 없는 공고",
                        ref_id=state.posting_id,
                    )
                ]
            }
        return {"posting_profile": profile}

    return posting_profile
