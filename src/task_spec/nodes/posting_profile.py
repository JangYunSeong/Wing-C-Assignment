"""① 공고 프로필 — 온톨로지 조회 (결정적, LLM 없음).

TODO(ontology-artifact): 버전 고정 산출물에서 공고 → 역량 프로필 조회로 교체.
"""

from __future__ import annotations

from typing import Any

from task_spec.schemas import PostingProfile, RunState


async def posting_profile(state: RunState) -> dict[str, Any]:
    profile = PostingProfile(
        posting_id=state.posting_id,
        ontology_version=state.ontology_version,
        competency_ids=[],
    )
    return {"posting_profile": profile}
