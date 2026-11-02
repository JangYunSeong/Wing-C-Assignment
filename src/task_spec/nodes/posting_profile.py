"""① 공고 프로필 — 결정적, LLM 없음.

- 경로 1: 온톨로지 포지션 (검수됨, source=ontology)
- 경로 2: 임시 공고 `prov-…` (자동 분석 + 요청자 확인, 미검수, source=provisional).
  분석 당시 온톨로지 버전이 실행 버전과 다르면 멈춘다 (불변식 4 — 다른 버전 규칙의 분석과 근거를 섞지 않는다).
- 찾지 못하면 오류(posting_not_found) → 그래프 END. 역량 0개·셀 없음은 경고만 남기고 진행.
온톨로지가 없으면(테스트·골격) 빈 프로필.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from task_spec.ontology.queries import posting_profile as query_profile
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.store import PostingStore, is_provisional, provisional_profile
from task_spec.postings.suggest import OntologyLists
from task_spec.schemas import NodeError, PostingProfile, RunState

NODE = "posting_profile"


def _err(code: str, message: str, ref: str) -> NodeError:
    return NodeError(node=NODE, code=code, message=message, ref_id=ref)


def make_posting_profile(
    ontology: OntologyRepository | None,
    store: PostingStore | None = None,
    lists: OntologyLists | None = None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def posting_profile(state: RunState) -> dict[str, Any]:
        pid = state.posting_id
        if ontology is None:
            return {
                "posting_profile": PostingProfile(
                    posting_id=pid, ontology_version=state.ontology_version
                )
            }

        profile: PostingProfile | None
        if is_provisional(pid):
            row = await store.get_provisional(pid) if store is not None else None
            if row is None or lists is None:
                return {"errors": [_err("posting_not_found", "임시 공고를 찾을 수 없음", pid)]}
            if row["ontology_version"] != state.ontology_version:
                return {
                    "errors": [
                        _err(
                            "posting_version_mismatch",
                            "임시 공고 분석 버전과 실행 온톨로지 버전이 다름",
                            pid,
                        )
                    ]
                }
            profile = provisional_profile(row, lists)
        else:
            profile = await query_profile(ontology, pid)
            if profile is None:
                return {"errors": [_err("posting_not_found", "온톨로지에 없는 공고", pid)]}

        warnings = []
        if not profile.competencies:
            warnings.append(
                _err(
                    "posting_no_competencies",
                    "공고에서 역량을 찾지 못함 — 평가 근거가 비게 된다",
                    pid,
                )
            )
        if (
            profile.source == "provisional"
            and lists is not None
            and profile.cell_id not in lists.cells_with_ncs
        ):
            # 온톨로지에 공고가 없던 직군×업종 조합 — 셀 범위 NCS 가 없어 평가 근거가 빈다 (보완 방식 [미정])
            warnings.append(
                _err(
                    "posting_cell_unseen", "온톨로지에 없는 직군×업종 조합 — 셀 범위 NCS 없음", pid
                )
            )
        if profile.cell_id is None:
            warnings.append(_err("posting_no_cell", "직군 또는 업종이 없어 셀을 정할 수 없음", pid))
        return {"posting_profile": profile, "errors": warnings}

    return posting_profile
