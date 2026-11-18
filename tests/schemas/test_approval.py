from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from task_spec.schemas import ApprovalDecision

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def test_approved_ok() -> None:
    d = ApprovalDecision(status="approved", reviewer="reviewer-fake", at=NOW)
    assert d.status == "approved"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "auto_approved", "reviewer": "r", "at": NOW},  # 없는 상태
        {"status": "approved", "reviewer": "", "at": NOW},  # 승인자 없음
        {"status": "approved", "reviewer": "   ", "at": NOW},  # 공백 승인자
        {"status": "revise", "reviewer": "r", "at": NOW},  # revise 인데 사유 없음
        {"status": "approved", "reviewer": "r", "at": datetime(2026, 11, 2)},  # 시간대 없음
        {"status": "approved", "reviewer": "r", "at": NOW, "extra": 1},  # 모르는 필드
    ],
)
def test_invalid_decisions_rejected(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        ApprovalDecision(**kwargs)


async def test_saved_common_revise_with_reassign_still_becomes_state() -> None:
    """공통 과제만 다시 만드는 수정 요청에 슬롯 배정 바꾸기(reassign)가 함께 든 결정은 막지 않는다 (#249) —
    draft_spec 이 반영하지 않고 reassign_ignored 를 남긴다 (tests/spec/test_verify.py).

    막지 않는 이유: 결정에 검증기를 더하면 이미 저장된 체크포인트가 깨진다. LangGraph 역직렬화는 검증이 실패하면
    model_construct 로 되살리지만, 노드 입력을 만들 때(RunState(**values)) mode="after" 모델 검증기가
    인스턴스에도 다시 돌아 실패한다. 이 테스트가 깨지면 그 경로를 다시 확인할 것."""
    from langgraph.checkpoint.base import empty_checkpoint

    from task_spec.graph.checkpointer import make_checkpointer
    from task_spec.schemas import RunState

    saved = ApprovalDecision(
        status="revise",
        reviewer="reviewer-fake",
        comment="가상 사유",
        revise_target="common",
        reassign={"c1": "s1"},
        at=NOW,
    )
    async with make_checkpointer(kind="memory") as cp:  # 프로젝트 직렬화기로 저장 → 읽기
        cfg = {"configurable": {"thread_id": "r-saved", "checkpoint_ns": ""}}
        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"approval": saved}
        ckpt["channel_versions"] = {"approval": 1}
        meta = {"source": "loop", "step": 3, "parents": {}}
        got = await cp.aget_tuple(await cp.aput(cfg, ckpt, meta, {"approval": 1}))  # type: ignore[arg-type]
    assert got is not None
    loaded = got.checkpoint["channel_values"]["approval"]
    assert isinstance(loaded, ApprovalDecision) and loaded.reassign == {"c1": "s1"}
    state = RunState(run_id="r-saved", ontology_version="v", posting_id="p", approval=loaded)
    assert state.approval == saved  # 노드 입력(State)을 만들 때도 실패하지 않는다
