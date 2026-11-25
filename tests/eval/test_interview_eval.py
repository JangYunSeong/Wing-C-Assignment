"""사례 인터뷰 평가 실행기 자기 검증 (#263) — 모범 응답(가짜 LLM)이면 만점, 지표 계산이 맞는지. 가상 데이터만."""

from __future__ import annotations

from eval.interview.run_eval import (
    DEFAULT_VERSIONS,
    Result,
    infer_field,
    leading_in,
    run_dialogue,
    summarize,
)
from eval.interview.samples import DIALOGUES, TEAM, Dialogue, Trap, dialogues
from task_spec.llm import LLMClient
from task_spec.llm.backend import Completion
from task_spec.nodes.pseudonymize.engine import Detectors
from task_spec.schemas.interview_incident import IncidentInterviewOutput
from tests.interview.conftest import (
    FACTS,
    SUCCESS,
    CapturingBackend,
    answer_output,
    check_output,
    complete_output,
)
from tests.interview.conftest import detectors as detectors  # noqa: F401 — pytest 픽스처

SELF = Dialogue(
    id="self-check",
    split="dev",
    sub="sw.backend",
    company="(주)가상테크",
    title="백엔드 개발자",
    jd=(("담당업무", "가상 배송 서비스의 상태 기록 API 개발"), *TEAM),
    opening=FACTS["symptom"] + " " + FACTS["conditions"],
    facts={k: FACTS[k] for k in ("symptom", "root_cause", "accepted_fixes", "conditions")},
    trap=Trap(FACTS["naive_fix"], "복구가 막힘", FACTS["why_naive_fails"]),
    success=SUCCESS,
    clarify="그 밖의 조건은 위와 같습니다.",
    leading=("식별자",),
)


def test_samples_are_fictional_and_split() -> None:
    assert len(dialogues("dev")) == 5 and len(dialogues("test")) == 5
    assert len({d.id for d in DIALOGUES}) == len(DIALOGUES)
    for d in DIALOGUES:
        assert d.company.startswith("(주)가상") and set(d.facts) == {
            "symptom",
            "root_cause",
            "accepted_fixes",
            "conditions",
        }
        text = d.opening + " ".join(d.facts.values())
        assert "@" not in text or "example.com" in text


def test_infer_field_and_leading() -> None:
    assert infer_field(
        "원인을 좁힐 때 확인한 로그는 무엇이었나요?", ["root_cause", "conditions"]
    ) == (
        "root_cause",
        True,
    )
    assert infer_field("더 알려 주세요", ["conditions", "accepted_fixes"]) == (
        "accepted_fixes",
        False,
    )
    assert leading_in("요청 식별 키가 없었나요?", "중복 결제", ("식별 키",)) == ["식별 키"]
    assert leading_in("요청 식별 키가 없었나요?", "요청 식별 키 가 없어서", ("식별 키",)) == []


async def test_perfect_model_scores_full_marks(tmp_path, detectors: Detectors) -> None:
    backend = CapturingBackend()
    ask = IncidentInterviewOutput(
        missing_fields=["root_cause"], question="원인은 어떻게 확인했나요?"
    )
    # 질문 → 초안(해결 조치 인용이 아직 없어 다시 물음) → 초안(처음 대응만 빠짐) → 처음 대응 반영 → 기준 답안 → 검증
    outputs = (
        ask,
        complete_output(),
        complete_output(),
        complete_output(),
        answer_output(),
        check_output(),
    )
    for output in outputs:
        backend.push(Completion(output.model_dump_json()))
    result = await run_dialogue(
        SELF, llm=LLMClient({"opencode": backend}), detectors=detectors, versions=DEFAULT_VERSIONS
    )
    assert result.failure == "" and result.completed and result.end_stage == "competencies"
    assert result.reasks == [] and result.leading == [] and result.unavailable == 0
    assert result.validation_passed is True and result.incident_turns == 2
    assert len(backend.prompts) == len(outputs)
    summary = summarize([result])
    assert summary["passed"] and summary["completion"] == 1.0


async def test_retries_and_failures_are_counted(tmp_path, detectors: Detectors) -> None:
    backend = CapturingBackend()
    for _ in range(3):  # 같은 단계를 2번까지 다시 보낸 뒤 포기
        backend.push(Completion("broken"), Completion("broken"))
    result = await run_dialogue(
        SELF, llm=LLMClient({"opencode": backend}), detectors=detectors, versions=DEFAULT_VERSIONS
    )
    assert not result.completed and result.unavailable == 3 and result.llm_steps == 3
    assert "InterviewUnavailable" in result.failure
    summary = summarize([result, Result(id="x", tags=[], completed=True, llm_steps=1)])
    assert summary["completion"] == 0.5 and not summary["passed"]
    assert summary["criteria"]["unavailable_ratio"]["pass"] is False


async def test_a_return_from_trap_to_incident_is_not_a_failure(
    tmp_path, detectors: Detectors
) -> None:
    """#308 할 일 (#300·#307): ④ 처음 대응을 낸 뒤 다른 칸이 빠졌으면 서비스가 ③ 으로 돌려보낸다. 평가 실행기는 ⑤ 만 기다려
    '끝 단계 incident_interview' 실패로 셌다 — 빠진 칸에 다시 답하고 이어 가며 복귀 횟수를 남긴다."""
    backend = CapturingBackend()
    ask = IncidentInterviewOutput(
        missing_fields=["root_cause"], question="원인은 어떻게 확인했나요?"
    )
    # 질문 → 초안(다시 물음) → 초안(처음 대응만 빠짐 → ④) → ④ 뒤 다시 질문(→ ③) → 초안 → 기준 답안 → 검증
    outputs = (
        ask,
        complete_output(),
        complete_output(),
        ask,
        complete_output(),
        answer_output(),
        check_output(),
    )
    for output in outputs:
        backend.push(Completion(output.model_dump_json()))
    result = await run_dialogue(
        SELF, llm=LLMClient({"opencode": backend}), detectors=detectors, versions=DEFAULT_VERSIONS
    )
    assert result.failure == "" and result.completed and result.end_stage == "competencies"
    assert result.trap_returns == 1
    assert summarize([result])["trap_returns"] == 1
