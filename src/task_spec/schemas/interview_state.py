"""채용담당자 인터뷰 상태 (#262 D1).

Wing-C-Interview 포크(9ef2f50)의 형 없는 dict 상태를 Pydantic 으로 옮겼다. 칸 이름은 포크 API 응답과 같다 (화면이 그대로 쓸 수 있게).
D1 은 기존 공고에서 시작해 사례·기준 답안 확인(⑥)까지 — 역량·과제·승인 칸은 D2 에서 더한다.

담당자가 쓴 글은 전부 가명화한 글만 담는다 (결정 4 [확정: 2026-11-19 사용자]) — 원 값은 data/interviews/<id>/mapping.json 에만.
예외: company·title(공고 표에 이미 원문인 공개 정보)과 incident_confirmation.reviewer(직원 식별자) — LLM 입력에는 넣지 않는다.
DB 에 저장되고 API 로 나가는 경계 모델이지만 프롬프트에 렌더되지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from task_spec.schemas.incident import PublicIncident
from task_spec.schemas.interview_answer import AnswerValidationRecord, SuccessField
from task_spec.schemas.interview_incident import IncidentFact
from task_spec.schemas.provenance import Provenance

Stage = Literal[
    "scope_interview",  # ② 팀 맥락 — 공고에 없는 항목만 고정 질문
    "incident_interview",  # ③ 사례 대화 — LLM 이 다음 질문을 정함
    "trap_interview",  # ④ 처음 시도한 대응
    "success_interview",  # ⑤ 성공 기준 → 기준 답안·답안 검증
    "incident_review",  # ⑥ 사례·답안 확인
    "competencies",  # ⑥ 확인 뒤 — 역량·과제·승인은 D2
    "discarded",  # 폐기 (담당자 폐기·방치 30일) — 대응표를 지웠으므로 더 이상 LLM 을 부르지 않는다
]
ScopeField = Literal["expected_outcome_90_days", "decision_situation"]


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ChatMessage(_State):
    role: Literal["user", "assistant"]
    text: str
    message_id: str | None = None


class PostingContext(_State):
    posting_id: str
    sub: str
    industry_id: str | None = None
    ontology_version: str


class TeamContext(_State):
    expected_outcome_90_days: str | None = None
    decision_situation: str | None = None


class ProjectTags(_State):
    overview: str = ""
    functional_spec: str = ""
    data_model: str = ""
    known_problem: str = ""


class TrapAnswers(_State):
    attempted_fix: str = ""
    observed_result: str = ""
    why_failed: str = ""
    unknown: bool = False


class SuccessAnswers(_State):
    reproduction: str
    expected_result: str
    invariants: str
    verification: str
    alternatives: str = ""


class AnswerQuestion(_State):
    question_id: str
    question: str


class AnswerClarification(_State):
    question_id: str
    question: str
    answer: str


class IncidentConfirmation(_State):
    reviewer: str | None = None
    at: str
    revision: int
    sha256: str


class InterviewState(_State):
    stage: Stage
    company: str  # 공고의 회사 (메타데이터, 원문) — 대응표의 ORG 토큰 첫 값
    title: str
    posting_id: str
    posting_context: PostingContext
    incident_sub: str
    jd_text: str = ""  # 가명화한 공고 줄 ('[절] 줄')
    team_context: TeamContext = Field(default_factory=TeamContext)
    scope_fields: list[ScopeField] = Field(default_factory=list)
    scope_answers: dict[ScopeField, str] = Field(default_factory=dict)
    scope_messages: list[ChatMessage] = Field(default_factory=list)
    project: ProjectTags = Field(default_factory=ProjectTags)
    incident_messages: list[ChatMessage] = Field(default_factory=list)
    incident_source: Literal["known_problem"] = "known_problem"  # D2: 회사 사례 모음에서 고르기
    # 이 인터뷰가 만드는 사례의 회사 사례 id (co:<직군>:<12자>) — 첫 초안 때 정하고 인터뷰 동안 고정, D2 에서 그대로 등록
    incident_id: str | None = None
    incident_draft: PublicIncident | None = None
    incident_confirmed: PublicIncident | None = None
    incident_confirmation: IncidentConfirmation | None = None
    incident_evidence: dict[IncidentFact, list[str]] = Field(default_factory=dict)
    incident_missing: list[IncidentFact] = Field(default_factory=list)
    incident_provenance: list[Provenance] = Field(default_factory=list)
    incident_case_id: str | None = None
    trap_case_id: str | None = None
    trap_answers: TrapAnswers | None = None
    success_answers: SuccessAnswers | None = None
    answer_provenance: list[Provenance] = Field(default_factory=list)
    answer_evidence: list[str] = Field(default_factory=list)
    answer_missing: list[SuccessField] = Field(default_factory=list)
    answer_question: str = ""
    answer_questions: list[AnswerQuestion] = Field(default_factory=list)
    answer_clarifications: list[AnswerClarification] = Field(default_factory=list)
    answer_validation: AnswerValidationRecord | None = None
    answer_validation_provenance: list[dict] = Field(default_factory=list)
    review_note: str | None = (
        None  # 앞 단계로 돌아갈 때 남긴 수정 요청 (가명화) — D2 과제 생성이 쓴다
    )
    discarded_at: str | None = None
    discard_reason: Literal["recruiter", "stale"] | None = None

    # ---------------------------------------------------------------- 초기화 묶음 (포크는 같은 목록을 여러 곳에 복사했다)
    def unconfirm(self) -> None:
        """사례·답안 확인을 취소한다 — 앞 단계가 바뀌면 다시 확인해야 한다."""
        self.incident_confirmed = None
        self.incident_confirmation = None

    def reset_answers(self) -> None:
        """기준 답안·답안 검증을 지운다 (사례·처음 시도한 대응이 바뀌면)."""
        self.unconfirm()
        self.success_answers = None
        self.answer_provenance = []
        self.answer_evidence = []
        self.answer_missing = []
        self.answer_question = ""
        self.answer_questions = []
        self.answer_clarifications = []
        self.answer_validation = None
        self.answer_validation_provenance = []

    def reset_incident(self) -> None:
        """사례 대화부터 다시 (프로젝트 설명을 바꾸면)."""
        self.reset_answers()
        self.incident_draft = None
        self.incident_source = "known_problem"
        self.incident_messages = []
        self.incident_provenance = []
        self.incident_evidence = {}
        self.incident_missing = []
        self.trap_answers = None
