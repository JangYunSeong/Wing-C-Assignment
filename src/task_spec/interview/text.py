"""인터뷰 글 처리 — 고정 질문, 프로젝트 태그, 공고의 팀 맥락. 포크 interviews.py 에서 D1 이 쓰는 부분만 옮겼다.

모두 가명화한 글을 받는다 (가명화는 nodes/pseudonymize/interview.py).
"""

from __future__ import annotations

import re

from task_spec.nodes.pseudonymize.detectors import TOKEN_RE
from task_spec.schemas.interview_state import ProjectTags, ScopeField, TeamContext

SCOPE_QUESTIONS: dict[ScopeField, str] = {
    "expected_outcome_90_days": (
        "입사 후 90일 안에 어떤 결과가 나오면 채용을 잘했다고 판단하나요? "
        "개인·고객 정보 없이 확인할 산출물이나 지표를 알려주세요. 미정이면 미정이라고 답해도 됩니다."
    ),
    "decision_situation": (
        "새 팀원이 맡을 일 중 팀에서 실제로 판단이 갈렸던 상황 한 가지를 알려주세요. "
        "어떤 선택지가 있었고, 시간·인력·권한 등 어떤 제약 아래 무엇을 기준으로 결정했나요? "
        "실명과 고객 정보는 빼 주세요. 공유할 사례가 없으면 미정이라고 답해도 됩니다."
    ),
}
CASE_OPENING = (
    "선택한 공고의 업무 중 이번 과제로 다룰 실제 문제 한 가지를 알려주세요. "
    "공고와 앞선 답변에 없는 사례 내용을 보태 주세요."
)
# ④ 처음 대응을 넣어 사례를 다시 정리했는데 다른 칸이 빠져 ③ 으로 돌아갈 때 — 질문 앞에 붙여 왜 돌아왔는지 알린다
TRAP_FOLLOW_UP_LEAD = "처음 시도한 대응을 반영해 사례를 정리하다 보니 더 확인할 내용이 있습니다."
UNSET = ("미정", "아직 미정")

_TAGS = {
    "개요": "overview",
    "기능 명세": "functional_spec",
    "데이터 모델": "data_model",
    "알려진 문제": "known_problem",
}
# 줄 전체가 '[태그]' 인 줄 — 가명화 토큰([PERSON_1] 등)만 있는 줄은 태그가 아니다
_TAG_LINE = re.compile(
    r"(?m)^\s*\[(?!(?:PERSON|CONTACT|LINK|SCHOOL|ORG|PRODUCT|COURSE)_\d+\])([^\]\n]+)\]\s*$"
)


def parse_project(text: str) -> ProjectTags:
    """'[개요]·[기능 명세]·[데이터 모델]·[알려진 문제]' 태그로 나눈 프로젝트 설명. 태그가 없으면 전부 개요."""
    if not text.strip():
        return ProjectTags()
    parts = _TAG_LINE.split(text)
    if len(parts) < 3:
        return ProjectTags(overview=parts[0].strip())
    if parts[0].strip():
        raise ValueError("태그 앞의 설명은 [개요] 안에 넣어 주세요")
    values: dict[str, str] = {}
    for tag, content in zip(parts[1::2], parts[2::2], strict=True):
        key = _TAGS.get(tag.strip())
        if key is None or key in values:
            raise ValueError("허용되지 않거나 중복된 프로젝트 태그가 있습니다")
        values[key] = content.strip()
    return ProjectTags.model_validate(values)


_POSTING_TEAM_LABELS: dict[ScopeField, re.Pattern[str]] = {
    "expected_outcome_90_days": re.compile(
        r"(?:입사\s*후\s*)?(?:첫\s*)?(?:90\s*일|3\s*개월)\s*(?:이?내\s*)?"
        r"(?:기대\s*)?(?:성과|목표|산출물)"
    ),
    "decision_situation": re.compile(
        r"(?:실제\s*)?(?:판단(?:이\s*갈린)?\s*상황|의사결정\s*사례)\s*(?:과|및|·)\s*(?:판단\s*)?기준"
    ),
}
_POSTING_OUTCOME = re.compile(
    r"^입사\s*후\s*(?:첫\s*)?(?:90\s*일|3\s*개월)\s*(?:안에|이?내(?:에)?|동안)\s+(\S.*)$"
)
_EMPLOYMENT_CONDITION = re.compile(r"^(?:수습|시용|급여|처우|계약\s*기간|평가\s*후|미정|없음)")


def posting_team_context(jd_text: str) -> TeamContext:
    """공고에 명시된 90일 성과와 판단 기준을 팀 맥락으로 쓴다 (포크와 같은 규칙)."""
    values: dict[str, str] = {}
    for line in jd_text.splitlines():
        line = line.lstrip("-•# ")
        label, separator, answer = line.replace("：", ":", 1).partition(":")
        answer = answer.strip()
        if separator and answer and answer not in {"미정", "아직 미정", "없음"}:
            for field, pattern in _POSTING_TEAM_LABELS.items():
                if pattern.fullmatch(label.strip()):
                    values.setdefault(field, answer)
        outcome = _POSTING_OUTCOME.fullmatch(line) if not separator else None
        if outcome and not _EMPLOYMENT_CONDITION.match(outcome[1]):
            values.setdefault("expected_outcome_90_days", outcome[1])
    return TeamContext(**values)


def numbers(text: str) -> set[str]:
    """글 속 숫자 (가명화 토큰의 번호는 빼고) — 원문에 없는 수치를 만들었는지 대조한다."""
    return set(re.findall(r"\d+(?:\.\d+)?", TOKEN_RE.sub("", text)))
