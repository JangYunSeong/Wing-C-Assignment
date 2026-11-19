"""① 공고 프로필 — 결정적, LLM 없음.

- 경로 1: 온톨로지 포지션 (검수됨, source=ontology)
- 경로 2: 임시 공고 `prov-…` (자동 분석 + 요청자 확인, 미검수, source=provisional).
  분석 당시 온톨로지 버전이 실행 버전과 다르면 멈춘다 (불변식 4 — 다른 버전 규칙의 분석과 근거를 섞지 않는다).
- 찾지 못하면 오류(posting_not_found) → 그래프 END. 역량 0개·셀 없음은 경고만 남기고 진행.
- 공고의 채용담당자 인터뷰 과제가 다른 온톨로지 판으로만 승인돼 있으면 멈춘다 (interview_task_version_mismatch) —
  그 과제를 이 판 실행에 쓸 수 없고, 공개 사례 과제로 바꿔 만들지도 않는다 [확정: 2026-11-19 사용자].
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
from task_spec.spec.store import CommonTaskRepo

NODE = "posting_profile"


def _err(code: str, message: str, ref: str) -> NodeError:
    return NodeError(node=NODE, code=code, message=message, ref_id=ref)


def make_posting_profile(
    ontology: OntologyRepository | None,
    store: PostingStore | None = None,
    lists: OntologyLists | None = None,
    common: CommonTaskRepo | None = None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def posting_profile(state: RunState) -> dict[str, Any]:
        pid = state.posting_id
        if common is not None:
            versions = await common.interview_versions(pid)
            if versions and state.ontology_version not in versions:
                return {
                    "errors": [
                        _err(
                            "interview_task_version_mismatch",
                            f"공고의 인터뷰 과제가 다른 온톨로지 판({', '.join(versions)})으로만 승인됨 — "
                            "인터뷰에서 실행 판으로 다시 승인해야 함",
                            pid,
                        )
                    ]
                }
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
            lists is not None
            and profile.cell_id is not None
            and profile.cell_id not in lists.cells_with_ncs
        ):
            # 온톨로지에 NCS 목록이 없는 직군×업종 셀 — 평가 근거를 직군 단위(sub_ncs)로 대체한다 (덜 정밀)
            warnings.append(
                _err(
                    "posting_cell_unseen",
                    "온톨로지에 NCS 목록이 없는 직군×업종 셀 — 평가 근거를 직군 단위 NCS 로 대체함",
                    pid,
                )
            )
        if profile.cell_id is None:
            warnings.append(_err("posting_no_cell", "직군 또는 업종이 없어 셀을 정할 수 없음", pid))
        return {"posting_profile": profile, "errors": warnings}

    return posting_profile
