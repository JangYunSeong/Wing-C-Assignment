"""채용담당자 인터뷰 상태 (#262 D1).

Wing-C-Interview 포크(9ef2f50)의 형 없는 dict 상태를 Pydantic 으로 옮겼다. 칸 이름은 포크 API 응답과 같다 (화면이 그대로 쓸 수 있게).
D1 은 기존 공고에서 시작해 사례·기준 답안 확인(⑥)까지 — 역량·과제·승인 칸은 D2 에서 더한다.

담당자가 쓴 글은 전부 가명화한 글만 담는다 (결정 4 [확정: 2026-11-19 사용자]) — 원 값은 data/interviews/<id>/mapping.json 에만.
예외: company·title(공고 표에 이미 원문인 공개 정보)과 incident_confirmation.reviewer(직원 식별자) — LLM 입력에는 넣지 않는다.
DB 에 저장되고 API 로 나가는 경계 모델이지만 프롬프트에 렌더되지 않는다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from task_spec.schemas.approval import TaskKind
from task_spec.schemas.base import Schema
from task_spec.schemas.domain import TaskItem
from task_spec.schemas.incident import FeatureSeed, PublicIncident
from task_spec.schemas.interview_answer import AnswerValidationRecord, SuccessField
from task_spec.schemas.interview_incident import IncidentFact
from task_spec.schemas.provenance import Provenance
from task_spec.schemas.trap_frame import TrapFrame

Stage = Literal[
    "scope_interview",  # ② 팀 맥락 — 공고에 없는 항목만 고정 질문
    "incident_interview",  # ③ 사례 대화 — LLM 이 다음 질문을 정함
    "trap_interview",  # ④ 처음 시도한 대응
    "success_interview",  # ⑤ 성공 기준 → 기준 답안·답안 검증
    "incident_review",  # ⑥ 사례·답안 확인
    "competencies",  # ⑦ 역량 고르기 (⑥ 확인 뒤)
    "task_review",  # ⑧ 생성한 과제·지원자 질문에 답할 범위 검토
    "approved",  # ⑨ 승인됨 (D2b) — 사례를 co: 로 등록, 공통 과제 승인, 대응표 삭제. 더 고치지 않는다(새 인터뷰로 교체)
    "discarded",  # 폐기 (담당자 폐기·방치 30일) — 대응표를 지웠으므로 더 이상 LLM 을 부르지 않는다
]
ScopeField = Literal["expected_outcome_90_days", "decision_situation"]
# 사례 출처 (#266 D2c) — 인터뷰로 만든 사례 · 공개 사례(git 파일, 포크의 public_fixed) · 같은 회사의 공유 승인된 사례(co:)
IncidentSource = Literal["known_problem", "public_fixed", "company"]


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


QUESTION_MAX = 300  # 질문 한 개 (담당자가 고칠 수 있다)
ANSWER_MAX = 1000  # 짚은 질문의 답·숨은 조건 — 추가 요구 답(600)보다 넉넉하게


class GeneralQuestion(Schema):
    """지원자가 자료를 요청하며 던질 일반 질문 — 답은 자료로 직접 판단하게 안내한다 (포크 그대로)."""

    question: str = Field(min_length=1, max_length=QUESTION_MAX)
    artifact_ids: list[Annotated[str, Field(pattern=r"^a\d{1,2}$")]] = Field(
        min_length=1, max_length=12
    )
    response_rule: str = Field(
        default="관련 자료를 요청해 직접 판단하도록 안내합니다. 원인이나 정답은 알려주지 않습니다.",
        min_length=1,
        max_length=QUESTION_MAX,
    )


class TargetedQuestion(Schema):
    """지원자가 숨은 조건을 직접 짚어 물을 때 — 담당자가 정한 답만 준다 (answer 가 비면 승인 못 함)."""

    question_id: str = Field(pattern=r"^q\d{1,2}$")
    # 처음 질문은 숨은 조건을 문장에 담는다 (interview/task.questions_for)
    question: str = Field(min_length=1, max_length=QUESTION_MAX + ANSWER_MAX)
    hidden_condition: str = Field(min_length=1, max_length=ANSWER_MAX)
    answer: str | None = Field(default=None, min_length=1, max_length=ANSWER_MAX)


ReinforceKind = Literal["gap", "design"]


class ReinforceQuestion(Schema):
    """보강 질문 (#308 검사 5, #318 검사 1·2) — 인터뷰 사례 과제를 만든 뒤 ⑧ 에서 담당자에게 한 번 묻는다. 답하지 않아도 승인은 막지 않는다.
    - gap: 빠진 정보 — 재료 분석·함정 틀의 빈칸, 틀 사유의 재현 근거(검사 1). 자유 글로 답한다.
    - design: 업무 요구를 못 지킬 수 있는 허용 설계를 정답에서 뺄지 (검사 2). choices 로 시작하는 답을 코드가 읽는다
      ('뺀다 — <대신 인정할 설계>' 도 된다 — 대신할 설계는 다음 재료 만들기의 문답 입력으로 간다).
    LLM 이 쓴 글이 섞이므로 가명화 검사(derived)를 거친 글만."""

    question_id: str = Field(pattern=r"^r\d$")
    question: str = Field(min_length=1)
    kind: ReinforceKind = "gap"
    # design: 재료의 허용 설계 원문 — 다시 만들 때 이 설계를 뺀다 (틀 출력이 달라져도 같은 설계를 가리키게)
    design: str | None = None
    choices: list[str] = Field(
        default_factory=list
    )  # design: 화면이 버튼으로 보일 답 (자유 글도 된다)


class ReinforceAnswer(Schema):
    """보강 질문에 담당자가 준 답 — 가명화한 글, 모르면 None. 다음 과제 만들기의 기능형 재료 입력과 재료 분석 입력에 들어간다."""

    question_id: str = Field(pattern=r"^r\d$")
    question: str = Field(min_length=1)
    answer: str | None = Field(default=None, min_length=1, max_length=ANSWER_MAX)
    kind: ReinforceKind = "gap"
    design: str | None = None  # 질문의 재료 허용 설계 원문 (design)
    # design 답을 코드가 읽은 결과 — remove 면 다시 만들 때 그 설계를 틀 적용 재료에서 뺀다 (interview/trap_frame.apply_frame).
    # '뺀다/남긴다' 로 시작하지 않는 답은 None (남긴다) — 그 글은 문답으로 다음 재료 만들기에 들어간다
    choice: Literal["remove", "keep"] | None = None


class InterviewApprovalRecord(Schema):
    """⑨ 승인 기록 (#266 D2b) — 승인 그래프의 finalize 가 인터뷰 판본 확정과 같은 트랜잭션에 남긴다.
    reviewer 는 직원 식별자(원문 예외, incident_confirmation.reviewer 와 같다) — LLM 입력에는 넣지 않는다."""

    reviewer: str
    at: str  # ISO 시각
    revision: int  # 승인한 인터뷰 판본
    common_id: str  # 승인한 공통 과제 (interview:{id})
    incident_id: str  # 과제가 쓴 사례 — 인터뷰 사례면 이때 co: 로 등록(공유 승인 대기), 공개·회사 사례면 그 id
    incident_registered: bool = (
        True  # 이 승인이 회사 사례를 새로 등록했는가 (공개·회사 사례에서 고르면 False)
    )
    replaced: str | None = None  # 교체한 이전 인터뷰 과제
    comment: str | None = None  # 가명화한 글


class InterviewSpec(Schema):
    """확인된 사례로 만든 공고 공통 과제와 평가 맥락 (#266 D2a, 포크 interviews.InterviewSpec).

    coding_incident = 확인된 사례 + 직군 기본 템플릿의 기능형 재료(feature) — D2b 승인 때 이 사례를 co: 로 등록하고,
    빌더는 공통 과제의 source_incident 로 DB 에서 찾아 재료를 쓴다 (TaskItem 에 재료를 따로 싣지 않는다).
    """

    posting_id: str
    ontology_version: str
    competency_ids: list[str] = Field(min_length=2, max_length=3)
    template_id: str | None  # 사례형(코딩 템플릿 없는 직군 — 명세가 곧 과제)은 없음
    project: ProjectTags
    incident_source: IncidentSource = "known_problem"
    incident: PublicIncident
    coding_incident: PublicIncident
    incident_confirmation: IncidentConfirmation | None = None
    answer_validation: AnswerValidationRecord | None = None
    team_context: TeamContext | None = None
    common_task: TaskItem
    extension_slots: list[TaskItem] = Field(default_factory=list)
    general_questions: list[GeneralQuestion]
    targeted_questions: list[TargetedQuestion]
    hidden_requirements: list[str] = Field(default_factory=list)
    evaluation_intent: list[str]
    evaluation_criteria: list[str] = Field(default_factory=list)
    generation_provenance: list[Provenance] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)
    # 보강 질문 (#308·#318) — 인터뷰 사례에 물을 것(설계 빼기·빠진 정보)이 있고 아직 답한 적이 없을 때만 (최대 3개)
    reinforce_questions: list[ReinforceQuestion] = Field(default_factory=list, max_length=3)
    # 이 공고의 함정 틀 (#318) — 과제 글을 고칠 때 공개 쪽 표시(검사 3)를 다시 계산한다. 틀이 없거나 예전 과제면 None
    trap_frame: TrapFrame | None = None


class InterviewGeneration(_State):
    """⑧ 과제 만들기 진행 (#280) — 생성 중·실패·중단일 때만 있고, 과제를 저장하면 비운다.

    단계 산출물은 끊긴 단계부터 이을 때 읽는다 — 생성 그래프의 체크포인트에는 id 만 두고 사본을 두지 않는다
    (graph/interview_generate.py). 산출물도 다른 칸처럼 가명화한 글만이다.
    """

    thread_id: str  # 생성 그래프 thread (interview/generation.thread_id)
    revision: int  # 생성 표시를 저장한 인터뷰 판본 — thread·과제 id 에 쓴다
    started_at: str  # ISO 시각
    competency_ids: list[str]
    reason: str | None = None  # 실패 사유 (고정 문구) — 있으면 실패, 이어서 하면 지운다
    coding_incident: PublicIncident | None = None  # seed: 기능형 재료를 붙인 사례
    seed_provenance: list[Provenance] = Field(default_factory=list)
    # seed: 재료 입구 분석 표시 (#301 PR 3, interview/material_analysis) — 생성을 마칠 때 과제 검토 표시에 합친다 (막지 않음)
    seed_flags: list[str] = Field(default_factory=list)
    # seed: 재료 분석이 짚은 빠진 정보 (가명화 검사를 거친 글) — 인터뷰 사례면 생성을 마칠 때 보강 질문으로 (#308)
    seed_gaps: list[str] = Field(default_factory=list)
    # seed: 함정 틀과 틀 적용 재료 (#318, 가명화 검사를 거친 글) — feature 단계가 원 재료 대신 틀 적용 재료로 명세 초안을 만들고,
    # 생성을 마칠 때 공통 과제 저장 행에 함께 둔다 (빌드 계약 단계가 쓴다). 틀 프롬프트 CURRENT 가 없으면 비어 있다
    trap_frame: TrapFrame | None = None
    framed_seed: FeatureSeed | None = None
    task: TaskItem | None = None  # feature → request → slots 를 거치며 갱신
    task_provenance: list[Provenance] = Field(default_factory=list)
    task_flags: list[str] = Field(default_factory=list)


class InterviewState(_State):
    stage: Stage
    company: str  # 공고의 회사 (메타데이터, 원문) — 대응표의 ORG 토큰 첫 값
    title: str
    posting_id: str
    posting_context: PostingContext
    incident_sub: str
    # 과제 종류 — 직군 기본 코딩 템플릿이 있으면 기능형, 없으면 사례형(명세가 곧 과제). 시작할 때 정해 화면이 처음부터 알린다.
    # None 은 이 칸 전에 만든 행 — 직군으로 정한다 (interview/task.task_kind_for)
    task_kind: TaskKind | None = None
    jd_text: str = ""  # 가명화한 공고 줄 ('[절] 줄')
    team_context: TeamContext = Field(default_factory=TeamContext)
    scope_fields: list[ScopeField] = Field(default_factory=list)
    scope_answers: dict[ScopeField, str] = Field(default_factory=dict)
    scope_messages: list[ChatMessage] = Field(default_factory=list)
    project: ProjectTags = Field(default_factory=ProjectTags)
    incident_messages: list[ChatMessage] = Field(default_factory=list)
    incident_source: IncidentSource = "known_problem"  # D2c: 공개·회사 사례에서 고르기
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
    review_note: str | None = None  # 앞 단계로 돌아갈 때 남긴 수정 요청 (가명화) — 과제 생성이 쓴다
    competency_ids: list[str] = Field(default_factory=list)  # ⑦ 고른 역량 (2~3)
    template_id: str | None = None  # 직군 기본 코딩 템플릿
    spec: InterviewSpec | None = None  # ⑧ 생성한 과제와 평가 맥락
    generation: InterviewGeneration | None = None  # ⑧ 과제 만들기 진행 (#280)
    # ⑧ 보강 질문에 준 답 (#308, 가명화) — 다시 만들 때 기능형 재료·재료 분석 입력에 넣고, 있으면 다시 묻지 않는다 (한 번만)
    reinforce_answers: list[ReinforceAnswer] = Field(default_factory=list)
    approval: InterviewApprovalRecord | None = None  # ⑨ 승인 기록 (D2b)
    discarded_at: str | None = None
    discard_reason: Literal["recruiter", "stale"] | None = None

    # ---------------------------------------------------------------- 초기화 묶음 (포크는 같은 목록을 여러 곳에 복사했다)
    def unconfirm(self) -> None:
        """사례·답안 확인을 취소한다 — 앞 단계가 바뀌면 다시 확인해야 하고, 만든 과제도 버린다."""
        self.incident_confirmed = None
        self.incident_confirmation = None
        self.spec = None
        self.generation = None
        self.competency_ids = []
        self.reinforce_answers = []  # 사례가 바뀌면 그 재료에 물은 보강 질문의 답도 맞지 않는다

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
