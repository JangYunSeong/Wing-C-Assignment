"""인터뷰 사례 작성·근거 검사. 공개 사례를 복제하지 않는다. 포크 interview_incidents.py 에서 옮김 (#262).

포크와 다른 점:
- 정규식 가명화(sanitize_interview_text) 대신 — LLM 이 쓴 글은 `check`(패턴 탐지기·위조 토큰 검사),
  담당자가 고친 칸은 `pseudonymize`(탐지기 가명화)를 거친다. 둘 다 nodes/pseudonymize/interview.py.
- 사례 id 는 `pub:<직군>:interview-…` 이 아니라 회사 사례 id(`co:<직군>:<12자>`) — 인터뷰 동안 고정, D2 에서 그대로 등록.
- 숫자 대조는 이 레포 토큰 7종([PERSON_1]·[ORG_1]·[CONTACT_1] …)을 모두 빼고 센다.
- 옛 작업본 복구(recover_trap_step)는 옮기지 않았다 — 이 레포 DB 에는 포크 작업본이 없다.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable

from task_spec.interview.errors import OutputRejected
from task_spec.interview.text import numbers
from task_spec.llm.quotes import SECTION_MARKER, compact_for_quote, quote_in
from task_spec.schemas import KnownIncidentCase, PublicIncident
from task_spec.schemas.interview_incident import IncidentFact, IncidentInterviewOutput
from task_spec.schemas.interview_state import InterviewState
from task_spec.starter.templates import TEMPLATE_FOR_SUB

Clean = Callable[[str], str]

FACT_LABELS: dict[IncidentFact, str] = {
    "symptom": "관찰된 증상",
    "root_cause": "확인된 원인과 근거",
    "accepted_fixes": "실제 조치와 결과",
    "conditions": "발생 조건 (정확한 규모는 몰라도 됩니다)",
    "naive_fix": "흔한 대응",
    "why_naive_fails": "흔한 대응이 실패하는 이유",
}
FOLLOW_UP_QUESTIONS: dict[IncidentFact, str] = {
    "symptom": "문제가 생긴 화면이나 요청에서 기대한 결과와 실제 결과가 어떻게 달랐나요? 오류 문구나 구체적인 예가 있으면 알려주세요.",
    "root_cause": "원인을 좁힐 때 확인한 로그·코드나 비교 결과는 무엇이었나요? 그 근거로 어떤 가능성을 제외했나요?",
    "accepted_fixes": "어떤 부분을 바꿨고, 같은 조건에서 다시 확인했을 때 결과가 어떻게 달라졌나요?",
    "naive_fix": "처음 시도한 대응은 무엇이었고, 시도한 뒤 어떤 결과를 보셨나요?",
    "why_naive_fails": "처음 시도한 대응이 문제를 해결하지 못했다고 판단한 근거는 무엇인가요?",
    "conditions": "문제가 생긴 경우와 정상인 경우를 비교하면 어떤 조건이 달랐나요? 동시에 처리한 작업이나 재시도 여부처럼 확인한 범위를 알려주세요.",
}
CONTENT_FIELDS = (
    "title",
    "symptom",
    "naive_fix",
    "root_cause",
    "why_naive_fails",
    "key_insight",
    "deep_structure",
    "surface",
    "tech",
    "notes",
    "hypotheses",
    "avoid_terms",
    "hidden_terms",
    "accepted_fixes",
    "source_literals",
)
REQUIRED_FIELDS = (
    "title",
    "symptom",
    "root_cause",
    "naive_fix",
    "why_naive_fails",
    "key_insight",
    "deep_structure",
    "accepted_fixes",
)
# 사례 출처·검증 상태·기능형 재료 — 모델이 추측하거나 담당자가 바꿀 수 없다. 기능형 재료·템플릿은 D2 가 확인된 사례에서
# 다시 만든다 — 여기서 고칠 수 있게 두면 가명화를 거치지 않은 값(dict 키 포함)이 LLM·DB 로 간다 (privacy-auditor H1)
IDENTITY_FIELDS = (
    "incident_id",
    "sub",
    "org",
    "url",
    "published",
    "source_lang",
    "verified",
    "task_pattern",
    "templates",
    "feature",
)
_QUOTATION_MARKS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_UNCERTAIN = re.compile(r"모르|모름|미정|미상|불명|추정|가설|확인 전|확인되지")


def incident_digest(incident: PublicIncident) -> str:
    return hashlib.sha256(incident.model_dump_json().encode()).hexdigest()


def align_incident_evidence(
    evidence: dict[IncidentFact, list[str]], source: str
) -> tuple[dict[IncidentFact, list[str]], set[IncidentFact]]:
    """따옴표 모양만 달라진 인용은 실제 원문 구간으로 되돌린다."""
    masked = SECTION_MARKER.sub(lambda match: " " * len(match.group()), source)
    positions = [index for index, char in enumerate(masked) if not char.isspace()]
    folded = "".join(masked[index] for index in positions).translate(_QUOTATION_MARKS)
    aligned: dict[IncidentFact, list[str]] = {}
    repaired: set[IncidentFact] = set()
    for field, quotes in evidence.items():
        aligned[field] = []
        for quote in quotes:
            original = quote
            if not quote_in(quote, source):
                needle = compact_for_quote(quote).translate(_QUOTATION_MARKS)
                offset = folded.find(needle) if needle else -1
                if offset >= 0:
                    original = source[positions[offset] : positions[offset + len(needle) - 1] + 1]
                    repaired.add(field)
            aligned[field].append(original)
    return aligned, repaired


def evidence_missing(field: IncidentFact, quotes: list[str], source: str) -> bool:
    return (
        not quotes
        or any(
            quote.strip() in ("미정", "아직 미정") or not quote_in(quote, source)
            for quote in quotes
        )
        or field in ("root_cause", "accepted_fixes")
        and any(_UNCERTAIN.search(quote) for quote in quotes)
    )


def _clean_strings(value: object, clean: Clean) -> object:
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, list):
        return [_clean_strings(v, clean) for v in value]
    if isinstance(value, dict):
        return {k: _clean_strings(v, clean) for k, v in value.items()}
    return value


def validate_incident(incident: PublicIncident, *, clean: Clean) -> PublicIncident:
    """내용 칸·인터뷰 항목·기능형 재료의 글을 `clean` 에 통과시키고 필수 칸을 확인한다."""
    data = incident.model_dump(mode="json")
    for field in CONTENT_FIELDS:
        data[field] = _clean_strings(data[field], clean)
    if any(not data[k] for k in REQUIRED_FIELDS) or any(not v for v in data["accepted_fixes"]):
        raise ValueError("증상·원인·조치·함정과 원리를 모두 확인해 주세요")
    for case in data.get("known_incident_cases", []):
        case["interview_text"] = clean(case["interview_text"])
        case["answer_text"] = clean(case["answer_text"])
    data["feature"] = _clean_strings(data.get("feature"), clean)
    return PublicIncident.model_validate(data)


def prepare_incident(
    output: IncidentInterviewOutput,
    *,
    incident_id: str,
    sub: str,
    source: str,
    check: Clean,
) -> tuple[PublicIncident | None, list[IncidentFact], str]:
    """모델 출력 → (사례 초안 | None, 빠진 칸, 다음 질문). 근거 인용이 원문에 없거나 불확실하면 그 칸을 다시 묻는다."""
    evidence, _ = align_incident_evidence(output.evidence, source)
    missing = set(output.missing_fields)
    for field in FACT_LABELS:
        if evidence_missing(field, evidence.get(field, []), source):
            missing.add(field)
    if not output.conditions.strip():
        missing.add("conditions")
    if missing or output.incident is None:
        fields = [f for f in FACT_LABELS if f in missing] or list(FACT_LABELS)
        question = check(output.question)
        return None, fields, question or FOLLOW_UP_QUESTIONS[fields[0]]
    # 출처·조직·검증 상태는 모델이 추측하거나 공개 사례에서 가져올 수 없다.
    data = output.incident.model_dump(mode="json")
    data.update(
        incident_id=incident_id,
        sub=sub,
        org="[ORG_1]",
        url="",
        published="",
        source_lang="ko",
        verified=False,
        known_incident_cases=[],
    )
    data["symptom"] += f"\n발생 조건: {output.conditions}"
    if data.get("feature"):
        data["feature"]["reviewed"] = False
        data["templates"] = [TEMPLATE_FOR_SUB[sub]] if sub in TEMPLATE_FOR_SUB else []
    candidate = validate_incident(PublicIncident.model_validate(data), clean=check)
    content = json.dumps({k: getattr(candidate, k) for k in CONTENT_FIELDS}, ensure_ascii=False)
    if numbers(content) - numbers(source):
        raise OutputRejected(
            "인터뷰에 근거가 없는 수치가 생성되었습니다. 답변을 보완해 다시 시도해 주세요"
        )
    return candidate, [], ""


def attach_interview_cases(incident: PublicIncident, state: InterviewState) -> PublicIncident:
    """여러 사건이 아니라 한 사건의 정상 진술과 실패한 시도 한 건이다."""
    if state.incident_case_id is None:
        state.incident_case_id = str(uuid.uuid4())
    text = (
        f"관찰된 증상: {incident.symptom}\n확인한 원인과 근거: {incident.root_cause}\n조치와 결과: "
        + " / ".join(incident.accepted_fixes)
    )
    cases = [KnownIncidentCase(case_id=state.incident_case_id, interview_text=text, is_trap=False)]
    trap = state.trap_answers
    if trap and not trap.unknown:
        if state.trap_case_id is None:
            state.trap_case_id = str(uuid.uuid4())
        failed = (
            f"처음 시도한 대응: {trap.attempted_fix}\n관찰한 결과: {trap.observed_result}\n"
            f"실패한 이유: {trap.why_failed}"
        )
        cases.append(
            KnownIncidentCase(case_id=state.trap_case_id, interview_text=failed, is_trap=True)
        )
    return incident.model_copy(update={"known_incident_cases": cases})


def edited_incident(
    incident: PublicIncident, draft: PublicIncident, *, pseudonymize: Clean
) -> PublicIncident:
    """담당자가 고친 사례. 출처·검증 상태·기능형 재료·인터뷰 항목 구분은 바꿀 수 없고, 바뀐 글만 가명화한다.

    바뀌지 않은 칸은 저장된 가명화 글 그대로 둔다 — 다시 탐지기에 넣으면 문맥이 달라 새 토큰이 생겨 검증 지문이 바뀔 수 있다.
    """
    if any(getattr(incident, k) != getattr(draft, k) for k in IDENTITY_FIELDS):
        raise ValueError("사례 ID·직군·출처와 원문 검증 상태는 변경할 수 없습니다")
    if [(c.case_id, c.is_trap) for c in incident.known_incident_cases] != [
        (c.case_id, c.is_trap) for c in draft.known_incident_cases
    ]:
        raise ValueError("확인된 사례와 실패한 시도의 UUID·구분은 바꿀 수 없습니다")
    if not incident.known_incident_cases or any(
        not c.answer_text.strip() for c in incident.known_incident_cases
    ):
        raise ValueError("모든 기준 답안을 입력해 주세요")
    new, old = incident.model_dump(mode="json"), draft.model_dump(mode="json")

    def changed(value: object, before: object) -> object:
        return value if value == before else _clean_strings(value, pseudonymize)

    for field in CONTENT_FIELDS:
        new[field] = changed(new.get(field), old.get(field))
    old_cases = {c["case_id"]: c for c in old.get("known_incident_cases", [])}
    for case in new.get("known_incident_cases", []):
        before = old_cases.get(case["case_id"], {})
        for key in ("interview_text", "answer_text"):
            case[key] = changed(case[key], before.get(key))
    # 바뀐 칸도 기존 칸도 저장 전 같은 검사(필수 칸)를 받는다 — 글은 이미 가명화했으므로 그대로 통과시킨다
    return validate_incident(PublicIncident.model_validate(new), clean=lambda text: text)
