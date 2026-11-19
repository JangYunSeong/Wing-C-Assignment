"""사례 근거 검사·태그·고친 사례 (#262) — 포크 test_grounding_blocks_… 를 옮기고 이 레포 규칙을 더했다. 가상 데이터만."""

from __future__ import annotations

import pytest

from task_spec.interview.incident import edited_incident, prepare_incident
from task_spec.interview.text import numbers, parse_project, posting_team_context
from task_spec.schemas import KnownIncidentCase
from tests.interview.conftest import FACTS, PROJECT, case, complete_output

KWARGS = dict(
    incident_id="co:sw.backend:0123456789ab", sub="sw.backend", source=PROJECT, check=lambda t: t
)


def test_grounding_blocks_fake_quotes_unknown_cause_and_invented_numbers() -> None:
    bad = complete_output(evidence={k: ["존재하지 않는 근거"] for k in FACTS})
    assert prepare_incident(bad, **KWARGS)[0] is None
    unknown = {k: [v] for k, v in FACTS.items()}
    unknown["root_cause"] = ["원인은 모름"]
    assert (
        prepare_incident(
            complete_output(evidence=unknown), **{**KWARGS, "source": PROJECT + "\n원인은 모름"}
        )[0]
        is None
    )
    undecided = {**{k: [v] for k, v in FACTS.items()}, "symptom": ["미정"]}
    draft, missing, question = prepare_incident(
        complete_output(evidence=undecided), **{**KWARGS, "source": PROJECT + "\n미정"}
    )
    assert (
        draft is None and missing == ["symptom"] and question
    )  # 모델 질문이 없으면 칸별 고정 질문
    invented = complete_output(incident=case().model_copy(update={"symptom": "동시 요청 999개"}))
    with pytest.raises(ValueError, match="수치"):
        prepare_incident(invented, **KWARGS)


def test_server_sets_identity_and_checks_every_model_text() -> None:
    draft, missing, _ = prepare_incident(complete_output(), **KWARGS)
    assert draft is not None and missing == []
    assert (
        draft.incident_id == KWARGS["incident_id"] and draft.org == "[ORG_1]" and not draft.verified
    )
    assert draft.url == draft.published == "" and draft.known_incident_cases == []
    seen: list[str] = []

    def check(text: str) -> str:
        seen.append(text)
        return text

    prepare_incident(complete_output(), **{**KWARGS, "check": check})
    assert FACTS["root_cause"] in seen and "요청 식별자의 검증 여부" in seen  # 내용 칸 전부 검사

    def reject(text: str) -> str:
        raise ValueError("검사 거절")

    with pytest.raises(ValueError, match="검사 거절"):
        prepare_incident(complete_output(), **{**KWARGS, "check": reject})


def test_token_numbers_are_not_counted_as_invented_numbers() -> None:
    assert numbers("[PERSON_12] 님과 [ORG_3] 의 3개 요청") == {"3"}
    with_token = case().model_copy(
        update={"root_cause": "[PERSON_1] 님이 요청 식별자 검증 누락을 코드에서 확인"}
    )
    output = complete_output(incident=with_token)
    assert prepare_incident(output, **{**KWARGS, "source": PROJECT + "\n[PERSON_1]"})[0] is not None


def test_project_tags_ignore_lines_that_are_only_a_token() -> None:
    tags = parse_project("[개요]\n가상 서비스\n[PERSON_1]\n[알려진 문제]\n중복 저장")
    assert tags.overview == "가상 서비스\n[PERSON_1]" and tags.known_problem == "중복 저장"
    assert parse_project("태그 없는 설명").overview == "태그 없는 설명"
    with pytest.raises(ValueError):
        parse_project("앞 설명\n[개요]\n가상")
    with pytest.raises(ValueError):
        parse_project("[모르는 태그]\n가상")


def test_posting_team_context_reads_labelled_lines() -> None:
    ctx = posting_team_context(
        "- 90일 기대 성과: 중복 저장 0건\n판단 상황과 기준: 미정\n입사 후 3개월 안에 결제 API 를 맡는다"
    )
    assert ctx.expected_outcome_90_days == "중복 저장 0건" and ctx.decision_situation is None


def _with_cases(answer: str = "기준 답안") -> object:
    return case().model_copy(
        update={
            "incident_id": "co:sw.backend:0123456789ab",
            "known_incident_cases": [
                KnownIncidentCase(
                    case_id="00000000-0000-4000-8000-000000000001",
                    interview_text="관찰된 증상",
                    answer_text=answer,
                    is_trap=False,
                )
            ],
        }
    )


def test_edited_incident_pseudonymizes_only_changed_text_and_guards_identity() -> None:
    draft = _with_cases()
    calls: list[str] = []

    def pseudonymize(text: str) -> str:
        calls.append(text)
        return text.replace("가상테크", "[ORG_1]")

    edited = draft.model_copy(update={"title": "가상테크 중복 저장"})
    result = edited_incident(edited, draft, pseudonymize=pseudonymize)
    assert result.title == "[ORG_1] 중복 저장" and calls == ["가상테크 중복 저장"]
    with pytest.raises(ValueError, match="변경할 수 없습니다"):
        edited_incident(
            draft.model_copy(update={"sub": "sw.frontend"}), draft, pseudonymize=pseudonymize
        )
    with pytest.raises(ValueError, match="기준 답안"):
        edited_incident(_with_cases(answer=""), draft, pseudonymize=pseudonymize)
