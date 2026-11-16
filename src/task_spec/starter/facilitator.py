"""진행자(응시 플랫폼) 보기 — 명세에서 지원자가 요청·질문하면 줄 수 있는 칸만 고른다 (2026-11-11 사용자: 평가 쪽 UI 가 DB 에서 읽게).

명세(common_task.task)에는 숨은 칸이 함께 있다 — 자료 역할(key·support·distractor), 답이 단서가 되는 함정(reveals), 숨은 요구·허용 설계·
관찰 항목·행동 기준. 이 보기의 모델에는 그 칸 자체가 없다 (빌드 API 와 같은 원칙 — 응답 모델에 없으면 새지 않는다).

질문 답 두 층 [확정: 2026-11-06 사용자, domain.Clarification]: 질문에 match_terms 중 하나라도 있으면 answer(구체), 없으면 answer_hint(모호),
answer_hint 가 없으면 answer. 진행자는 고르기만 하고 문장을 바꾸지 않는다. match_terms·고르지 않은 자료·2단계는 진행자 서버 쪽에서만
쓴다 — 이 보기를 통째로 지원자 화면에 내리지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from task_spec.schemas.domain import Artifact, TaskItem

ANSWER_RULE = (
    "질문에 match_terms 중 하나라도 들어 있으면 answer, 아니면 answer_hint 를 문장 그대로 준다 (answer_hint 가 없으면 answer). "
    "어느 주제에도 맞지 않으면 추가로 줄 정보가 없다고 한다."
)
# 새 규칙 글 (docs/design/analysis-stage.md §4.6, 손 시범으로 확인 중 — 아직 FacilitatorView.rule 에 쓰지 않는다).
# 응시 플랫폼의 진행자 LLM 이 읽는다. 항목의 구체 조건은 문장(ask_when)으로, 없으면 매칭 낱말을 예시 표현으로 본다 (2026-11-13 사용자).
ANSWER_RULE_ASK = (
    "질문이 실제로 묻는 항목에만 답한다. 질문이 어떤 항목의 구체 조건(ask_when, 없으면 match_terms 를 예시 표현으로)에 해당하면 그 항목의 answer 를, "
    "구체 조건에는 해당하지 않고 주제(topic)만 맞으면 answer_hint 를 문장 그대로 준다. 질문이 두 항목 이상을 함께 물으면 "
    "구체 조건에 해당하는 항목의 answer 를 모두 주고, 어느 항목의 구체 조건에도 해당하지 않으면 '한 가지씩 물어봐 주세요'라고 한다. "
    "어느 항목에도 해당하지 않으면 추가로 줄 정보가 없다고 한다. 묻지 않은 항목의 내용은 말하지 않는다."
)


class Material(BaseModel):
    """요청하면 주는 자료 — 제목·본문만 (역할 없음)."""

    id: str
    title: str
    content: str


class MaterialSet(BaseModel):
    max_requests: int | None
    items: list[Material]


class Answer(BaseModel):
    """추가 요구 하나 — 주제로 질문을 맞추고, 매칭 낱말로 구체·모호한 답을 고른다 (reveals 없음)."""

    id: str
    topic: str
    match_terms: list[str]
    answer: str
    answer_hint: str | None
    ask_when: str | None = (
        None  # 구체 답 조건 문장 (§4.2 새 형식, 손 시범에서 확인 중) — 없으면 match_terms 를 예시 표현으로
    )


class Clarifications(BaseModel):
    max_questions: int | None
    rule: str = ANSWER_RULE
    items: list[Answer]


class Stage1(BaseModel):
    time_limit_min: int
    materials: MaterialSet
    clarifications: Clarifications


class Stage2(BaseModel):
    """1단계 제출 뒤 공개 — 겉 증상·제출물·자료 (함정 id·관찰 항목 없음)."""

    reveal_after: Literal["stage1_submitted"]
    time_limit_min: int
    scenario: str
    deliverables: list[str]
    materials: MaterialSet


class FacilitatorView(BaseModel):
    common_id: str
    title: str | None
    stage1: Stage1
    stage2: Stage2 | None


def _materials(arts: list[Artifact], max_requests: int | None) -> MaterialSet:
    return MaterialSet(
        max_requests=max_requests,
        items=[Material(id=a.artifact_id, title=a.title, content=a.content) for a in arts],
    )


def facilitator_view(common_id: str, task: TaskItem) -> FacilitatorView:
    d = task.debug_stage
    return FacilitatorView(
        common_id=common_id,
        title=task.title,
        stage1=Stage1(
            time_limit_min=task.time_limit_min,
            materials=_materials(task.artifacts, task.max_requests),
            clarifications=Clarifications(
                max_questions=task.max_clarifications,
                items=[
                    Answer(
                        id=c.clarification_id,
                        topic=c.topic,
                        match_terms=list(c.match_terms),
                        answer=c.answer,
                        answer_hint=c.answer_hint,
                    )
                    for c in task.clarifications
                ],
            ),
        ),
        stage2=None
        if d is None
        else Stage2(
            reveal_after=d.reveal_after,
            time_limit_min=d.time_limit_min,
            scenario=d.scenario,
            deliverables=list(d.deliverables),
            materials=_materials(d.artifacts, d.max_requests),
        ),
    )
