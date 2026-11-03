"""슬롯 배정(규칙) + 면접 질문(LLM) — PR-B. 가상 데이터만."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from task_spec.llm import Completion, FakeBackend
from task_spec.llm.backend import TransientLLMError
from task_spec.nodes.pseudonymize import mint_pseudonymized
from task_spec.ontology.relatedness import Relatedness
from task_spec.prompting import PromptRegistry
from task_spec.schemas import (
    ApprovalDecision,
    Claim,
    Comparison,
    Observable,
    RunState,
    SlotQuestionsOutput,
    TargetCompetency,
    TaskItem,
    VerifyTarget,
)
from task_spec.spec import questions as q_mod
from task_spec.spec.assign import BASIS_NONE, BASIS_REVIEWER, assign_slots
from task_spec.spec.questions import (
    FLAG_NO_SLOT,
    FLAG_Q_FAILED,
    FLAG_UNAPPROVED_SLOT,
    build_slot_verifications,
    check_questions,
    make_questions,
)
from tests.spec.test_draft import _client, _ctx

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
QUOTE = "[ORG_1] 에서 수집기 고루틴 구조를 바꿔 지연을 40% 줄였다."


def _slot(sid: str, comp: str) -> TaskItem:
    return TaskItem(
        task_id=sid,
        track="extension_slot",
        target_competencies=[TargetCompetency(competency_id=comp, label=f"역량 {comp}")],
        tech_env=_ctx().env,
        difficulty_target="medium",
        time_limit_min=30,
        title=f"슬롯 {sid}",
        scenario="같은 시스템에서 이번에는 …",
        observables=[Observable(kind="first_mention_of", value=f"{sid} 핵심 개념")],
    )


def _vt(cid: str, comps: list[str], score: float = 5.0) -> VerifyTarget:
    return VerifyTarget(claim_id=cid, competency_ids=comps, score=score)


SLOTS = [_slot("s1", "comp:go"), _slot("s2", "comp:api"), _slot("s3", "comp:cs")]


def test_assign_same_competency_then_related_then_none() -> None:
    rel = Relatedness(pairs={"comp:grpc": {"comp:api"}})
    out = assign_slots(
        [_vt("c1", ["comp:go"]), _vt("c2", ["comp:grpc"]), _vt("c3", ["comp:mkt"])], SLOTS, rel
    )
    assert [(a.claim_id, a.slot_id) for a in out] == [("c1", "s1"), ("c2", "s2"), ("c3", None)]
    assert out[0].basis.startswith("역량 같음") and "comp:grpc ↔ comp:api" in out[1].basis
    assert out[2].basis == BASIS_NONE


def test_one_slot_per_claim_first_come_first_served() -> None:
    out = assign_slots([_vt("c1", ["comp:go"]), _vt("c2", ["comp:go"])], SLOTS)
    assert [a.slot_id for a in out] == ["s1", None]  # 점수 높은(먼저 고른) 주장이 가져감


def test_reviewer_reassign_overrides_rules() -> None:
    out = assign_slots(
        [_vt("c1", ["comp:go"]), _vt("c2", ["comp:api"])],
        SLOTS,
        reassign={"c1": "s3", "c2": None},
    )
    assert [(a.slot_id, a.basis) for a in out] == [("s3", BASIS_REVIEWER), (None, BASIS_REVIEWER)]
    bad = assign_slots([_vt("c1", ["comp:go"])], SLOTS, reassign={"c1": "s9"})
    assert bad[0].slot_id is None and "s9" in bad[0].basis


def test_reassign_only_on_revise() -> None:
    with pytest.raises(ValueError):
        ApprovalDecision(
            status="approved",
            reviewer="r",
            reassign={"c1": "s1"},
            at=datetime(2026, 11, 3, tzinfo=UTC),
        )


def _q(**over: Any) -> dict[str, Any]:
    d = {
        "link_text": "수집기 동시성 구조 개편",
        "follow_up_questions": [
            "고루틴 수는 어떻게 정했나요?",
            "버퍼와 워커 중 무엇을 먼저 바꿨나요?",
        ],
    }
    d.update(over)
    return d


def test_check_questions() -> None:
    ok = SlotQuestionsOutput.model_validate(_q())
    assert check_questions(ok, QUOTE, ["s1 핵심 개념"]) == []
    bad = SlotQuestionsOutput.model_validate(
        _q(
            link_text="[ORG_1] 에서 지연 40% 감소",
            follow_up_questions=["지연을 40% 줄였다는데?", "s1 핵심 개념은?"],
        )
    )
    text = "\n".join(check_questions(bad, QUOTE, ["s1 핵심 개념"]))
    assert "원문" in text and "수치" in text
    leak = SlotQuestionsOutput.model_validate(_q(link_text="s1 핵심 개념 해결 경험"))
    assert any("정답 개념" in p for p in check_questions(leak, QUOTE, ["s1 핵심 개념"]))


async def test_make_questions_reasks_then_ok_and_failure_flag() -> None:
    fake = FakeBackend().push(
        Completion(json.dumps(_q(link_text="지연 40% 감소"), ensure_ascii=False)),
        Completion(json.dumps(_q(), ensure_ascii=False)),
    )
    res = await make_questions(
        "c1",
        mint_pseudonymized(QUOTE),
        QUOTE,
        "Go 동시성",
        SLOTS[0],
        [],
        llm=_client(fake),
        prompts=PROMPTS,
        ontology_version="v",
    )
    assert len(fake.calls) == 2 and res.flags == [] and res.output is not None
    dead = FakeBackend().push(*[TransientLLMError("InternalServerError")] * 8)
    res2 = await make_questions(
        "c1",
        mint_pseudonymized(QUOTE),
        QUOTE,
        "Go",
        None,
        [],
        llm=_client(dead),
        prompts=PROMPTS,
        ontology_version="v",
    )
    assert res2.output is None and res2.flags == [FLAG_Q_FAILED]


async def test_build_slot_verifications_items_only_for_slotted_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def texts(state: RunState, ids: list[str]) -> tuple[dict[str, Any], list[Any]]:
        return {i: mint_pseudonymized(QUOTE) for i in ids}, []

    monkeypatch.setattr(q_mod, "_claim_texts", texts)
    common = TaskItem(
        task_id="ct-1",
        track="common",
        target_competencies=[TargetCompetency(competency_id="comp:java", label="Java")],
        tech_env=_ctx().env,
        difficulty_target="high",
        time_limit_min=60,
        slots=SLOTS,
        observables=[Observable(kind="first_mention_of", value="공통 핵심")],
    )
    claims = [Claim(claim_id=c, doc_id="d1", action="가상 행동", quote=QUOTE) for c in ("c1", "c2")]
    state = RunState(
        run_id="r",
        ontology_version="v",
        posting_id="p",
        claims=claims,
        comparison=Comparison(
            claims_to_verify=[_vt("c1", ["comp:go"], 6), _vt("c2", ["comp:mkt"], 5)]
        ),
    )
    fake = FakeBackend().push(*[Completion(json.dumps(_q(), ensure_ascii=False))] * 2)
    assignments, items, prov, errors = await build_slot_verifications(
        state, common, spec_id="r-r1", llm=_client(fake), prompts=PROMPTS, slots_approved=False
    )
    assert [(a.claim_id, a.slot_id) for a in assignments] == [("c1", "s1"), ("c2", None)]
    assert all(a.follow_up_questions for a in assignments) and len(prov) == 2
    assert FLAG_NO_SLOT in assignments[1].review_flags
    assert len(items) == 1 and items[0].slot_id == "s1" and items[0].parent_task_id == "ct-1"
    assert items[0].link_text == "수집기 동시성 구조 개편" and items[0].time_limit_min == 30
    assert FLAG_UNAPPROVED_SLOT in items[0].review_flags  # 승인 전 슬롯
