"""경험 검증 — 주장 고르기·난이도·원문 베끼기 검사·revise 대상. 가상 데이터만.

지원자별 확장 문항 서술(verify_task)은 폐기됨 — 배정·면접 질문은 test_assign.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from task_spec.nodes.spec import make_draft_spec
from task_spec.schemas import ApprovalDecision, TaskItem, VerifyTarget
from task_spec.spec.store import InMemoryCommonStore
from task_spec.spec.verify import copied, item_difficulty, pick_targets
from tests.spec.test_store import _state

QUOTE = "[ORG_1] 에서 주문 집계 배치를 스트리밍으로 바꿔 처리 시간을 45% 줄였다."


def _vt(cid: str, comps: list[str], score: float) -> VerifyTarget:
    return VerifyTarget(claim_id=cid, competency_ids=comps, score=score)


def test_pick_penalizes_common_overlap_and_keeps_competencies_distinct() -> None:
    vts = [
        _vt("c1", ["comp:a"], 6.0),  # 공통 과제 역량 → 6.0 - 1.5 = 4.5
        _vt("c2", ["comp:x"], 5.0),
        _vt("c3", ["comp:x"], 4.9),  # c2 와 역량이 겹쳐 제외
        _vt("c4", ["comp:y"], 4.0),
    ]
    assert [t.claim_id for t in pick_targets(vts, {"comp:a"})] == ["c2", "c1"]
    # 겹쳐도 점수가 충분히 높으면 고른다 (빼지 않음)
    assert pick_targets([_vt("c1", ["comp:a"], 9.0)], {"comp:a"})[0].claim_id == "c1"


def test_difficulty_follows_posting_level_not_role() -> None:
    assert item_difficulty(["수행"]) == "high"
    assert item_difficulty(["활용", "이해"]) == "medium"
    assert item_difficulty([]) == "medium"  # 공고 밖 역량
    assert item_difficulty(["언급"]) == "low"


def test_copy_check_catches_tokens_numbers_and_long_spans() -> None:
    text = "[ORG_1] 처럼 처리 시간을 45% 줄였다는 경험. 주문 집계 배치를 스트리밍으로 바꿔"
    found = copied(text, QUOTE)
    assert (
        "가명화 토큰" in found
        and any("45" in x for x in found)
        and any("같은 문장" in x for x in found)
    )
    assert copied("수집기 동시성 구조를 어떻게 바꿨나요?", QUOTE) == []


async def test_revise_common_only_keeps_verification_items() -> None:
    store = InMemoryCommonStore()
    draft = make_draft_spec(None, store=store)
    s1 = (await draft(_state("run-a")))["spec"]
    old = TaskItem(
        task_id="run-a-r1-verify-1",
        track="claim_verification",
        target_competencies=s1.common_tasks[0].target_competencies,
        claims_to_verify=["c1"],
        parent_task_id=s1.common_tasks[0].task_id,
        tech_env=s1.common_tasks[0].tech_env,
        difficulty_target="medium",
        time_limit_min=30,
    )
    s1 = s1.model_copy(update={"verification_tasks": [old]})
    decision = ApprovalDecision(
        status="revise",
        reviewer="reviewer-fake",
        comment="공통 과제 자료를 줄여 주세요",
        revise_target="common",
        at=datetime(2026, 11, 3, tzinfo=UTC),
    )
    s2 = (await draft(_state("run-a", spec=s1, approval=decision)))["spec"]
    assert s2.common_refs[0].revision == 2  # 공통 과제는 새 판
    assert [v.claims_to_verify for v in s2.verification_tasks] == [["c1"]]  # 확장 문항 유지
    assert s2.verification_tasks[0].parent_task_id == s2.common_tasks[0].task_id


async def test_revise_verification_only_reuses_common() -> None:
    store = InMemoryCommonStore()
    draft = make_draft_spec(None, store=store)
    s1 = (await draft(_state("run-a")))["spec"]
    decision = ApprovalDecision(
        status="revise",
        reviewer="reviewer-fake",
        comment="확장 문항을 더 구체적으로",
        revise_target="verification",
        at=datetime(2026, 11, 3, tzinfo=UTC),
    )
    s2 = (await draft(_state("run-a", spec=s1, approval=decision)))["spec"]
    assert s2.common_refs[0].reused and s2.common_refs[0].revision == 1  # 공통 과제 그대로
    assert len(store.rows) == 1
