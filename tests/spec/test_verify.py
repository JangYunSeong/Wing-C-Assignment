"""경험 검증 확장 문항 — 주장 고르기·기술 환경·난이도·자동 검증(베끼기 포함). 가상 데이터만."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.nodes.pseudonymize import mint_pseudonymized
from task_spec.nodes.spec import make_draft_spec
from task_spec.prompting import PromptRegistry
from task_spec.schemas import ApprovalDecision, TaskItem, TechEnv, VerifyTarget, VerifyTaskOutput
from task_spec.spec.store import InMemoryCommonStore
from task_spec.spec.verify import (
    FLAG_VERIFY_INVALID,
    VerifyContext,
    build_input,
    check,
    draft_verify,
    item_difficulty,
    pick_targets,
    repair,
    verify_tech_env,
)
from tests.spec.test_draft import _ctx as _draft_ctx
from tests.spec.test_store import _state

LANGS = ["C", "C++", "Go", "Java", "Python", "R", "Rust"]
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


def test_tech_env_intersects_claim_and_posting_languages() -> None:
    env = verify_tech_env("Go 로 수집기를 다시 짰다", ["Java", "Go"], LANGS)
    assert env.languages == ["Go"] and "∩" in env.basis
    env = verify_tech_env("Rust 로 다시 짰다", ["Java"], LANGS)  # 공고 밖 언어 → 공고 언어
    assert env.languages == ["Java"] and "공고 언어" in env.basis


def test_difficulty_follows_posting_level_not_role() -> None:
    assert item_difficulty(["수행"]) == "high"
    assert item_difficulty(["활용", "이해"]) == "medium"
    assert item_difficulty([]) == "medium"  # 공고 밖 역량
    assert item_difficulty(["언급"]) == "low"


def _ctx() -> VerifyContext:
    d = _draft_ctx()
    return VerifyContext(
        claim_id="c1",
        claim_text=mint_pseudonymized(QUOTE),
        quote=QUOTE,
        targets=d.targets,
        criteria=d.criteria,
        env=TechEnv(languages=["Python"], basis="공고 언어"),
        difficulty="high",
        common_task="가상 공통 과제 제목\n가상 상황",
        sub_label=d.sub_label,
        industry_label=d.industry_label,
        ontology_version=d.ontology_version,
        language_labels=LANGS,
    )


def _out(**over: Any) -> VerifyTaskOutput:
    d: dict[str, Any] = {
        "title": "같은 시스템의 실시간 집계 전환 검토",
        "scenario": "같은 회사에서 이번에는 정산 데이터를 실시간으로 집계해야 한다. 지원서에 적은 경험(배치 전환)을 살려 답하라.",
        "deliverables": ["전환 계획 메모", "AI 에게 맡긴 부분과 직접 판단한 부분"],
        "observables": [{"kind": "first_mention_of", "value": "중복 처리"}],
        "behavioral_anchors": {
            "level_1": {"behavior": "일반론만", "criterion_ids": ["E1.1#1"]},
            "level_3": {"behavior": "구체 절차", "criterion_ids": ["E2.1#1"]},
            "level_5": {"behavior": "대안 비교", "criterion_ids": ["E1.1#2"]},
        },
        "follow_up_questions": ["그때 버린 대안은 무엇이었나요?", "예상과 달랐던 점은?"],
        "tech_used": ["Python"],
    }
    d.update(over)
    return VerifyTaskOutput.model_validate(d)


def test_input_carries_pseudonymized_claim_only() -> None:
    inp = build_input(_ctx())
    assert inp.claim == "[ORG_1] 에서 주문 집계 배치를 스트리밍으로 바꿔 처리 시간을 45% 줄였다."
    assert inp.time_limit_min == 30


def test_check_passes_clean_item() -> None:
    assert check(_out(), _ctx()) == []


def test_check_catches_copying_tokens_numbers_and_long_spans() -> None:
    text = (
        "[ORG_1] 처럼 처리 시간을 45% 줄였다는 경험을 설명하라. 주문 집계 배치를 스트리밍으로 바꿔"
    )
    problems = check(_out(scenario=text), _ctx())
    copied = next(p for p in problems if "원문" in p)
    assert "가명화 토큰" in copied and "45" in copied and "같은 문장" in copied


def test_check_rejects_traps_style_observables_and_bad_ids() -> None:
    anchors = _out().behavioral_anchors.model_dump()
    anchors["level_3"]["criterion_ids"] = ["E9.9#9"]
    out = _out(
        observables=[{"kind": "catches_trap", "value": "t1"}],
        behavioral_anchors=anchors,
    )
    text = "\n".join(check(out, _ctx()))
    assert "first_mention_of 만" in text and "E9.9#9" in text
    fixed, ok = repair(out, _ctx())
    assert not ok  # level_3 근거가 비어 행동 기준을 비운다
    assert fixed.observables  # 관찰 행동은 비우지 않는다


def test_follow_up_questions_must_not_quote_claim() -> None:
    out = _out(follow_up_questions=["처리 시간을 45% 줄인 방법은?", "다른 방법은?"])
    assert any("수치 45" in p for p in check(out, _ctx()))


# ---------------------------------------------------------------- LLM (FakeBackend)


PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


async def _no_sleep(_: float) -> None:
    return None


def _client(fake: FakeBackend) -> LLMClient:
    return LLMClient({"opencode": fake, "openai": fake}, sleep=_no_sleep)


def _json(**over: Any) -> str:
    return json.dumps(_out(**over).model_dump(mode="json"), ensure_ascii=False)


async def test_draft_verify_reasks_on_copy_then_ok() -> None:
    fake = FakeBackend().push(
        Completion(_json(scenario="[ORG_1] 에서 처리 시간을 45% 줄인 경험을 설명하라.")),
        Completion(_json()),
    )
    res = await draft_verify(_ctx(), llm=_client(fake), prompts=PROMPTS)
    assert len(fake.calls) == 2 and res.flags == [] and res.output is not None
    assert any("원문" in p for p in res.first_problems)


async def test_draft_verify_copy_twice_is_flagged_for_approver() -> None:
    bad = _json(scenario="[ORG_1] 에서 처리 시간을 45% 줄인 경험을 설명하라.")
    fake = FakeBackend().push(Completion(bad), Completion(bad))
    res = await draft_verify(_ctx(), llm=_client(fake), prompts=PROMPTS)
    assert res.output is not None and res.flags[0].startswith(FLAG_VERIFY_INVALID)


# ---------------------------------------------------------------- revise_target (노드)


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
