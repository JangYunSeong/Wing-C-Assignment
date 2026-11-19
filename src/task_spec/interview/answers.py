"""기준 답안 초안·근거·해법·함정 판별 기록. 포크 interview_answers.py 에서 옮김 (#262).

포크와 다른 점: 원문 근거는 이미 가명화한 글이라 다시 정규식에 넣지 않는다. LLM 입력은 `stored_json`(가명화 모듈이 저장된
가명화 글을 다시 검사해 만든 PseudonymizedText), LLM 출력의 글은 `check`(패턴 탐지기·위조 토큰). 토큰 판별은 이 레포 7종.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any, Protocol, get_args

from task_spec.interview.errors import OutputRejected
from task_spec.interview.text import numbers
from task_spec.llm import LLMClient
from task_spec.llm.quotes import compact_for_quote, quote_in
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import Provenance, PublicIncident
from task_spec.schemas.interview_answer import (
    AnswerCheckTopic,
    AnswerValidationRecord,
    InterviewAnswerCheckInput,
    InterviewAnswerCheckOutput,
    InterviewAnswerGroundedInput,
    InterviewAnswerGroundedOutput,
)
from task_spec.schemas.interview_state import AnswerClarification, AnswerQuestion, InterviewState
from task_spec.schemas.pseudo_text import PseudonymizedText

ANSWER_PROMPT = "interview_answer"
CHECK_PROMPT = "interview_answer_check"
# 답안 줄 머리의 '[재현 조건]' 같은 절 표시 (가명화 토큰은 빼고)
_SECTION_HEAD = re.compile(
    r"(?m)^\s*\[(?!(?:PERSON|CONTACT|LINK|SCHOOL|ORG|PRODUCT|COURSE)_\d+\])[^\]]+\]\s*"
)


class StoredJson(Protocol):
    def __call__(self, value: Any, *, compact: bool = False) -> PseudonymizedText: ...


Check = Callable[[str], str]


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def answer_sources(state: InterviewState) -> list[dict[str, str]]:
    """원문 근거 — 담당자 진술만. 편집·생성된 사례와 질문 문장은 근거에서 뺀다."""
    project = state.project.model_dump()
    success = state.success_answers.model_dump() if state.success_answers else {}
    trap = state.trap_answers.model_dump() if state.trap_answers else {}
    texts = [
        *(project[k] for k in sorted(project)),
        *(m.text for m in state.incident_messages if m.role == "user"),
        *(success[k] for k in sorted(success)),
        *(trap[k] for k in sorted(trap) if isinstance(trap[k], str)),
        *(q.answer for q in state.answer_clarifications),
    ]
    return [
        {"source_id": f"source-{i}", "text": t}
        for i, t in enumerate(texts, 1)
        if isinstance(t, str) and t.strip()
    ]


def answer_probes(state: InterviewState) -> list[dict[str, str]]:
    # 기대 판정은 인터뷰에서 확인한 성공 조치·실패 시도에서 고정한다. 검증 모델에는 숨긴다.
    fixes = state.incident_evidence.get("accepted_fixes", [])
    probes = [{"probe_id": "p1", "proposal": "\n".join(fixes), "expected": "accept"}]
    trap = state.trap_answers
    if trap and not trap.unknown:
        probes.append({"probe_id": "p2", "proposal": trap.attempted_fix, "expected": "reject"})
    return probes


def validation_current(incident: PublicIncident, state: InterviewState) -> bool:
    record = state.answer_validation
    return bool(
        record
        and record.passed
        and not state.answer_questions
        and record.incident_sha256 == digest(incident.model_dump(mode="json"))
        and record.sources_sha256 == digest(answer_sources(state))
    )


def set_answer_questions(state: InterviewState, questions: list[str]) -> None:
    state.answer_questions = [
        AnswerQuestion(question_id="aq-" + hashlib.sha256(q.encode()).hexdigest()[:16], question=q)
        for q in dict.fromkeys(questions)
    ]
    state.answer_question = questions[0] if questions else ""


def collect_clarifications(
    state: InterviewState, replies: dict[str, str], pseudonymize: Check
) -> None:
    known = {q.question_id: q.question for q in state.answer_clarifications}
    known.update({q.question_id: q.question for q in state.answer_questions})
    if set(replies) - known.keys():
        raise ValueError("추가 질문이 변경됐습니다. 화면을 새로고침해 주세요")
    previous = {q.question_id: q for q in state.answer_clarifications}
    for key, raw in replies.items():
        text = pseudonymize(raw)
        if not text:
            raise ValueError("추가 질문에 답해 주세요. 모르면 미확인이라고 입력해 주세요")
        previous[key] = AnswerClarification(question_id=key, question=known[key], answer=text)
    state.answer_clarifications = list(previous.values())
    if len(previous) > 30:
        raise ValueError("추가 질문이 많습니다. 확인한 내용을 원래 성공 조건에 정리해 주세요")


def _checked(value: Any, check: Check) -> Any:
    if isinstance(value, str):
        return check(value)
    if isinstance(value, list):
        return [_checked(v, check) for v in value]
    if isinstance(value, dict):
        return {k: _checked(v, check) for k, v in value.items()}
    return value


async def draft_answer(
    incident: PublicIncident,
    state: InterviewState,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    stored_json: StoredJson,
) -> tuple[InterviewAnswerGroundedOutput, Provenance]:
    """성공 조건·처음 시도한 대응 → 기준 답안 초안 (또는 보충 질문)."""
    assert state.success_answers is not None and state.trap_answers is not None
    facts = {
        k: getattr(incident, k)
        for k in (
            "symptom",
            "root_cause",
            "accepted_fixes",
            "naive_fix",
            "why_naive_fails",
            "deep_structure",
        )
    }
    result = await llm.call(
        assemble(
            prompts.get(ANSWER_PROMPT),
            InterviewAnswerGroundedInput(
                incident_json=stored_json(facts),
                sources_json=stored_json({"sources": answer_sources(state)}),
                success_json=stored_json(
                    {
                        **state.success_answers.model_dump(),
                        "clarifications": [c.model_dump() for c in state.answer_clarifications],
                    }
                ),
                trap_json=stored_json(state.trap_answers.model_dump()),
            ),
            ontology_version=ontology_version,
        ),
        InterviewAnswerGroundedOutput,
    )
    return result.parsed, result.provenance


async def check_answer(
    incident: PublicIncident,
    state: InterviewState,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    stored_json: StoredJson,
    check: Check,
) -> AnswerValidationRecord:
    """기준 답안을 원문과 따로 대조한다 (별도 검토자 프롬프트). 서버가 근거 인용·판별·덮기를 다시 확인한다."""
    sources = answer_sources(state)
    source = "\n".join(s["text"] for s in sources)
    probes = answer_probes(state)
    if not probes[0]["proposal"] or not all(
        quote_in(q, source) for q in state.incident_evidence.get("accepted_fixes", [])
    ):
        raise OutputRejected("확인된 성공·실패의 근거가 없습니다. 사례 인터뷰를 보완해 주세요")
    result = await llm.call(
        assemble(
            prompts.get(CHECK_PROMPT),
            InterviewAnswerCheckInput(
                sources_json=stored_json(
                    {
                        "sources": sources,
                        "clarifications": [c.model_dump() for c in state.answer_clarifications],
                    }
                ),
                incident_json=stored_json(incident.model_dump(mode="json"), compact=True),
                probes_json=stored_json(
                    [{k: v for k, v in p.items() if k != "expected"} for p in probes]
                ),
            ),
            ontology_version=ontology_version,
        ),
        InterviewAnswerCheckOutput,
    )
    # 표시할 검증 결과도 저장 전에 검사한다.
    output = InterviewAnswerCheckOutput.model_validate(_checked(result.parsed.model_dump(), check))
    source_by_id = {s["source_id"]: s["text"] for s in sources}
    for claim in output.claims:
        if claim.source_ids:
            if any(key not in source_by_id for key in claim.source_ids):
                raise OutputRejected("검증 근거의 원문 ID가 없습니다")
            claim.evidence[:] = [source_by_id[key] for key in dict.fromkeys(claim.source_ids)]
    if len({c.topic for c in output.checks}) != 6 or {c.topic for c in output.checks} != set(
        get_args(AnswerCheckTopic)
    ):
        raise OutputRejected("검증 항목이 부족합니다")
    expected = {p["probe_id"]: p["expected"] for p in probes}
    if (
        len(output.probes) != len(expected)
        or {p.probe_id for p in output.probes} != expected.keys()
    ):
        raise OutputRejected("올바른 해법과 실패한 대응의 판별 결과가 부족합니다")
    answers = {c.is_trap: c.answer_text for c in incident.known_incident_cases}
    issues: list[str] = []
    if numbers("\n".join(answers.values())) - numbers(source):
        issues.append("인터뷰에서 확인하지 않은 수치가 기준 답안에 있습니다")
    if any(not q.strip() for q in output.questions):
        raise OutputRejected("추가 질문이 비어 있습니다")
    for claim in output.claims:
        if claim.is_trap not in answers or not quote_in(claim.text, answers[claim.is_trap]):
            raise OutputRejected("검증 대상 주장이 기준 답안에 없습니다")
        if claim.evidence and not all(quote_in(q, source) for q in claim.evidence):
            raise OutputRejected("검증 근거 인용이 인터뷰에 없습니다")
        if claim.status == "pass" and not claim.evidence:
            raise OutputRejected("근거 없는 주장은 통과시킬 수 없습니다")
    if {c.is_trap for c in output.claims} != answers.keys():
        raise OutputRejected("일부 기준 답안이 검증되지 않았습니다")

    def body(text: str) -> str:
        return compact_for_quote(_SECTION_HEAD.sub("", text))

    for is_trap, answer in answers.items():
        text = body(answer)
        covered = [False] * len(text)
        for claim in output.claims:
            if claim.is_trap != is_trap:
                continue
            fragment = body(claim.text)
            if not fragment:
                continue
            start = text.find(fragment)
            while start >= 0:
                covered[start : start + len(fragment)] = [True] * len(fragment)
                start = text.find(fragment, start + 1)
        if re.search(
            r"\w", "".join(char for char, found in zip(text, covered, strict=True) if not found)
        ):
            issues.append("주장별 검증에서 빠진 답안 문장이 있습니다")
    needs_info = (
        any(c.status == "needs_information" for c in output.claims)
        or any(c.status == "needs_information" for c in output.checks)
        or any(p.verdict == "needs_information" for p in output.probes)
    )
    if needs_info and not output.questions:
        raise OutputRejected("부족한 정보를 확인할 추가 질문이 없습니다")
    issues.extend(c.reason for c in output.claims if c.status != "pass")
    issues.extend(c.reason for c in output.checks if c.status != "pass")
    issues.extend(p.reason for p in output.probes if p.verdict != expected[p.probe_id])
    return AnswerValidationRecord(
        **output.model_dump(),
        incident_sha256=digest(incident.model_dump(mode="json")),
        sources_sha256=digest(sources),
        passed=not issues and not output.questions,
        issues=issues,
        provenance=result.provenance.model_dump(mode="json"),
        sources=sources,
    )
