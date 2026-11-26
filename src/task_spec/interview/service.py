"""채용담당자 인터뷰 업무 (D1, #262). 포크 api/routers/interviews.py 의 단계 처리를 FastAPI 밖으로 옮겼다.

흐름: 기존 공고에서 시작(create) → ② 팀 맥락(message, 고정 질문) → ③ 사례 대화(message, LLM 후속 질문)
→ ④ 처음 시도한 대응(trap) → ⑤ 성공 기준·기준 답안·답안 검증(success) → ⑥ 사례·답안 확인(validate·confirm).
앞 단계로 돌아가기(revise·project), 단계별 임시 저장(save_drafts), 폐기(discard), 보관 기간 정리(sweep).

지키는 것:
- 담당자 글은 가명화한 뒤에만 LLM 으로 간다 — 가명화·대응표는 nodes/pseudonymize/interview.py (불변식 2, 결정 4).
  LLM 이 쓴 글은 저장 전에 패턴 탐지기·위조 토큰 검사.
- 판본(revision) 낙관적 갱신 + 인터뷰별 잠금(같은 인터뷰의 요청을 한 줄로 — 대응표 덮어쓰기 방지. uvicorn 워커 1개 전제).
  대응표는 DB 저장이 성공한 뒤에만 저장한다 — 충돌한 요청이 다른 요청의 토큰을 지우지 않게.
- 인터뷰 프롬프트에 CURRENT 가 없으면(평가 전) 시작·LLM 단계를 '준비 중'(503)으로 막는다 (prompt-registry, 2026-11-16 사용자).
- 로그에는 id·형식·건수만 (원문·가명화 글·대응표를 남기지 않는다).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal, NamedTuple, Protocol, TypeVar

from pydantic import BaseModel, RootModel, ValidationError

from task_spec.interview.answers import (
    ANSWER_PROMPT,
    CHECK_PROMPT,
    answer_sources,
    check_answer,
    collect_clarifications,
    draft_answer,
    set_answer_questions,
    validation_current,
)
from task_spec.interview.approval import (
    ApprovalCommit,
    ApprovalCommitter,
    ApprovalOutcome,
    approval_problems,
    blocked_findings,
    interview_ref,
    pattern_detect,
)
from task_spec.interview.errors import (
    InterviewClosed,
    InterviewConflict,
    InterviewDeleteFailed,
    InterviewError,
    InterviewGenerating,
    InterviewInvalid,
    InterviewNotFound,
    InterviewNotReady,
    InterviewUnavailable,
    OutputRejected,
    PostingMissing,
)
from task_spec.interview.generation import (
    GenerationBusy,
    GenerationNotResumable,
    GenerationRunner,
)
from task_spec.interview.generation import thread_id as generation_thread_id
from task_spec.interview.incident import (
    align_incident_evidence,
    attach_interview_cases,
    edited_incident,
    incident_digest,
    prepare_incident,
    validate_incident,
)
from task_spec.interview.material_analysis import (
    DESIGN_CHOICES,
    analysis_flags,
    analyze_material,
    design_choice,
    material_block,
    reinforce_block,
    reinforce_lines,
    reinforce_questions,
    reinforced_flag,
)
from task_spec.interview.material_analysis import FAILED as MATERIAL_FAILED
from task_spec.interview.material_analysis import TASK as MATERIAL_TASK
from task_spec.interview.store import CLOSED_STAGES, InterviewRepo, InterviewRow, InterviewSummary
from task_spec.interview.stream import QuestionStream
from task_spec.interview.task import (
    FEATURE_SEED_PROMPT,
    TARGETED_PROMPT,
    CompetencyChoice,
    MadeTask,
    TaskGenerationFailed,
    TaskMaker,
    TaskRequest,
    coding_template,
    copies_answer,
    evaluation_criteria,
    hidden_requirements,
    kept_competencies,
    mentions_artifact_id,
    prepare_coding_incident,
    pseudonymize_changes,
    questions_for,
    ready_coding_incident,
    task_frame,
    with_content_flags,
    with_interview_flag,
)
from task_spec.interview.text import (
    CASE_OPENING,
    CASE_REOPEN_QUESTION,
    SCOPE_QUESTIONS,
    TRAP_FOLLOW_UP_LEAD,
    TRAP_RETRY_NOTICE,
    TRAP_UNKNOWN_FOLLOW_UP_LEAD,
    UNSET,
    model_conversation,
    numbers,
    parse_project,
    posting_team_context,
)
from task_spec.interview.trap_frame import FAILED as TRAP_FAILED
from task_spec.interview.trap_frame import (
    FLAG_FRAME_EDITED,
    apply_frame,
    design_questions,
    evidence_gaps,
    frame_block,
    frame_flags,
    frame_problems,
    frame_texts,
    make_frame,
)
from task_spec.interview.trap_frame import TASK as TRAP_TASK
from task_spec.llm import LLMClient, SchemaViolation, TransientLLMError
from task_spec.llm.backend import TextCallback
from task_spec.llm.client import EmptyResponse
from task_spec.llm.quotes import quote_in
from task_spec.nodes.pseudonymize import (
    InterviewMappingGone,
    InterviewPseudonymizer,
    InterviewTextRejected,
)
from task_spec.nodes.pseudonymize.engine import RunMapping
from task_spec.postings.company import company_id as company_key
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    CompetencyEvidence,
    FeatureSeed,
    FramedMaterial,
    GenerateStep,
    MaterialAnalysis,
    PostingProfile,
    Provenance,
    PublicIncident,
    TaskItem,
    TrapFrame,
)
from task_spec.schemas.interview_answer import (
    AnswerValidationRecord,
    TargetedAnswerDraftInput,
    TargetedAnswerDraftOutput,
)
from task_spec.schemas.interview_approval import InterviewApprovalDecision, InterviewApprovalResult
from task_spec.schemas.interview_incident import IncidentInterviewInput, IncidentInterviewOutput
from task_spec.schemas.interview_state import (
    ANSWER_MAX,
    ChatMessage,
    GeneralQuestion,
    IncidentConfirmation,
    IncidentSource,
    InterviewApprovalRecord,
    InterviewGeneration,
    InterviewSpec,
    InterviewState,
    PostingContext,
    ReinforceAnswer,
    Stage,
    SuccessAnswers,
    TargetedQuestion,
    TrapAnswers,
)
from task_spec.schemas.pseudo_text import PseudonymizedText
from task_spec.spec.company_incidents import (
    FLAG_COMPANY_REUSED,
    CompanyIncidentRepo,
    Detect,
    feature_fit,
    is_company_incident,
    new_incident_id,
)
from task_spec.spec.draft import FLAG_DRAFT_FAILED, FLAG_DRAFT_INVALID, FLAG_DRAFT_INVALID_LEGACY
from task_spec.spec.feature import trap_link_problems
from task_spec.spec.incidents import incident_block, incident_by_id, known_cases_block
from task_spec.spec.store import CommonKey, CommonTaskRepo, InterviewTaskConflict, StoredCommon
from task_spec.starter.store import BuildRepo, BuildRuleError
from task_spec.starter.templates import Template

logger = logging.getLogger(__name__)

INCIDENT_PROMPT = "interview_incident_extended"
D1_PROMPTS = (INCIDENT_PROMPT, ANSWER_PROMPT, CHECK_PROMPT)
_LLM_ERRORS = (SchemaViolation, EmptyResponse, TransientLLMError, ValueError)
# 화면의 9단계 번호 (포크 DESIGN.md) — 그 단계를 제출하면 그 단계의 원문 임시 저장을 지운다
STEP_CHAT, STEP_TRAP, STEP_SUCCESS, STEP_REVIEW = "3", "4", "5", "6"
STEP_COMPETENCIES, STEP_TASK = "7", "8"
SUCCESS_FIELDS = ("reproduction", "expected_result", "invariants", "verification", "alternatives")
TRAP_FIELDS = ("attempted_fix", "observed_result", "why_failed")
RevisionTarget = Literal["project", "incident", "trap", "success", "common", "questions"]
_STALE = "인터뷰 판본이 바뀌었습니다. 새로고침 후 다시 시도해 주세요"
_GENERATING = "과제를 만들고 있습니다. 끝난 뒤 다시 시도해 주세요"
_GENERATE_FAILED = "과제를 만들지 못했습니다. 다시 시도해 주세요"
NOTE_TRAP_UNKNOWN = "실제 실패 시도는 확인되지 않음. 흔한 대응은 가상 함정 후보입니다."
T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


class _GeneralQuestions(RootModel[list[GeneralQuestion]]):
    pass


@dataclass(frozen=True)
class _GenerationInputs:
    """과제 만들기 단계의 입력 — 단계마다 인터뷰 상태·공고·온톨로지에서 다시 모은다 (#280, 그래프 State 에는 id 만)."""

    interview: (
        bool  # 인터뷰 사례 (기능형 재료를 LLM 으로 정리) — 공개·회사 사례는 재료를 그대로 쓴다
    )
    llm: LLMClient
    prompts: PromptRegistry
    tasks: TaskMaker
    common_store: CommonTaskRepo
    incident: PublicIncident
    mapping: RunMapping
    profile: PostingProfile
    chosen: PostingProfile  # 고른 역량만
    matches: list[CompetencyEvidence]
    # 공고 전체 역량의 근거 — 확장 슬롯이 공통 과제 밖 역량을 고른다 (설계 검사 #332 S-F18)
    all_matches: list[CompetencyEvidence]
    template: Template
    reviewer_note: str | None
    # ⑧ 보강 질문의 답 (#308, 인터뷰 사례만) — seed 가 기능형 재료·재료 분석 입력에 넣는다
    reinforce: tuple[ReinforceAnswer, ...] = ()


class _MaterialCheck(NamedTuple):
    """재료 입구 분석 결과 — 검토 표시 · provenance · 빠진 정보(가명화 검사를 거친 글, 보강 질문 재료) · 분석(틀 입력, 실패·건너뜀이면 None)."""

    flags: list[str]
    provenance: list[Provenance]
    gaps: list[str]
    analysis: MaterialAnalysis | None = None


class _FrameResult(NamedTuple):
    """함정 틀 결과 (#318) — 틀·틀 적용 재료(건너뜀·실패·쓸 수 없으면 None) · 검토 표시 · provenance ·
    보강 질문 후보 빈칸(재현 근거 검사 1 → 틀의 빈칸 순, 가명화 검사를 거친 글)."""

    frame: TrapFrame | None
    framed: FeatureSeed | None
    flags: list[str]
    provenance: list[Provenance]
    gaps: tuple[str, ...] = ()


def _links(task: TaskItem) -> list[str]:
    return trap_link_problems(
        [t.trap_id for t in task.traps], task.acceptance_hidden, task.accepted_designs
    )


def _design_counts(task: TaskItem) -> dict[str, int]:
    return {
        t.trap_id: sum(t.trap_id in d.trap_ids for d in task.accepted_designs) for t in task.traps
    }


def _broken_links(old: TaskItem, edited: TaskItem) -> list[str]:
    """고친 기능형 과제가 새로 깨뜨린 숨은 요구 연결 (설계 검사 B-F5) — 고치기 전에도 있던 문제는 뺀다. 허용 설계 수는 줄었을 때만
    (예전 판이 이미 모자랐으면 늘리는 고침은 막지 않는다 — 문장에 개수가 들어 있어 문장 비교로는 못 가린다)."""
    if edited.task_kind != "feature":
        return []
    before = set(_links(old))
    was, now = _design_counts(old), _design_counts(edited)
    fewer = {t for t, n in now.items() if n < 2 and n < was.get(t, 2)}
    out: list[str] = []
    for p in _links(edited):
        if p in before:
            continue
        if "를 만족하는 허용 설계가" in p and not any(
            p.startswith(f"숨은 요구 {t} ") for t in fewer
        ):
            continue
        out.append(p)
    return out


def _hidden_edited(old: TaskItem, new: TaskItem) -> bool:
    """숨은 요구 글·허용 설계를 고쳤는가 — 틀 적용 재료(빌드 계약 단계)와 글(숨은 테스트·허용 구현)이 갈린다."""
    return [t.model_dump() for t in old.traps] != [t.model_dump() for t in new.traps] or [
        d.model_dump() for d in old.accepted_designs
    ] != [d.model_dump() for d in new.accepted_designs]


def _with_trap(incident: PublicIncident, trap: TrapAnswers) -> PublicIncident:
    """④ 답을 사례에 넣는다 — 실제 시도면 흔한 대응·실패 이유를 답 그대로, 모름이면 비고 한 줄.

    초안이 ④ 전에 있었든 ④ 뒤에 만들어졌든 같은 결과. '모름' 비고는 한 번만 두고, 실제 시도로 바꾸면 지운다 (code review W5).
    """
    notes = "\n".join(
        line for line in incident.notes.split("\n") if line != NOTE_TRAP_UNKNOWN
    ).strip()
    if trap.unknown:
        return incident.model_copy(update={"notes": f"{notes}\n{NOTE_TRAP_UNKNOWN}".strip()})
    return incident.model_copy(
        update={
            "naive_fix": trap.attempted_fix,
            "why_naive_fails": trap.why_failed,
            "notes": notes,
        }
    )


def _generation_reason(exc: Exception) -> str:
    """과제 만들기 실패 사유 — 고정 문구만 (예외 메시지에 모델 출력·입력 조각이 섞일 수 있다)."""
    if isinstance(
        exc, TaskGenerationFailed
    ):  # 고른 역량·직군으로는 만들 수 없음 — 이 모듈의 고정 문구
        return str(exc)
    if isinstance(
        exc, InterviewError
    ):  # 업무 오류 (대응표 삭제·사례 보관·모델 호출 실패 등) — 고정 문구
        return exc.message
    return _GENERATE_FAILED


class _TargetedQuestions(RootModel[list[TargetedQuestion]]):
    pass


def _error_locations(exc: ValidationError) -> str:
    """검증 오류의 칸 위치만 — 값·입력 글은 싣지 않는다. 모르는 칸은 담당자가 만든 키라 이름도 싣지 않는다."""
    out: list[str] = []
    for err in exc.errors(include_url=False, include_input=False)[:5]:
        parts = [str(p) for p in err["loc"]]
        if err["type"] == "extra_forbidden" and parts:
            parts[-1] = "(모르는 칸)"
        out.append(".".join(parts) or "(전체)")
    return ", ".join(dict.fromkeys(out))


class _KeyedLocks:
    """인터뷰 id 별 잠금 — 쓰는 요청이 없으면 지운다. 나눠 쓰는 잠금(id 해시)은 긴 LLM 호출이 상관없는 인터뷰까지 막는다 (code review W2)."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock, users = self._held.get(key, (asyncio.Lock(), 0))
        self._held[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._held[key]
            if users <= 1:
                del self._held[key]
            else:
                self._held[key] = (lock, users - 1)


class PostingReader(Protocol):
    async def profile(self, posting_id: str) -> PostingProfile | None: ...

    async def lines(self, posting_id: str) -> list[tuple[str, str]]: ...

    async def company(self, posting_id: str) -> str | None: ...


@dataclass
class InterviewService:
    store: InterviewRepo
    pseudo: InterviewPseudonymizer
    postings: PostingReader
    llm: LLMClient | None
    prompts: PromptRegistry | None
    ontology_version: str  # 프롬프트 provenance 의 온톨로지 판 (공고 목록의 판)
    mapping_ttl: timedelta = timedelta(days=30)
    draft_ttl: timedelta = timedelta(days=1)
    tasks: TaskMaker | None = None  # 과제 생성기 묶음 (D2a — 온톨로지·LLM)
    common_store: CommonTaskRepo | None = None  # 공통 과제 저장소 (interview:{id} 초안)
    approvals: ApprovalCommitter | None = None  # 승인 확정 — 세 표를 한 트랜잭션으로 (D2b)
    builds: BuildRepo | None = (
        None  # 승인한 과제의 시작 코드 빌드 요청 (POST /interviews/{id}/builds)
    )
    detect: Detect = pattern_detect  # 회사 사례 등록 검사 (연락처·링크)
    # 사례 출처 (D2c) — 공개 사례 카탈로그(git 파일)와 같은 회사의 공유 승인된 사례
    public_incidents: Mapping[str, PublicIncident] | None = None
    company_incidents: CompanyIncidentRepo | None = None
    # 과제 만들기 실행기 (#280) — 생성 그래프를 백그라운드로 (api/interview_generation.py). 서비스를 만든 뒤 앱이 넣는다
    generation: GenerationRunner | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _locks: _KeyedLocks = field(default_factory=_KeyedLocks)

    # ================================================================ 공통
    def _lock(self, interview_id: str) -> AbstractAsyncContextManager[None]:
        return self._locks.hold(interview_id)

    def ready(self) -> bool:
        return (
            self.llm is not None
            and self.prompts is not None
            and all(self.prompts.has_current(t) for t in D1_PROMPTS)
        )

    def _require_ready(self) -> tuple[LLMClient, PromptRegistry]:
        if not self.ready() or self.llm is None or self.prompts is None:
            raise InterviewNotReady(
                "사례 인터뷰는 프롬프트 평가를 통과한 뒤 열립니다 (CURRENT 없음)"
            )
        return self.llm, self.prompts

    def view(self, row: InterviewRow) -> dict[str, Any]:
        drafts = row.ui_drafts
        if (
            row.drafts_started_at is not None
            and row.drafts_started_at < self.now() - self.draft_ttl
        ):
            drafts = {}  # 처음 쓴 뒤 하루 지난 원문 임시 저장은 보여주지 않는다 (정리는 sweep)
        return {
            "interview_id": row.id,
            "revision": row.revision,
            "ui_drafts": drafts,
            "draft_revision": row.draft_revision,
            **row.state.model_dump(mode="json"),
            "generation": self._generation_summary(row),
        }

    def _generation_summary(self, row: InterviewRow) -> dict[str, Any] | None:
        """화면용 과제 만들기 진행 — 단계 산출물(초안 사본)은 싣지 않는다. 닫힌 인터뷰는 없음."""
        g = row.state.generation
        if g is None or row.state.stage in CLOSED_STAGES:
            return None
        return {
            "status": self._generation_status(g),
            "thread_id": g.thread_id,
            "started_at": g.started_at,
            "competency_ids": list(g.competency_ids),
            "reason": g.reason,
        }

    async def _load(self, interview_id: str, *, open_only: bool = True) -> InterviewRow:
        row = await self.store.get(interview_id)
        if row is None:
            raise InterviewNotFound("인터뷰를 찾을 수 없습니다")
        if open_only and row.state.stage in CLOSED_STAGES:
            if row.state.stage == "approved":
                raise InterviewClosed(
                    "승인한 인터뷰는 고치지 않습니다. 과제를 바꾸려면 새 인터뷰로 교체해 주세요"
                )
            raise InterviewClosed("폐기된 인터뷰입니다. 새 인터뷰를 시작해 주세요")
        return row

    def _expect_revision(self, row: InterviewRow, revision: int) -> None:
        """상태를 바꾸는 요청의 입구 — 과제를 만드는 중이면 막고(#280), 판본을 확인한다."""
        self._ensure_idle(row)
        self._check_revision(row, revision)

    @staticmethod
    def _check_revision(row: InterviewRow, revision: int) -> None:
        if row.revision != revision:
            raise InterviewConflict(_STALE)

    @staticmethod
    def _require(state: InterviewState, *stages: Stage) -> None:
        if state.stage not in stages:
            raise InterviewConflict(f"현재 단계에서는 이 작업을 할 수 없습니다 ({state.stage})")

    def _mapping(self, interview_id: str) -> RunMapping:
        try:
            return self.pseudo.load(interview_id)
        except InterviewMappingGone:
            raise InterviewClosed(
                "이 인터뷰의 이름 대응표가 지워져 더 진행할 수 없습니다. 새 인터뷰를 시작해 주세요"
            ) from None

    async def _pseudonymize(self, raw: str, mapping: RunMapping) -> str:
        """담당자 원문 → 가명화 글 (탐지기는 CPU 를 쓰므로 스레드에서). 거절 사유는 화면에 그대로 보여도 되는 말."""
        try:
            return await asyncio.to_thread(self._plain(mapping), raw)
        except InterviewTextRejected as exc:
            raise InterviewInvalid(str(exc)) from None

    def _plain(self, mapping: RunMapping) -> Callable[[str], str]:
        """가명화한 글을 상태에 담을 일반 str 로. PseudonymizedText 는 깊은 복사·검증에서 다시 만들어지지 못하므로
        상태에는 str 로 두고, LLM 에 보낼 때만 stored_json 이 다시 검사해 만든다."""

        def plain(raw: str) -> str:
            return str(self.pseudo.pseudonymize(raw, mapping))

        return plain

    def _check(self, mapping: RunMapping) -> Callable[[str], str]:
        return partial(self.pseudo.derived, mapping=mapping)

    def _stored_json(self, mapping: RunMapping) -> Callable[..., PseudonymizedText]:
        def stored(value: Any, *, compact: bool = False) -> PseudonymizedText:
            return self.pseudo.stored_json(value, mapping, compact=compact)

        return stored

    async def _save(
        self,
        row: InterviewRow,
        revision: int,
        state: InterviewState,
        mapping: RunMapping | None,
        *,
        steps: Sequence[str] = (),
        keep_generation: bool = False,
    ) -> dict[str, Any]:
        """keep_generation 은 과제 만들기 경로만 — 다른 업무 저장은 실패·중단한 과제 만들기를 버린다 (#280, 도는 중이면
        입구에서 이미 막혔다). 버리지 않으면 이어서 한 생성이 그 사이 바뀐 상태를 덮는다."""
        self._check_revision(row, revision)
        if not keep_generation:
            state.generation = None
        dropped = row.state.generation
        if mapping is not None:
            # 대응표를 DB 보다 먼저 저장한다. 대응표는 토큰을 더하기만 하고 같은 인터뷰 요청은 잠금으로 한 줄이라, 이 대응표는
            # 디스크의 것을 포함한다 — DB 저장이 충돌·취소·실패해도 쓰지 않는 토큰이 남을 뿐이다. 반대 순서면 DB 만 저장되고
            # 대응표가 빠진 채 끊길 수 있고, 그 뒤 모든 LLM 단계가 '없는 토큰'으로 막힌다 (code review W1).
            await asyncio.to_thread(self.pseudo.save, row.id, mapping)
        updated = await self.store.save(row.id, revision, state, clear_steps=steps)
        if updated is None:
            raise InterviewConflict(_STALE)
        if dropped is not None and (
            state.generation is None or state.generation.thread_id != dropped.thread_id
        ):  # 버리거나 새로 시작한 생성의 thread — 이어서 할 일이 없다
            await self._forget_generation(dropped.thread_id)
        return self.view(updated)

    async def _forget_generation(self, thread_id: str) -> None:
        """이어서 할 일이 없어진 생성 thread 의 체크포인트를 지운다 (#280 I2). 실패해도 업무 저장은 이미 됐다 — 로그만."""
        if self.generation is None:
            return
        try:
            await self.generation.forget(thread_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("interview_generation_forget_failed type=%s", type(exc).__name__)

    async def _llm(self, call: Awaitable[T]) -> T:
        """LLM 호출 묶음. 형식·근거 오류는 부르는 쪽이 처리하고, 그 밖의 공급자 오류(인증·요청 오류 등)는 503 (code review W3)."""
        try:
            return await call
        except (*_LLM_ERRORS, InterviewError):
            raise
        except Exception as exc:
            logger.error("interview_llm_failed type=%s", type(exc).__name__)
            raise InterviewUnavailable(
                "모델을 부르지 못했습니다. 잠시 후 다시 시도해 주세요"
            ) from None

    def _question_stream(self, emit: Emit, mapping: RunMapping) -> QuestionStream:
        """질문 조각을 화면에 보내기 전에 패턴 탐지기·위조 토큰 검사 — 걸리면 그 조각은 보내지 않는다 (code review W4).
        확정 질문은 저장 전에 다시 검사하고, 걸리면 그 턴은 실패한다."""

        async def safe(event: str, data: dict[str, Any]) -> None:
            text = data.get("text", "")
            if text:
                try:
                    self.pseudo.derived(text, mapping)
                except InterviewTextRejected:
                    return
            await emit(event, data)

        return QuestionStream(safe)

    # ================================================================ 시작·조회
    async def create(self, posting_id: str) -> dict[str, Any]:
        """기존 공고에서 인터뷰를 시작한다. 공고 줄은 가명화해 맥락으로 두고, 공고에 없는 팀 맥락만 고정 질문으로 묻는다."""
        self._require_ready()
        profile = await self.postings.profile(posting_id)
        if profile is None:
            raise PostingMissing("공고를 찾을 수 없습니다")
        if not profile.sub:
            raise InterviewInvalid("직군이 확인된 공고를 선택해 주세요")
        company = (await self.postings.company(posting_id)) or profile.company or "공고 등록 조직"
        interview_id = f"int-{uuid.uuid4().hex}"
        async with self._lock(interview_id):
            mapping = self.pseudo.new_mapping(interview_id, company)
            jd_lines: list[str] = []
            facts: list[str] = []
            for section, text in await self.postings.lines(posting_id):
                try:
                    fact = await asyncio.to_thread(self._plain(mapping), text)
                except InterviewTextRejected:
                    continue  # 가명화할 수 없는 줄(비밀 값 모양 등)은 맥락에서 뺀다
                if fact:
                    facts.append(fact)
                    jd_lines.append(f"[{section}] {fact}")
            scope = posting_team_context("\n".join(facts))
            fields = [f for f in SCOPE_QUESTIONS if getattr(scope, f) is None]
            state = InterviewState(
                stage="scope_interview" if fields else "incident_interview",
                company=company,
                title=profile.position_name or "선택한 채용 공고",
                posting_id=posting_id,
                posting_context=PostingContext(
                    posting_id=posting_id,
                    sub=profile.sub,
                    industry_id=profile.industry_id,
                    ontology_version=profile.ontology_version,
                ),
                incident_sub=profile.sub,
                jd_text="\n".join(jd_lines)[:40000],
                team_context=scope,
                scope_fields=fields,
                scope_messages=(
                    [ChatMessage(role="assistant", text=SCOPE_QUESTIONS[fields[0]])]
                    if fields
                    else []
                ),
                incident_messages=[ChatMessage(role="assistant", text=CASE_OPENING)],
            )
            await asyncio.to_thread(self.pseudo.save, interview_id, mapping)
            try:
                row = await self.store.create(interview_id, state)
            except BaseException:
                await asyncio.to_thread(self.pseudo.delete, interview_id)
                raise
        logger.info(
            "interview_created id=%s stage=%s lines=%d", interview_id, state.stage, len(jd_lines)
        )
        return self.view(row)

    async def get(self, interview_id: str) -> dict[str, Any]:
        return self.view(await self._load(interview_id, open_only=False))

    async def summaries(
        self, *, company: str, stage: str | None, limit: int
    ) -> list[InterviewSummary]:
        """회사별 인터뷰 목록 (최근 진행 순) — Wing 이 시작한 인터뷰를 브라우저에만 기억해 다른 담당자의 인터뷰가 안 보였다.
        company 는 회사 id 또는 표기 (postings/company.company_id 로 맞춤). 방치 기간을 넘은 열린 인터뷰는 정리 작업 전이라도 discarded."""
        key = company_key(company)
        if key is None:
            return []  # 법인 표기·공백뿐 — 맞는 회사가 없다
        return await self.store.summaries(
            company=key,
            stages=[stage] if stage is not None else None,
            stale_before=self.now() - self.mapping_ttl,
            limit=limit,
        )

    # ================================================================ ②·③ 대화
    async def _message_received(
        self, row: InterviewRow, mapping: RunMapping, message_id: str | None, text: str
    ) -> bool:
        """같은 message_id 로 다시 보낸 답이면 저장하지 않고 지금 상태를 돌려준다 (스트림이 끊긴 뒤 재전송).
        판본·단계 검사보다 먼저 본다 — 그 뒤 단계가 바뀌었어도 같은 메시지 재전송은 200 + 지금 상태다."""
        if message_id is None:
            return False
        for entry in [*row.state.scope_messages, *row.state.incident_messages]:
            if entry.message_id == message_id and entry.role == "user":
                again = await self._pseudonymize(text, copy.deepcopy(mapping))
                if entry.text != again:
                    raise InterviewConflict("같은 메시지 ID로 다른 답변을 보낼 수 없습니다")
                return True
        return False

    async def message(
        self,
        interview_id: str,
        *,
        text: str,
        revision: int,
        message_id: str | None = None,
        emit: Emit | None = None,
    ) -> dict[str, Any]:
        """emit 을 주면 모델이 쓰는 질문 조각을 ('assistant', {'text': …}) 로 보낸다 (검사를 넘은 조각만)."""
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            mapping = self._mapping(interview_id)
            if await self._message_received(row, mapping, message_id, text):
                return self.view(row)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(state, "scope_interview", "incident_interview", "incident_review")
            if state.stage != "scope_interview":
                self._require_interview_case(state)
            answer = await self._pseudonymize(text, mapping)
            if not answer:
                raise InterviewInvalid("빈 답변은 저장할 수 없습니다")
            entry = ChatMessage(role="user", text=answer, message_id=message_id)
            if state.stage == "scope_interview":
                scope_field = state.scope_fields[len(state.scope_answers)]
                state.scope_answers = {**state.scope_answers, scope_field: answer}
                if answer not in UNSET:
                    setattr(state.team_context, scope_field, answer)
                state.scope_messages = [*state.scope_messages, entry]
                if len(state.scope_answers) < len(state.scope_fields):
                    nxt = state.scope_fields[len(state.scope_answers)]
                    state.scope_messages = [
                        *state.scope_messages,
                        ChatMessage(role="assistant", text=SCOPE_QUESTIONS[nxt]),
                    ]
                else:
                    state.stage = "incident_interview"
                return await self._save(row, revision, state, mapping, steps=(STEP_CHAT,))
            state.incident_messages = [*state.incident_messages, entry]
            on_delta = self._question_stream(emit, mapping) if emit is not None else None
            await self._incident_turn(state, mapping, on_delta=on_delta)
            return await self._save(row, revision, state, mapping, steps=(STEP_CHAT,))

    async def precheck_message(
        self, interview_id: str, *, text: str, revision: int, message_id: str | None = None
    ) -> str:
        """스트림을 열기 전 검사 (저장하지 않음) — 판본·단계·가명화 거절을 HTTP 오류로 먼저 돌려준다. 가명화한 글을 돌려준다."""
        row = await self._load(interview_id)
        mapping = copy.deepcopy(self._mapping(interview_id))
        if not await self._message_received(row, mapping, message_id, text):
            self._expect_revision(row, revision)
            self._require(row.state, "scope_interview", "incident_interview", "incident_review")
            if row.state.stage != "scope_interview":
                self._require_interview_case(row.state)
                self._require_ready()
        answer = await self._pseudonymize(text, mapping)
        if not answer:
            raise InterviewInvalid("빈 답변은 저장할 수 없습니다")
        return answer

    async def _incident_turn(
        self,
        state: InterviewState,
        mapping: RunMapping,
        *,
        on_delta: TextCallback | None = None,
        trap_step: bool = False,
    ) -> None:
        """대화 전체를 모델에 보내 다음 질문 또는 사례 초안을 받는다. 근거 인용이 담당자 말에 없으면 그 칸을 다시 묻는다.

        trap_step: ④ 제출에서 부른다 — 다른 칸이 빠져 ③ 으로 돌아가면 질문 앞에 이유를 붙이고, 처음 대응이 여전히
        빠지면 저장하지 않고 오류 (④ 를 고쳐 다시 내면 된다). ③ 에서 그렇게 되면 ④ 로 돌려보낸다.
        """
        llm, prompts = self._require_ready()
        context: dict[str, Any] = {
            "brief": [],  # 포크의 채용 방향 문답(D3) — 기존 공고에서 시작하면 비어 있다
            "project": state.project.model_dump(),
            "jd_text": state.jd_text,
            "trap_answers": state.trap_answers.model_dump() if state.trap_answers else None,
        }
        team = state.team_context.model_dump(exclude_none=True)
        if team:
            context["assessment_context"] = team
        conversation = model_conversation(state.incident_messages)
        stored_json = self._stored_json(mapping)
        if state.incident_id is None:
            state.incident_id = new_incident_id(state.incident_sub)
        try:
            result = await self._llm(
                llm.call(
                    assemble(
                        prompts.get(INCIDENT_PROMPT),
                        IncidentInterviewInput(
                            sub=state.incident_sub,
                            context_json=stored_json(context),
                            conversation_json=stored_json(conversation),
                        ),
                        ontology_version=self.ontology_version,
                    ),
                    IncidentInterviewOutput,
                    on_delta=on_delta,
                )
            )
            source = "\n".join(
                [
                    *context["project"].values(),
                    state.jd_text,
                    *(m.text for m in state.incident_messages if m.role == "user"),
                    *(
                        v
                        for v in (
                            state.trap_answers.model_dump() if state.trap_answers else {}
                        ).values()
                        if isinstance(v, str)
                    ),
                ]
            )
            evidence, _ = align_incident_evidence(result.parsed.evidence, source)
            check = self._check(mapping)
            # 근거 인용도 LLM 글이다 — 저장·응답 전에 같은 검사 (privacy-auditor L2)
            evidence = {field: [check(q) for q in quotes] for field, quotes in evidence.items()}
            draft, missing, question = prepare_incident(
                result.parsed.model_copy(update={"evidence": evidence}),
                incident_id=state.incident_id,
                sub=state.incident_sub,
                source=source,
                check=self._check(mapping),
            )
            if draft is not None:
                if state.trap_answers is not None:
                    draft = _with_trap(draft, state.trap_answers)
                draft = attach_interview_cases(draft, state)
        except _LLM_ERRORS as exc:
            logger.warning("interview_incident_rejected type=%s", type(exc).__name__)
            raise InterviewUnavailable(
                "사례를 정리하지 못했습니다. 마지막 저장 상태에서 답변을 보완해 다시 시도해 주세요"
            ) from None
        only_trap_missing = set(missing) <= {"naive_fix", "why_naive_fails"}
        needs_trap = state.trap_answers is None and (draft is not None or only_trap_missing)
        # 처음 대응을 받았는데도 그 두 칸만 빠졌다 — ③ 대화로는 채울 수 없다 (프롬프트가 묻지 않는다)
        retry_trap = state.trap_answers is not None and draft is None and only_trap_missing
        if retry_trap and trap_step:
            raise InterviewUnavailable(
                "처음 시도한 대응을 사례에 반영하지 못했습니다. 다시 시도해 주세요"
            )
        if needs_trap:
            question = ""
        elif retry_trap:
            # ④ 로 — 받은 답(trap_answers)은 남겨 화면이 칸을 채워 보여 줄 수 있다
            question = TRAP_RETRY_NOTICE
        elif trap_step and question and state.trap_answers is not None:
            lead = (
                TRAP_UNKNOWN_FOLLOW_UP_LEAD if state.trap_answers.unknown else TRAP_FOLLOW_UP_LEAD
            )
            question = f"{lead} {question}"
        state.reset_answers()
        state.incident_draft = draft
        state.incident_source = "known_problem"
        state.incident_evidence = evidence
        state.incident_missing = missing
        state.stage = (
            "trap_interview"
            if needs_trap or retry_trap
            else "success_interview"
            if draft
            else "incident_interview"
        )
        state.incident_provenance = [*state.incident_provenance, result.provenance]
        if question:
            state.incident_messages = [
                *state.incident_messages,
                ChatMessage(role="assistant", text=question),
            ]

    async def project(
        self, interview_id: str, *, text: str, revision: int, note: str | None = None
    ) -> dict[str, Any]:
        """프로젝트 설명('[개요]' 등 태그)을 바꾸면 사례 대화를 처음부터 다시 한다. note 는 앞 단계로 돌아갈 때의 수정 요청."""
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(
                state,
                "incident_interview",
                "trap_interview",
                "success_interview",
                "incident_review",
                "competencies",
            )
            mapping = self._mapping(interview_id)
            if note is not None:
                state.review_note = await self._pseudonymize(note, mapping)
            pseudo = await self._pseudonymize(text, mapping)
            try:
                state.project = parse_project(pseudo)
            except ValueError as exc:
                raise InterviewInvalid(str(exc)) from None
            state.reset_incident()
            state.stage = "incident_interview"
            if text.strip():
                await self._incident_turn(state, mapping)
            return await self._save(row, revision, state, mapping, steps=(STEP_CHAT,))

    # ================================================================ ④ 처음 시도한 대응
    async def trap(
        self,
        interview_id: str,
        *,
        revision: int,
        attempted_fix: str,
        observed_result: str,
        why_failed: str,
        unknown: bool,
    ) -> dict[str, Any]:
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(state, "trap_interview", "success_interview", "incident_review")
            self._require_interview_case(state)
            mapping = self._mapping(interview_id)
            raw = {
                "attempted_fix": attempted_fix,
                "observed_result": observed_result,
                "why_failed": why_failed,
            }
            answers = {k: await self._pseudonymize(v, mapping) for k, v in raw.items()}
            if not unknown and not all(answers.values()):
                raise InterviewInvalid("처음 시도한 대응·관찰 결과·실패 이유를 모두 입력해 주세요")
            trap = state.trap_answers = TrapAnswers(**answers, unknown=unknown)
            state.reset_answers()
            if state.incident_draft is not None:
                incident = _with_trap(state.incident_draft, trap)
                state.incident_draft = attach_interview_cases(incident, state)
                state.stage = "success_interview"
            else:
                # ③ 이 처음 대응만 빠진 채 끝나 초안이 없다 — 처음 대응을 넣어 다시 정리하고, 다른 칸이 빠지면 ③ 으로 돌아간다
                await self._incident_turn(state, mapping, trap_step=True)
            return await self._save(row, revision, state, mapping, steps=(STEP_TRAP,))

    # ================================================================ ⑤ 성공 기준 → 기준 답안 → 검증
    def _record_validation(self, state: InterviewState, record: AnswerValidationRecord) -> None:
        state.answer_validation = record
        state.unconfirm()
        state.stage = "success_interview" if record.questions else "incident_review"
        state.answer_validation_provenance = [
            *state.answer_validation_provenance,
            record.provenance,
        ]
        set_answer_questions(state, record.questions)

    async def success(
        self,
        interview_id: str,
        *,
        revision: int,
        reproduction: str,
        expected_result: str,
        invariants: str,
        verification: str,
        alternatives: str = "",
        clarification_answers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(state, "success_interview", "incident_review")
            self._require_interview_case(state)
            if state.incident_draft is None or state.trap_answers is None:
                raise InterviewInvalid("사례와 처음 시도한 대응을 먼저 확인해 주세요")
            llm, prompts = self._require_ready()
            mapping = self._mapping(interview_id)
            raw = dict(
                zip(
                    SUCCESS_FIELDS,
                    (reproduction, expected_result, invariants, verification, alternatives),
                    strict=True,
                )
            )
            success = {k: await self._pseudonymize(v, mapping) for k, v in raw.items()}
            if any(not v.strip() for k, v in success.items() if k != "alternatives"):
                raise InterviewInvalid("재현 조건·성공 결과·불변 조건·검증 방법을 입력해 주세요")
            try:
                await asyncio.to_thread(
                    collect_clarifications,
                    state,
                    clarification_answers or {},
                    self._plain(mapping),
                )
            except ValueError as exc:
                raise InterviewInvalid(str(exc)) from None
            state.success_answers = SuccessAnswers(**success)
            check = self._check(mapping)
            stored_json = self._stored_json(mapping)
            incident = state.incident_draft
            try:
                output, provenance = await self._llm(
                    draft_answer(
                        incident,
                        state,
                        llm=llm,
                        prompts=prompts,
                        ontology_version=self.ontology_version,
                        stored_json=stored_json,
                    )
                )
                state.answer_provenance = [*state.answer_provenance, provenance]
                if output.missing_fields or output.questions or output.question.strip():
                    questions = output.questions or [
                        output.question or "성공 조건과 확인 방법을 보완해 주세요"
                    ]
                    set_answer_questions(state, [check(q) for q in questions])
                    state.answer_missing = output.missing_fields
                    state.answer_validation = None
                    state.unconfirm()
                    state.stage = "success_interview"
                    return await self._save(row, revision, state, mapping, steps=(STEP_SUCCESS,))
                sources = answer_sources(state)
                source_by_id = {s["source_id"]: s["text"] for s in sources}
                evidence = list(output.evidence)
                if output.source_ids:
                    if any(key not in source_by_id for key in output.source_ids):
                        raise OutputRejected("기준 답안 근거의 원문 ID가 없습니다")
                    evidence = [source_by_id[key] for key in dict.fromkeys(output.source_ids)]
                source = "\n".join(s["text"] for s in sources)
                if (
                    not output.answer_text.strip()
                    or (
                        any(c.is_trap for c in incident.known_incident_cases)
                        and not output.trap_answer_text.strip()
                    )
                    or not evidence
                    or not all(quote_in(q, source) for q in evidence)
                ):
                    raise OutputRejected("기준 답안의 근거를 확인할 수 없습니다")
                if numbers(output.answer_text + "\n" + output.trap_answer_text) - numbers(source):
                    raise OutputRejected("확인되지 않은 수치가 기준 답안에 추가되었습니다")
                normal, trap_text = check(output.answer_text), check(output.trap_answer_text)
                cases = [
                    c.model_copy(update={"answer_text": trap_text if c.is_trap else normal})
                    for c in incident.known_incident_cases
                ]
                incident = validate_incident(
                    incident.model_copy(update={"known_incident_cases": cases}), clean=check
                )
                state.incident_draft = incident
                state.answer_evidence = evidence
                state.answer_missing = []
                record = await self._llm(
                    check_answer(
                        incident,
                        state,
                        llm=llm,
                        prompts=prompts,
                        ontology_version=self.ontology_version,
                        stored_json=stored_json,
                        check=check,
                    )
                )
            except _LLM_ERRORS as exc:
                logger.warning(
                    "interview_answer_rejected type=%s reason=%s",
                    type(exc).__name__,
                    str(exc) if isinstance(exc, OutputRejected) else "-",
                )
                raise InterviewUnavailable(
                    "기준 답안을 정리·검증하지 못했습니다. 마지막 저장 상태에서 입력을 확인해 다시 시도해 주세요"
                ) from None
            self._record_validation(state, record)
            return await self._save(row, revision, state, mapping, steps=(STEP_SUCCESS,))

    # ================================================================ ⑥ 사례·답안 확인
    async def _edited(
        self, incident: PublicIncident, state: InterviewState, mapping: RunMapping
    ) -> PublicIncident:
        if state.incident_draft is None:
            raise InterviewInvalid("확인할 사례가 없습니다")
        try:
            return await asyncio.to_thread(
                edited_incident,
                incident,
                state.incident_draft,
                pseudonymize=self._plain(mapping),
            )
        except ValueError as exc:  # InterviewTextRejected 포함 — 화면에 보여도 되는 말
            raise InterviewInvalid(str(exc)) from None

    async def validate(
        self, interview_id: str, *, revision: int, incident: PublicIncident
    ) -> dict[str, Any]:
        """담당자가 고친 사례·기준 답안을 다시 검증한다."""
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(state, "incident_review")
            self._require_interview_case(state)
            if state.success_answers is None:
                raise InterviewInvalid("성공 기준 인터뷰를 먼저 완료해 주세요")
            llm, prompts = self._require_ready()
            mapping = self._mapping(interview_id)
            edited = await self._edited(incident, state, mapping)
            try:
                record = await self._llm(
                    check_answer(
                        edited,
                        state,
                        llm=llm,
                        prompts=prompts,
                        ontology_version=self.ontology_version,
                        stored_json=self._stored_json(mapping),
                        check=self._check(mapping),
                    )
                )
            except _LLM_ERRORS as exc:
                logger.warning("interview_validate_rejected type=%s", type(exc).__name__)
                raise InterviewUnavailable(
                    "기준 답안을 검증하지 못했습니다. 마지막 저장 상태에서 다시 시도해 주세요"
                ) from None
            state.incident_draft = edited
            self._record_validation(state, record)
            return await self._save(row, revision, state, mapping, steps=(STEP_REVIEW,))

    async def confirm(
        self,
        interview_id: str,
        *,
        revision: int,
        incident: PublicIncident,
        reviewer: str | None = None,
    ) -> dict[str, Any]:
        """검증을 통과한 지금 판의 사례·기준 답안을 확인한다 → 역량·과제 단계(D2)로."""
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(state, "incident_review")
            if state.incident_source != "known_problem":
                return await self._confirm_chosen(row, revision, state, incident, reviewer)
            if state.trap_answers is None:
                raise InterviewInvalid("처음 시도한 대응 스텝을 먼저 확인해 주세요")
            if any(not c.answer_text.strip() for c in incident.known_incident_cases):
                raise InterviewInvalid("성공 기준을 바탕으로 정리한 기준 답안을 확인해 주세요")
            if reviewer is not None and not reviewer.strip():
                raise InterviewInvalid("사례 확인자를 입력해 주세요")
            if state.incident_missing:
                raise InterviewInvalid("부족한 사례 정보를 먼저 확인해 주세요")
            mapping = self._mapping(interview_id)
            edited = await self._edited(incident, state, mapping)
            if not validation_current(edited, state):
                raise InterviewInvalid(
                    "변경한 사례·답안의 근거와 해법·함정 판별을 다시 검증해 주세요"
                )
            state.incident_draft = edited
            state.incident_confirmed = edited
            state.incident_confirmation = IncidentConfirmation(
                reviewer=reviewer.strip() if reviewer is not None else None,
                at=self.now().isoformat(),
                revision=revision,
                sha256=incident_digest(edited),
            )
            state.stage = "competencies"
            return await self._save(row, revision, state, mapping, steps=(STEP_REVIEW,))

    # ================================================================ ⑦·⑧ 역량·과제 (D2a, #266)
    def _require_step(self, prompt: str, what: str) -> tuple[LLMClient, PromptRegistry]:
        llm, prompts = self._require_ready()
        if not prompts.has_current(prompt):
            raise InterviewNotReady(
                f"{what}는 {prompt} 프롬프트 평가를 통과한 뒤 열립니다 (CURRENT 없음)"
            )
        return llm, prompts

    def _require_tasks(self) -> tuple[TaskMaker, CommonTaskRepo]:
        if self.tasks is None or self.common_store is None:
            raise InterviewNotReady("과제 생성기가 준비되지 않았습니다")
        return self.tasks, self.common_store

    async def _competencies(self, tasks: TaskMaker, profile: PostingProfile) -> CompetencyChoice:
        """공고 역량의 수행준거 근거 (온톨로지 조회). 조회 오류는 원인을 싣지 않고 503."""
        try:
            return await tasks.competencies(profile)
        except Exception as exc:
            logger.error("interview_competencies_failed type=%s", type(exc).__name__)
            raise InterviewUnavailable(
                "공고 역량 근거를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요"
            ) from None

    async def _apply_edit(self, model: type[M], new: Any, old: Any, mapping: RunMapping) -> M:
        """담당자가 고친 구조 → 바뀐 글만 가명화해 모델로. 형식 오류는 칸 위치만 알린다."""
        try:
            data = await asyncio.to_thread(pseudonymize_changes, new, old, self._plain(mapping))
            return model.model_validate(data)
        except InterviewTextRejected as exc:  # 화면에 보여도 되는 고정 문구
            raise InterviewInvalid(str(exc)) from None
        except ValidationError as exc:
            raise InterviewInvalid(f"형식을 확인해 주세요: {_error_locations(exc)}") from None

    async def _profile(self, posting_id: str) -> PostingProfile:
        profile = await self.postings.profile(posting_id)
        if profile is None or not profile.sub:
            raise PostingMissing("공고를 찾을 수 없습니다")
        return profile

    async def competency_preview(self, interview_id: str) -> dict[str, Any]:
        """고를 수 있는 공고 역량(수행준거 근거가 있는 것)과 규칙이 권하는 역량, 직군 기본 코딩 템플릿."""
        row = await self._load(interview_id)
        self._require(row.state, "competencies", "task_review")
        tasks, _ = self._require_tasks()
        profile = await self._profile(row.state.posting_id)
        choice = await self._competencies(tasks, profile)
        try:
            template_id = coding_template(profile.sub or "").manifest.template_id
        except TaskGenerationFailed as exc:
            raise InterviewInvalid(str(exc)) from None
        return {
            "ontology_version": profile.ontology_version,
            "template_id": template_id,
            "competencies": [
                {
                    "competency_id": c.competency_id,
                    "label": c.label,
                    "primary": c.primary,
                    "ncs_units": [
                        u.model_dump()
                        for e in choice.matches
                        if e.competency_id == c.competency_id
                        for u in e.ncs_units
                        if u.n_criteria
                    ],
                }
                for c in choice.available
            ],
            "suggested_ids": choice.suggested,
        }

    async def select_competencies(
        self, interview_id: str, *, revision: int, competency_ids: list[str]
    ) -> dict[str, Any]:
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(state, "competencies", "task_review")
            if state.spec is not None and state.competency_ids == competency_ids:
                # 같은 선택을 다시 보냄(두 번 누름 등) — 만든 과제를 지우지 않는다
                return self.view(row)
            tasks, _ = self._require_tasks()
            profile = await self._profile(state.posting_id)
            choice = await self._competencies(tasks, profile)
            available = {c.competency_id for c in choice.available}
            if (
                not 2 <= len(competency_ids) <= 3
                or len(set(competency_ids)) != len(competency_ids)
                or not set(competency_ids) <= available
            ):
                raise InterviewInvalid("수행준거 근거가 있는 공고 역량 2~3개를 고르세요")
            # 공통 과제 뼈대 규칙(주 역량 먼저, 보조 역량은 2개를 채울 때만)으로 빠질 역량이 있으면 지금 알린다 — 예전에는 과제 만들기
            # 끝(기능형 초안 LLM 뒤)에야 거절했다 (설계 검사 #332 S-F7)
            chosen = profile.model_copy(
                update={
                    "competencies": [
                        c for c in profile.competencies if c.competency_id in set(competency_ids)
                    ]
                }
            )
            kept = set(kept_competencies(chosen, choice.matches))
            # 근거 조회 결과가 비면(규칙이 아무것도 못 담음) 여기서 막지 않는다 — 생성 단계가 같은 규칙으로 다시 본다
            if kept and (
                dropped := [c.label for c in chosen.competencies if c.competency_id not in kept]
            ):
                raise InterviewInvalid(
                    f"고른 역량 가운데 {', '.join(dropped)} 제외 — 공통 과제는 주 역량을 먼저 담고, 보조 역량은 주 역량이 "
                    "하나일 때 하나만 담습니다. 다시 골라 주세요"
                )
            try:
                template = coding_template(profile.sub or "")
            except TaskGenerationFailed as exc:
                raise InterviewInvalid(str(exc)) from None
            state.competency_ids = list(competency_ids)
            state.template_id = template.manifest.template_id
            state.spec = None
            state.stage = "competencies"
            return await self._save(row, revision, state, None, steps=(STEP_COMPETENCIES,))

    def _reviewer_note(self, state: InterviewState) -> str | None:
        """포크 generate 와 같은 수정 요청 — 확인된 사례·기준 답안을 유지하며 팀 맥락을 소재·산출물에 반영."""
        note = state.review_note or ""
        scope = state.team_context.model_dump(exclude_none=True)
        if scope:
            labels = {
                "expected_outcome_90_days": "90일 내 기대 성과",
                "decision_situation": "평가할 판단 상황과 기준",
            }
            lines = "\n".join(f"{labels[k]}: {v}" for k, v in scope.items())
            note = (
                "확인된 사례와 기준 답안을 유지하며 아래 채용 맥락을 과제의 소재와 산출물에 반영하세요.\n"
                f"{lines}\n{note}"
            ).strip()
        return note or None

    # ================================================================ ⑧ 과제 만들기 (#280) — 생성 그래프 graph/interview_generate.py
    # 명세 생성 과정과 같은 실행 방식: generate 는 검증하고 생성 표시를 저장한 뒤 그래프를 띄우고 바로 돌아온다. 그래프가 단계마다
    # generation_step 을 부르고, 산출물은 인터뷰 상태(generation)에 둔다 — 끊긴 단계부터 이을 때 거기서 읽는다.
    def _require_runner(self) -> GenerationRunner:
        if self.generation is None:
            raise InterviewNotReady("과제 생성 실행기가 준비되지 않았습니다")
        return self.generation

    def _generating(self, row: InterviewRow) -> bool:
        """이 프로세스에서 이 인터뷰의 과제를 만드는 중 (워커 1개 전제 — api/app.py)."""
        g = row.state.generation
        return (
            g is not None and self.generation is not None and self.generation.is_active(g.thread_id)
        )

    def _ensure_idle(self, row: InterviewRow) -> None:
        if self._generating(row):
            raise InterviewGenerating(_GENERATING)

    async def ensure_idle(self, interview_id: str) -> None:
        """과제를 만드는 중이면 409 — 서비스 잠금 밖에서 그래프를 도는 승인 API 의 입구 (#280)."""
        self._ensure_idle(await self._load(interview_id, open_only=False))

    def _generation_status(
        self, g: InterviewGeneration
    ) -> Literal["running", "failed", "interrupted"]:
        """도는 중 = 이 프로세스의 실행기에 있음, 실패 = 사유가 남음, 그 밖 = 재시작·배포로 끊김 (이어서 하기)."""
        if self.generation is not None and self.generation.is_active(g.thread_id):
            return "running"
        return "failed" if g.reason is not None else "interrupted"

    async def _generation_check(self, state: InterviewState, competency_ids: list[str]) -> None:
        """시작·이어서 하기 전에 — 고른 역량·준비된 프롬프트·확인한 사례·답안 검증·고른 회사 사례."""
        self._require(state, "competencies", "task_review")
        if not state.competency_ids or state.competency_ids != competency_ids:
            raise InterviewInvalid("먼저 고른 역량과 생성 요청이 다릅니다")
        interview = state.incident_source == "known_problem"
        # 기능형 재료 정리(interview_feature_seed)는 인터뷰 사례만 — 공개·회사 사례는 재료를 그대로 쓴다
        if interview:
            self._require_step(FEATURE_SEED_PROMPT, "과제 생성")
        else:
            self._require_ready()
        self._require_tasks()
        incident = state.incident_confirmed
        if (
            incident is None
            or state.incident_confirmation is None
            or state.incident_confirmation.sha256 != incident_digest(incident)
        ):
            raise InterviewInvalid("사례·답안 확인 단계를 먼저 완료해 주세요")
        if interview and not validation_current(incident, state):
            raise InterviewInvalid("기준 답안 검증을 완료하고 다시 확인해 주세요")
        if state.incident_source == "company":  # 고른 뒤 보관·반려됐으면 쓰지 않는다
            await self._company_case(incident.incident_id, state)

    async def generate(
        self, interview_id: str, *, revision: int, competency_ids: list[str]
    ) -> dict[str, Any]:
        """과제 만들기 시작 — 확인된 사례 → 기능형 재료 → 공고 공통 과제(interview:{id} 초안) → 지원자 질문을 백그라운드로.
        바로 돌아오고(생성 표시가 담긴 화면), 진행·결과·실패는 GET 으로 본다. 승인은 D2b 그래프."""
        runner = self._require_runner()
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            await self._generation_check(state, competency_ids)
            self._mapping(interview_id)  # 대응표가 지워졌으면 지금 멈춘다
            marker = row.revision + 1  # 아래 저장이 올리는 판본
            thread = generation_thread_id(interview_id, marker)
            state.generation = InterviewGeneration(
                thread_id=thread,
                revision=marker,
                started_at=self.now().isoformat(),
                competency_ids=list(competency_ids),
            )
            # 표시를 저장하기 전에 띄운다 — 단계는 표시를 이 잠금 안에서 읽으므로(generation_step) 이 저장이 끝난 뒤에 읽는다.
            # 저장 뒤에 띄우면 그 사이 읽은 요청이 표시만 보고 '중단됨'으로 본다. 저장이 실패하면 그 thread 는 표시를 못 찾아 stale
            runner.start(interview_id, thread)
            return await self._save(row, revision, state, None, keep_generation=True)

    async def resume_generation(self, interview_id: str, *, revision: int) -> dict[str, Any]:
        """실패·중단한 과제 만들기를 그 단계부터 — 같은 thread, 끝난 단계는 다시 하지 않는다."""
        runner = self._require_runner()
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            g = state.generation
            if g is None:
                raise InterviewConflict("이어서 할 과제 만들기가 없습니다")
            await self._generation_check(state, list(g.competency_ids))
            try:
                await runner.resume(g.thread_id)
            except GenerationBusy:
                raise InterviewGenerating(_GENERATING) from None
            except GenerationNotResumable:
                raise InterviewConflict(
                    "이어서 할 단계가 없습니다. 과제를 다시 만들어 주세요"
                ) from None
            state.generation = g.model_copy(update={"reason": None})
            return await self._save(row, revision, state, None, keep_generation=True)

    async def generation_progress(self, interview_id: str) -> dict[str, Any]:
        """화면 폴링용 — 진행 상태·단계(도는 중이면 지금, 실패·중단이면 이어서 할 단계)·판본. 만드는 중이 아니면 idle."""
        row = await self._load(interview_id, open_only=False)
        g = row.state.generation
        if g is None or row.state.stage in CLOSED_STAGES:
            return {
                "status": "idle",
                "current_node": None,
                "started_at": None,
                "reason": None,
                "revision": row.revision,
            }
        progress = await self.generation.progress(g.thread_id) if self.generation else None
        return {
            "status": self._generation_status(g),
            "current_node": progress.step if progress else None,
            "started_at": g.started_at,
            "reason": g.reason,
            "revision": row.revision,
        }

    @staticmethod
    def _own_generation(row: InterviewRow | None, thread_id: str) -> InterviewGeneration | None:
        """이 thread 의 생성 표시 — 다시 만들기·역량 바꾸기·폐기·승인으로 바뀌었으면 None (낡은 thread)."""
        if row is None or row.state.stage in CLOSED_STAGES:
            return None
        g = row.state.generation
        return g if g is not None and g.thread_id == thread_id else None

    async def _generation_inputs(
        self, interview_id: str, state: InterviewState, g: InterviewGeneration
    ) -> _GenerationInputs:
        interview = state.incident_source == "known_problem"
        llm, prompts = (
            self._require_step(FEATURE_SEED_PROMPT, "과제 생성")
            if interview
            else self._require_ready()
        )
        tasks, common_store = self._require_tasks()
        incident = state.incident_confirmed
        if incident is None:
            raise InterviewInvalid("사례·답안 확인 단계를 먼저 완료해 주세요")
        mapping = self._mapping(interview_id)
        profile = await self._profile(state.posting_id)
        choice = await self._competencies(tasks, profile)
        selected = set(g.competency_ids)
        chosen = profile.model_copy(
            update={
                "competencies": [c for c in profile.competencies if c.competency_id in selected]
            }
        )
        return _GenerationInputs(
            interview=interview,
            llm=llm,
            prompts=prompts,
            tasks=tasks,
            common_store=common_store,
            incident=incident,
            mapping=mapping,
            profile=profile,
            chosen=chosen,
            matches=[e for e in choice.matches if e.competency_id in selected],
            all_matches=list(choice.matches),
            template=coding_template(profile.sub or ""),
            reviewer_note=self._reviewer_note(state),
            reinforce=tuple(state.reinforce_answers) if interview else (),
        )

    @classmethod
    def _framed(cls, g: InterviewGeneration) -> PublicIncident:
        """명세 초안에 쓸 재료 — 틀 적용 재료가 있으면 원 재료의 기능형 칸만 바꾼 사본 (#318). 등록·표시용 재료(spec.coding_incident)는
        원 재료 그대로다 — 공용 회사 사례 재료는 공고 하나의 틀로 바꾸지 않는다 (D1)."""
        coding = cls._seeded(g)
        return (
            coding
            if g.framed_seed is None
            else coding.model_copy(update={"feature": g.framed_seed})
        )

    @staticmethod
    def _seeded(g: InterviewGeneration) -> PublicIncident:
        if g.coding_incident is None:  # 그래프가 seed 를 저장한 뒤에만 다음 단계로 간다
            raise InterviewConflict(
                "과제 만들기 단계 순서가 어긋났습니다. 과제를 다시 만들어 주세요"
            )
        return g.coding_incident

    @staticmethod
    def _drafted(g: InterviewGeneration) -> MadeTask:
        if g.task is None:
            raise InterviewConflict(
                "과제 만들기 단계 순서가 어긋났습니다. 과제를 다시 만들어 주세요"
            )
        return MadeTask(task=g.task, provenance=list(g.task_provenance), flags=list(g.task_flags))

    async def _material_check(
        self, coding: PublicIncident, inp: _GenerationInputs, *, shared: bool = False
    ) -> _MaterialCheck:
        """재료 입구 분석 (#301 PR 3) — 회사 재료가 빌드 하네스에서 재현되는지 짚어 과제 검토 표시로 (승인을 막지 않음).
        프롬프트 CURRENT 가 없으면(평가 전) 건너뛴다. 분석이 실패해도 과제 만들기는 이어 간다 — 실패도 표시로 남긴다.
        입력은 저장본 검사(stored_json)를 거친 가명화 글, 표시는 LLM 이 쓴 글이라 저장 전에 다시 검사한다 (derived).

        shared=True: 공유 승인된 회사 사례를 다시 쓸 때 (#308). 재료에 등록한 인터뷰의 토큰이 남아 있어 `shared_json` 으로 그 재료에
        있는 토큰까지 허용하고, 표시 검사도 같은 집합을 쓴다. 그 전에 등록 규칙(blocked_findings — LLM 입력 칸의 연락처·링크)으로
        재료를 다시 보고, 입력 칸은 그 규칙이 LLM 입력으로 승인한 칸만 (notes 제외). 보관·반려된 재료는 _company_case 가 이미 막는다.

        인터뷰 사례에 ⑧ 보강 질문의 답이 있으면 재료 블록의 reinforcement 칸으로 함께 보낸다 (#308 검사 5). 빠진 정보(gaps)는
        표시와 같은 가명화 검사를 거쳐 돌려준다 — 인터뷰 사례면 생성 마무리가 보강 질문으로 쓴다."""
        if not inp.prompts.has_current(MATERIAL_TASK):
            return _MaterialCheck([], [], [])
        try:
            block = material_block(coding, shared=shared)
            if inp.reinforce and not shared:
                block["reinforcement"] = reinforce_lines(inp.reinforce)
            material, tokens = await self._guarded_material(coding, block, inp, shared=shared)
            analysis, provenance = await self._llm(
                analyze_material(
                    material,
                    inp.template,
                    llm=inp.llm,
                    prompts=inp.prompts,
                    ontology_version=inp.profile.ontology_version,
                )
            )
            flags = [
                self.pseudo.derived(f, inp.mapping, tokens)
                for f in analysis_flags(analysis, inp.template, coding.feature)
            ]
            gaps = [self.pseudo.derived(gap, inp.mapping, tokens) for gap in analysis.gaps]
        except (*_LLM_ERRORS, InterviewError) as exc:
            logger.warning("interview_material_check_failed type=%s", type(exc).__name__)
            return _MaterialCheck([MATERIAL_FAILED], [], [])
        logger.info(
            "interview_material_check reproducible=%s flags=%d gaps=%d",
            analysis.reproducible,
            len(flags),
            len(gaps),
        )
        return _MaterialCheck(flags, [provenance], gaps, analysis)

    async def _guarded_material(
        self, coding: PublicIncident, block: dict[str, Any], inp: _GenerationInputs, *, shared: bool
    ) -> tuple[PseudonymizedText, frozenset[str]]:
        """재료 블록 → LLM 입력 (가명화 검사를 거친 글)과 출력 검사에 더 허용할 토큰. 재료 분석과 함정 틀이 같이 쓴다.
        shared=True(공유 회사 사례): 등록 때와 같은 규칙·탐지기로 다시 보고(탐지기가 블로킹이라 스레드에서) shared_json (#316)."""
        if not shared:
            return self.pseudo.stored_json(block, inp.mapping), frozenset()
        if await asyncio.to_thread(blocked_findings, coding, self.detect):
            raise InterviewTextRejected("공유 재료의 LLM 입력 칸에 연락처·링크 형식이 있습니다")
        return self.pseudo.shared_json(block, inp.mapping)

    async def _trap_frame(
        self,
        coding: PublicIncident,
        inp: _GenerationInputs,
        analysis: MaterialAnalysis | None,
        *,
        shared: bool = False,
    ) -> _FrameResult:
        """함정 틀 (#318, docs/design/trap-frame.md) — 재료 분석 다음에 이 공고의 틀을 정하고 틀 적용 재료를 만든다.
        프롬프트 CURRENT 가 없으면(평가 전) 건너뛴다 — 원 재료 그대로. 틀이 실패하거나 템플릿과 맞지 않아도 과제는 원 재료로 만들고
        표시만 남긴다 (막지 않음). 입력은 재료 분석과 같은 가명화 길(_guarded_material), 틀의 글은 저장·다음 LLM 입력 전에 derived."""
        if not inp.prompts.has_current(TRAP_TASK) or coding.feature is None:
            return _FrameResult(None, None, [], [])
        try:
            block = frame_block(coding, analysis, inp.reinforce, shared=shared)
            material, tokens = await self._guarded_material(coding, block, inp, shared=shared)
            frame, provenance = await self._llm(
                make_frame(
                    material,
                    inp.template,
                    llm=inp.llm,
                    prompts=inp.prompts,
                    ontology_version=inp.profile.ontology_version,
                )
            )
            for text in frame_texts(frame):
                self.pseudo.derived(text, inp.mapping, tokens)
            problems = frame_problems(frame, inp.template)
            if problems:
                logger.warning("interview_trap_frame_unusable problems=%d", len(problems))
                return _FrameResult(None, None, problems, [provenance])
            # 검사 2 (#318 §5.2) — ⑧ 설계 질문에 담당자가 '뺀다'고 한 설계를 뺀다 (재료 원문, 다시 만든 재료는 앞부분으로 맞춘다)
            designs = [a for a in inp.reinforce if a.kind == "design"]
            removed = [a.design for a in designs if a.choice == "remove" and a.design]
            framed, notes = apply_frame(coding.feature, frame, removed)
            if unread := sum(1 for a in designs if a.choice is None and a.answer):
                notes.append(
                    f"설계 질문 답 {unread}개를 '{'/'.join(DESIGN_CHOICES)}' 로 읽지 못해 설계를 남김 — 사람 확인"
                )
            # 검사 1 (#318 §5.1) — 틀 사유의 성립 조건에 쓰는 운영 값의 근거 (등록 재료 기준 — 틀 적용 재료는 조절값을 비울 수 있다)
            evidence = evidence_gaps(frame.reason, inp.template, coding.feature, analysis)
            flags = [
                self.pseudo.derived(f, inp.mapping, tokens)
                for f in [*frame_flags(frame, notes), *(e.flag for e in evidence)]
            ]
            gaps = tuple(
                self.pseudo.derived(gap, inp.mapping, tokens)
                for gap in [*(e.question for e in evidence), *frame.gaps]
            )
        except (*_LLM_ERRORS, InterviewError) as exc:
            logger.warning("interview_trap_frame_failed type=%s", type(exc).__name__)
            return _FrameResult(None, None, [TRAP_FAILED], [])
        logger.info(
            "interview_trap_frame kind=%s storage=%d designs_flagged=%d",
            frame.kind,
            len(frame.forbidden_storage),
            sum(1 for d in frame.design_matrix if d.holds != "yes"),
        )
        return _FrameResult(frame, framed, flags, [provenance], gaps)

    async def _run_step(
        self, interview_id: str, step: GenerateStep, inp: _GenerationInputs, g: InterviewGeneration
    ) -> dict[str, Any]:
        """seed·feature·request·slots 하나 — 잠금 없이 (LLM 몇 분). 저장할 산출물 칸을 돌려준다."""
        if step == "seed":
            if inp.interview:
                parts = [incident_block(inp.incident), known_cases_block(inp.incident)]
                if inp.reinforce:  # ⑧ 보강 질문의 답 (#308) — 재료를 정리할 때 함께 본다
                    parts.append(reinforce_block(inp.reinforce))
                text = self.pseudo.stored("\n".join(parts), inp.mapping)
                coding, seed_provenance, seed_fit = await self._llm(
                    prepare_coding_incident(
                        inp.incident,
                        inp.template,
                        interview=text,
                        llm=inp.llm,
                        prompts=inp.prompts,
                        ontology_version=inp.profile.ontology_version,
                    )
                )
                self._registrable(coding)
                checked = await self._material_check(coding, inp)
                framed = await self._trap_frame(coding, inp, checked.analysis)
                return {
                    "coding_incident": coding,
                    "seed_provenance": [*seed_provenance, *checked.provenance, *framed.provenance],
                    "seed_flags": [*seed_fit, *checked.flags, *framed.flags],
                    # 보강 질문 후보 빈칸 — 재현 근거(검사 1) → 틀 → 재료 분석 순 (설계 질문은 마무리 때 틀에서 만든다)
                    "seed_gaps": list(dict.fromkeys([*framed.gaps, *checked.gaps])),
                    "trap_frame": framed.frame,
                    "framed_seed": framed.framed,
                }
            coding = ready_coding_incident(inp.incident, inp.template)
            if not is_company_incident(coding.incident_id):
                # 공개 재료는 서비스에서 보지 않는다 — 평가(eval/interview/run_material_analysis.py)로 한 번 본다 (#301)
                return {"coding_incident": coding, "seed_provenance": [], "seed_flags": []}
            # 회사 사례도 재료 입구 분석 — 재료는 고치지 않고 표시만 (물을 담당자가 없다). 2026-11-25 #308 4차: 이 분기는 분석을
            # 부르지 않아 #311 전에 등록한 회사 재료를 다시 쓰면 재현 의문이 보이지 않았다. 재현 여부는 템플릿 판에 따라 달라 쓸 때마다 본다
            checked = await self._material_check(coding, inp, shared=True)
            # 함정 틀도 공고마다 — 공용 재료는 바꾸지 않고 틀 적용 재료는 이 공고의 공통 과제 행에 둔다 (#318 D1)
            framed = await self._trap_frame(coding, inp, checked.analysis, shared=True)
            return {
                "coding_incident": coding,
                "seed_provenance": [*checked.provenance, *framed.provenance],
                "seed_flags": [
                    *checked.flags,
                    *framed.flags,
                ],  # 물을 담당자가 없어 빠진 정보는 표시로만 (보강 질문 없음)
                "trap_frame": framed.frame,
                "framed_seed": framed.framed,
            }
        req = TaskRequest(
            profile=inp.chosen,
            matches=inp.matches,
            slot_profile=inp.profile,  # 확장 슬롯은 공고 전체 역량으로 (설계 검사 #332 S-F18)
            slot_matches=inp.all_matches,
            coding_incident=self._framed(
                g
            ),  # 틀 적용 재료가 있으면 그것으로 (#318) — 명세가 재료에서 숨은 요구·허용 설계를 복사한다
            template=inp.template,
            task_id=f"{interview_id}-common-{g.revision}",
            reviewer_note=inp.reviewer_note,
            frame=g.trap_frame,  # 공개 쪽을 명세 초안에 (#318 — draft_feature_task v13)
        )
        if step == "feature":
            made = await self._llm(inp.tasks.feature(req))
        elif step == "request":
            made = await self._llm(inp.tasks.request(req, self._drafted(g)))
        else:
            made = await self._llm(inp.tasks.slots(req, self._drafted(g)))
        return {"task": made.task, "task_provenance": made.provenance, "task_flags": made.flags}

    async def generation_step(self, interview_id: str, thread_id: str, step: GenerateStep) -> bool:
        """생성 그래프의 단계 하나 — 입력을 모아 단계를 돌리고(잠금 없이) 잠금 안에서 산출물을 저장한다.
        생성 표시가 이 thread 가 아니면 아무것도 하지 않고 False (그래프는 stale 로 끝난다)."""
        if step == "finish":
            return await self._finish_generation(interview_id, thread_id)
        # 표시는 잠금 안에서 읽는다 — 잠금 밖이면 시작(generate)이 표시를 저장하기 전 행을 읽어 stale 로 끝난다
        # (Postgres 저장은 이벤트 루프에 양보한다, #280 개인정보 점검 I3)
        async with self._lock(interview_id):
            row = await self.store.get(interview_id)
        g = self._own_generation(row, thread_id)
        if row is None or g is None:
            return False
        inp = await self._generation_inputs(interview_id, row.state, g)
        update = await self._run_step(interview_id, step, inp, g)
        async with self._lock(interview_id):
            row = await self.store.get(interview_id)
            g = self._own_generation(row, thread_id)
            if row is None or g is None:
                return False
            state = row.state.model_copy(deep=True)
            state.generation = g.model_copy(update=update)
            # 대응표는 넘기지 않는다 — 단계는 토큰을 만들지 않고(pseudo.stored), 몇 분 전에 읽은 대응표를 다시 쓰면 그 사이
            # 더해진 토큰을 되돌린다 (개인정보 점검 L3)
            await self._save(row, row.revision, state, None, keep_generation=True)
        return True

    async def _finish_generation(self, interview_id: str, thread_id: str) -> bool:
        """질문을 만들고 공통 과제 초안·과제를 저장한다 (잠금 안에서) — 생성 표시를 비우고 과제 검토 단계로."""
        async with self._lock(interview_id):
            row = await self.store.get(interview_id)
            g = self._own_generation(row, thread_id)
            if row is None or g is None:
                return False
            state = row.state.model_copy(deep=True)
            inp = await self._generation_inputs(interview_id, state, g)
            coding_incident = self._seeded(g)
            made = self._drafted(g)
            # 인터뷰 사례만 '공유 승인 전' 표시로 바꾼다 — 고른 회사 사례는 공유 승인된 것이라 생성기 표시가 맞다
            task = with_interview_flag(made.task, coding_incident) if inp.interview else made.task
            general, targeted = questions_for(task)
            if not general or not targeted:
                logger.warning("interview_generate_rejected type=no_questions")
                raise InterviewUnavailable(
                    "자료와 숨은 조건에 연결된 질문을 만들지 못했습니다. 다시 시도해 주세요"
                )
            reuse: list[str] = []
            if (
                state.incident_source == "company"
            ):  # 다른 공고도 쓰는 사례 — 두 공고에 모두 지원한 사람은 함정·정답을 안다
                used = await inp.common_store.incident_usage(
                    [coding_incident.incident_id], exclude_posting=state.posting_id
                )
                if n := used.get(coding_incident.incident_id, 0):
                    reuse.append(FLAG_COMPANY_REUSED.format(id=coding_incident.incident_id, n=n))
            # 보강 질문 (#308 검사 5) — 인터뷰 사례에 빠진 정보가 있으면 ⑧ 에서 한 번 묻는다. 답한 적이 있으면 다시 묻지 않고
            # 답을 넣어 만들었다는 표시만 (빠진 정보는 분석 표시로 그대로 보인다)
            asked = list(state.reinforce_answers) if inp.interview else []
            questions = (
                reinforce_questions(
                    g.seed_gaps,
                    # 검사 2 (#318 §5.2) — 업무 요구를 못 지킬 수 있는 허용 설계를 뺄지 (설계 질문 먼저)
                    [  # 질문 글·재료 설계 원문 모두 저장 전에 다시 검사 (설계 원문은 API 응답·다음 만들기 입력으로도 간다)
                        (self.pseudo.derived(q, inp.mapping), self.pseudo.derived(d, inp.mapping))
                        for q, d in design_questions(g.trap_frame, coding_incident.feature)
                    ]
                    if g.trap_frame is not None and coding_incident.feature is not None
                    else (),
                )
                if inp.interview and not asked
                else []
            )
            seed_flags = [*g.seed_flags, *([reinforced_flag(asked)] if asked else [])]
            provenance = [*g.seed_provenance, *made.provenance]
            saved = await inp.common_store.save_draft(
                CommonKey(
                    state.posting_id, inp.profile.ontology_version, f"interview:{interview_id}"
                ),
                task,
                provenance,
                # 함정 틀 (#318 D1) — 틀 적용 재료와 틀을 이 공고의 공통 과제 행에. 빌드 계약 단계가 원 재료 대신 쓴다
                framed=FramedMaterial(seed=g.framed_seed, frame=g.trap_frame)
                if g.framed_seed is not None and g.trap_frame is not None
                else None,
            )
            task = saved.task
            state.spec = InterviewSpec(
                posting_id=state.posting_id,
                ontology_version=inp.profile.ontology_version,
                competency_ids=list(g.competency_ids),
                template_id=inp.template.manifest.template_id,
                project=state.project,
                incident_source=state.incident_source,
                incident=inp.incident,
                coding_incident=coding_incident,
                incident_confirmation=state.incident_confirmation,
                answer_validation=state.answer_validation,
                team_context=state.team_context
                if state.team_context.model_dump(exclude_none=True)
                else None,
                common_task=task,
                extension_slots=task.slots,
                general_questions=general,
                targeted_questions=targeted,
                hidden_requirements=hidden_requirements(task, targeted),
                evaluation_intent=[
                    f"{c.label}: 근거 자료를 요청하고 AI의 답을 검증하는 과정"
                    for c in inp.chosen.competencies
                ],
                evaluation_criteria=evaluation_criteria(task),
                generation_provenance=provenance,
                review_flags=with_content_flags(
                    [*made.flags, *task.review_flags, *reuse, *seed_flags],
                    task,
                    coding_incident,
                    frame=g.trap_frame,
                ),
                reinforce_questions=questions,
                trap_frame=g.trap_frame,
            )
            state.stage = "task_review"
            state.review_note = None
            state.generation = None
            await self._save(row, row.revision, state, inp.mapping, steps=(STEP_TASK,))
        return True

    async def generation_failed(
        self, interview_id: str, thread_id: str, step: GenerateStep, exc: Exception
    ) -> None:
        """실패 사유를 고정 문구로 남긴다 — 화면은 사유와 '이어서 하기'(그 단계부터)를 보인다."""
        known = isinstance(exc, (*_LLM_ERRORS, InterviewError))
        (logger.warning if known else logger.error)(
            "interview_generate_failed step=%s type=%s reason=%s",
            step,
            type(exc).__name__,
            # 사유는 고정 문구일 때만 로그에 (모델 출력·입력 조각이 섞일 수 있다)
            str(exc) if isinstance(exc, OutputRejected | InterviewTextRejected) else "-",
        )
        async with self._lock(interview_id):
            row = await self.store.get(interview_id)
            g = self._own_generation(row, thread_id)
            if row is None or g is None:
                return
            state = row.state.model_copy(deep=True)
            state.generation = g.model_copy(update={"reason": _generation_reason(exc)})
            await self._save(row, row.revision, state, None, keep_generation=True)

    async def reinforce(
        self, interview_id: str, *, revision: int, answers: dict[str, str | None]
    ) -> dict[str, Any]:
        """⑧ 보강 질문에 답한다 (#308 검사 5) — 답을 가명화해 저장하고, 과제를 버리고 ⑦ 로 (revise common 과 같은 길).
        다음 과제 만들기가 문답을 기능형 재료·재료 분석 입력에 넣는다. 모든 질문에 답이나 '모름'(None·빈 글)을 보낸다.
        답하지 않고 승인해도 된다 — 막지 않는다."""
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(state, "task_review")
            spec = state.spec
            if spec is None or not spec.reinforce_questions:
                raise InterviewInvalid("답할 보강 질문이 없습니다")
            if set(answers) != {q.question_id for q in spec.reinforce_questions}:
                raise InterviewInvalid("모든 보강 질문에 답하거나 '모름'으로 보내 주세요")
            mapping = self._mapping(interview_id)
            done: list[ReinforceAnswer] = []
            for q in spec.reinforce_questions:
                raw = (answers[q.question_id] or "").strip()
                text = await self._pseudonymize(raw, mapping) if raw else None
                if text is not None and len(text) > ANSWER_MAX:
                    raise InterviewInvalid(f"답은 {ANSWER_MAX}자 이내로 적어 주세요")
                done.append(
                    ReinforceAnswer(
                        question_id=q.question_id,
                        question=q.question,
                        answer=text,
                        kind=q.kind,
                        design=q.design,
                        # 설계 질문은 '뺀다/남긴다' 로 시작하는 답만 읽는다 (#318 검사 2) — 고정 답이라 가명화 전 글에서 읽는다
                        choice=design_choice(raw) if q.kind == "design" else None,
                    )
                )
            state.reinforce_answers = done
            state.spec = None
            state.stage = "competencies"
            logger.info(
                "interview_reinforced answered=%d unknown=%d",
                sum(1 for a in done if a.answer),
                sum(1 for a in done if a.answer is None),
            )
            return await self._save(row, revision, state, mapping, steps=(STEP_TASK,))

    async def set_answers(
        self, interview_id: str, *, revision: int, answers: dict[str, str]
    ) -> dict[str, Any]:
        """지원자가 숨은 조건을 직접 짚어 물을 때 줄 답 — 담당자 글이라 가명화한다."""
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(state, "task_review")
            spec = state.spec
            if spec is None:
                raise InterviewInvalid("과제를 먼저 생성해 주세요")
            ids = {q.question_id for q in spec.targeted_questions}
            if not answers or set(answers) - ids or any(not a.strip() for a in answers.values()):
                raise InterviewInvalid("질문 ID와 답을 확인해 주세요")
            mapping = self._mapping(interview_id)
            done = {k: await self._pseudonymize(v, mapping) for k, v in answers.items()}
            try:
                targeted = [
                    TargetedQuestion.model_validate(
                        {**q.model_dump(), "answer": done[q.question_id]}
                    )
                    if q.question_id in done
                    else q
                    for q in spec.targeted_questions
                ]
            except ValidationError:
                raise InterviewInvalid(f"답은 {ANSWER_MAX}자 이내로 적어 주세요") from None
            state.spec = spec.model_copy(
                update={
                    "targeted_questions": targeted,
                    # 답은 지원자가 정확히 물으면 그대로 준다 — 기준 답안을 옮겨 적었는지·토큰이 남았는지 다시 본다
                    "review_flags": with_content_flags(
                        spec.review_flags,
                        spec.common_task,
                        spec.coding_incident,
                        targeted,
                        frame=spec.trap_frame,
                    ),
                }
            )
            return await self._save(row, revision, state, mapping, steps=(STEP_TASK,))

    async def draft_targeted_answer(
        self, interview_id: str, question_id: str, *, revision: int, question: str
    ) -> dict[str, Any]:
        """짚은 질문에 줄 답의 편집용 초안 (저장하지 않음). 내부 자료 id 가 들어가거나 기준 답안을 옮겨 적거나
        너무 길면 한 번 더 받는다 — 두 번 다 걸리면 503 (담당자는 직접 적을 수 있다)."""
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            self._require(row.state, "task_review")
            spec = row.state.spec
            if spec is None:
                raise InterviewInvalid("과제를 먼저 생성해 주세요")
            target = next(
                (q for q in spec.targeted_questions if q.question_id == question_id), None
            )
            if target is None:
                raise InterviewInvalid("현재 과제의 짚은 질문을 선택해 주세요")
            llm, prompts = self._require_step(TARGETED_PROMPT, "답할 범위 초안")
            mapping = self._mapping(interview_id)
            text = await self._pseudonymize(question, mapping)
            if not text:
                raise InterviewInvalid("질문을 입력해 주세요")
            task_json = spec.common_task.model_dump_json(
                include={
                    "scenario": True,
                    "requirements": {"__all__": {"text"}},
                    "facts": {"__all__": {"topic", "ask_when", "answer", "answer_hint"}},
                    "clarifications": {
                        "__all__": {"topic", "ask_when", "match_terms", "answer", "answer_hint"}
                    },
                    "artifacts": {"__all__": {"title", "content"}},
                }
            )
            check = self._check(mapping)
            try:
                prompt = assemble(
                    prompts.get(TARGETED_PROMPT),
                    TargetedAnswerDraftInput(
                        question=self.pseudo.stored(text, mapping),
                        hidden_condition=self.pseudo.stored(target.hidden_condition, mapping),
                        task_json=self.pseudo.stored(task_json, mapping),
                    ),
                    ontology_version=spec.ontology_version,
                )
                for _ in range(2):
                    result = await self._llm(llm.call(prompt, TargetedAnswerDraftOutput))
                    answer = check(result.parsed.answer)
                    if mentions_artifact_id(answer):
                        reason = "artifact_id"
                    elif copies_answer(answer, spec.coding_incident):
                        reason = "answer_copy"
                    elif len(answer) > ANSWER_MAX:
                        reason = "too_long"
                    else:
                        await asyncio.to_thread(self.pseudo.save, interview_id, mapping)
                        return {"question_id": question_id, "answer": answer}
                    logger.info("interview_targeted_retry reason=%s", reason)
            except _LLM_ERRORS as exc:
                logger.warning("interview_targeted_rejected type=%s", type(exc).__name__)
            raise InterviewUnavailable("답할 범위 초안을 만들지 못했습니다. 다시 시도해 주세요")

    async def _revise_task(
        self,
        state: InterviewState,
        mapping: RunMapping,
        *,
        task: TaskItem | None,
        general: list[GeneralQuestion] | None,
        targeted: list[TargetedQuestion] | None,
        target: Literal["common", "questions"],
        interview_id: str,
    ) -> None:
        """⑧ 과제 검토에서 고치기.
        common: 공통 과제 JSON — 뼈대(task_frame)는 고정, 바뀐 글만 가명화해 새 초안 판으로 저장. 자료·숨은 조건이 그대로면
          담당자가 다듬은 질문·정한 답을 두고, 바뀌었으면 질문을 다시 만든다(답은 비움). 평가 기준·글 표시를 다시 계산한다.
        questions: 일반·짚은 질문 — 짚은 질문의 답은 지운다(의도). 답은 /answers 로만 넣어 누설 검사를 거친다."""
        spec = state.spec
        if target == "common" and (task is None or spec is None):
            state.spec = None
            state.stage = "competencies"
            return
        if spec is None:
            raise InterviewInvalid("질문 목록을 먼저 생성해 주세요")
        if target == "common":
            assert task is not None
            old = spec.common_task
            if task_frame(task) != task_frame(old):
                raise InterviewInvalid(
                    "공통 과제의 트랙·유형·측정 역량·사례·기술 환경·확장 슬롯 구성은 바꿀 수 없습니다"
                )
            if task.behavioral_anchors is None:
                raise InterviewInvalid("평가 행동 기준은 비울 수 없습니다")
            _, common_store = self._require_tasks()
            edited = await self._apply_edit(
                TaskItem, task.model_dump(mode="json"), old.model_dump(mode="json"), mapping
            )
            new_general, new_targeted = questions_for(edited)
            if not new_general or not new_targeted:
                raise InterviewInvalid(
                    "자료와 숨은 조건(관찰할 개념 또는 함정)이 있어야 지원자 질문을 만들 수 있습니다"
                )
            if broken := _broken_links(old, edited):
                # 생성 때 명세 검사의 연결 규칙(숨은 요구마다 숨김 수용 기준·허용 설계 2개 이상)을 고친 글에도 — 빌드의 숨은 테스트·허용
                # 구현은 이 글로 만든다 (설계 검사 B-F5). 고친 글이 새로 깨뜨린 것만 막는다 (예전 판에 이미 있던 것은 그대로)
                raise InterviewInvalid(
                    "숨은 요구·숨김 수용 기준·허용 설계 연결을 고쳐 주세요 — " + "; ".join(broken)
                )
            artifact_ids = {a.artifact_id for a in edited.artifacts}
            general_q = (
                spec.general_questions
                if all(set(q.artifact_ids) <= artifact_ids for q in spec.general_questions)
                else new_general
            )
            same_hidden = [q.hidden_condition for q in new_targeted] == [
                q.hidden_condition for q in questions_for(old)[1]
            ]
            targeted_q = spec.targeted_questions if same_hidden else new_targeted
            # 함정 틀은 재료·템플릿에서 정해지고 글 고치기로 바뀌지 않는다 (#318 D1) — 틀 적용 재료를 새 판이 이어받는다.
            # 승인은 최신 판을 승인한 뒤 다른 초안 판을 지우므로, 여기서 옮기지 않으면 틀을 가진 생성 판과 함께 사라진다 (설계 검사 S-F1)
            framed = await common_store.framed(old.task_id)
            saved = await common_store.save_draft(
                CommonKey(spec.posting_id, spec.ontology_version, f"interview:{interview_id}"),
                edited,
                spec.generation_provenance,
                framed=framed,
            )
            new_task = saved.task
            old_flags = {*old.review_flags, *(f for slot in old.slots for f in slot.review_flags)}
            new_flags = [
                *new_task.review_flags,
                *(f for slot in new_task.slots for f in slot.review_flags),
            ]
            # 생성기 표시(과제 밖)는 두되, 서술 실패·자동 검증 실패 표시는 담당자가 고친 글에 더는 없으면 뺀다
            kept = [
                f
                for f in spec.review_flags
                if f not in old_flags
                and not (
                    f.startswith((FLAG_DRAFT_FAILED, FLAG_DRAFT_INVALID, FLAG_DRAFT_INVALID_LEGACY))
                    and f not in new_flags
                )
            ]
            state.spec = spec.model_copy(
                update={
                    "common_task": new_task,
                    "extension_slots": new_task.slots,
                    "general_questions": general_q,
                    "targeted_questions": targeted_q,
                    "hidden_requirements": hidden_requirements(new_task, targeted_q),
                    "evaluation_criteria": evaluation_criteria(new_task),
                    "review_flags": with_content_flags(
                        [
                            *kept,
                            *new_flags,
                            *(
                                [FLAG_FRAME_EDITED]
                                if spec.trap_frame is not None and _hidden_edited(old, new_task)
                                else []
                            ),
                        ],
                        new_task,
                        spec.coding_incident,
                        targeted_q,
                        frame=spec.trap_frame,
                    ),
                }
            )
        else:
            general = general or spec.general_questions
            targeted = targeted or spec.targeted_questions
            artifact_ids = {a.artifact_id for a in spec.common_task.artifacts}
            if (
                not general
                or not targeted
                or any(not set(q.artifact_ids) <= artifact_ids for q in general)
            ):
                raise InterviewInvalid(
                    "일반 질문은 실제 자료 ID에 연결하고 짚은 질문을 한 개 이상 남겨 주세요"
                )
            if len({q.question_id for q in targeted}) != len(targeted):
                raise InterviewInvalid("짚은 질문 ID가 중복되었습니다")
            old_general = [q.model_dump() for q in spec.general_questions]
            edited_general = await self._apply_edit(
                _GeneralQuestions, [q.model_dump() for q in general], old_general, mapping
            )
            before = {q.question_id: q.model_dump() for q in spec.targeted_questions}
            edited_targeted = await self._apply_edit(
                _TargetedQuestions,
                [q.model_copy(update={"answer": None}).model_dump() for q in targeted],
                [{**q, "answer": None} for q in before.values()],  # question_id 로 짝짓는다
                mapping,
            )
            state.spec = spec.model_copy(
                update={
                    "general_questions": edited_general.root,
                    "targeted_questions": edited_targeted.root,
                    "hidden_requirements": hidden_requirements(
                        spec.common_task, edited_targeted.root
                    ),
                    "review_flags": with_content_flags(
                        spec.review_flags,
                        spec.common_task,
                        spec.coding_incident,
                        edited_targeted.root,
                        frame=spec.trap_frame,
                    ),
                }
            )
        state.stage = "task_review"

    # ================================================================ 사례 출처 (D2c, #266)
    @staticmethod
    def _require_interview_case(state: InterviewState) -> None:
        """사례 대화·처음 대응·성공 기준·답안 검증은 인터뷰 사례만 — 고른 공개·회사 사례를 바꾸지 않는다 (code review W1·W2)."""
        if state.incident_source != "known_problem":
            raise InterviewInvalid(
                "공개·회사 사례를 고른 상태입니다. 인터뷰 사례로 바꾸려면 사례 출처를 고르세요"
            )

    def _public_candidates(self, sub: str) -> list[PublicIncident]:
        catalog = self.public_incidents or {}
        pool = [
            *([catalog[sub]] if sub in catalog else []),
            *getattr(catalog, "alternates", {}).get(sub, []),
        ]
        return sorted({i.incident_id: i for i in pool}.values(), key=lambda i: i.incident_id)

    async def _company_case(self, incident_id: str, state: InterviewState) -> PublicIncident:
        """이 공고 회사·직군의 공유 승인된 사례 — 아니면 InterviewInvalid (다른 회사 사례·공유 승인 전·보관된 사례는 쓰지 않는다)."""
        company = company_key(await self.postings.company(state.posting_id))
        rec = (
            await self.company_incidents.get(incident_id)
            if self.company_incidents is not None and company is not None
            else None
        )
        if (
            rec is None
            or rec.company_id != company
            or rec.sub != state.incident_sub
            or rec.status != "shared"
        ):
            raise InterviewInvalid("이 공고 회사의 공유 승인된 사례가 아닙니다")
        return rec.incident

    async def incident_options(self, interview_id: str) -> dict[str, Any]:
        """고를 수 있는 사례 — 직군의 공개 사례와 같은 회사의 공유 승인된 사례. 기능형 재료가 직군 템플릿에 맞아야 쓸 수 있다(usable).
        회사 사례는 다른 공고가 쓰는 수를 함께 보인다 (두 공고에 모두 지원한 사람은 함정·정답을 안다)."""
        row = await self._load(interview_id)
        state = row.state
        sub = state.incident_sub
        company = company_key(await self.postings.company(state.posting_id))
        shared = (
            await self.company_incidents.shared(company, sub)
            if self.company_incidents is not None and company is not None
            else []
        )
        used = (
            await self.common_store.incident_usage(
                [r.incident_id for r in shared], exclude_posting=state.posting_id
            )
            if self.common_store is not None and shared
            else {}
        )
        return {
            "interview_id": interview_id,
            "revision": row.revision,
            "incident_source": state.incident_source,
            "sub": sub,
            "company_id": company,
            "public": [
                {
                    "incident_id": i.incident_id,
                    "title": i.title,
                    "tech": i.tech,
                    "usable": feature_fit(i, sub),
                }
                for i in self._public_candidates(sub)
            ],
            "company": [
                {
                    "incident_id": r.incident_id,
                    "title": r.incident.title,
                    "tech": r.incident.tech,
                    "usable": feature_fit(r.incident, sub),
                    "from_interview": r.source.get("kind") == "interview",
                    "used_by_postings": used.get(r.incident_id, 0),
                }
                for r in shared
            ],
        }

    async def select_incident_source(
        self,
        interview_id: str,
        *,
        revision: int,
        source: IncidentSource,
        incident_id: str | None,
    ) -> dict[str, Any]:
        """사례 출처 고르기 (포크 POST /incident/source). 인터뷰 사례면 사례 대화로, 공개·회사 사례면 그 사례를 확인 단계에 올린다.
        사례·답안·과제는 비우고 대화(메시지)는 둔다 — 인터뷰 사례로 돌아오면 이어서 쓴다."""
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(
                state,
                "incident_interview",
                "trap_interview",
                "success_interview",
                "incident_review",
                "competencies",
                "task_review",
            )
            sub = state.incident_sub
            chosen: PublicIncident | None = None
            if source == "known_problem":
                if incident_id:
                    raise InterviewInvalid("인터뷰 사례에는 사례 id 를 보내지 않습니다")
                if state.incident_source == "known_problem":
                    # 이미 인터뷰 사례 — 진행 중인 입력을 지우지 않는다 (code review W3)
                    return self.view(row)
            elif source == "public_fixed":
                catalog = self.public_incidents or {}
                chosen = incident_by_id(catalog, incident_id) if incident_id else catalog.get(sub)
                if chosen is None or chosen.sub != sub or chosen.incident_id.startswith("co:"):
                    raise InterviewInvalid("이 직군의 공개 사례가 아닙니다")
            else:
                if not incident_id:
                    raise InterviewInvalid("회사 사례를 골라 주세요")
                chosen = await self._company_case(incident_id, state)
            if chosen is not None and not feature_fit(chosen, sub):
                raise InterviewInvalid(
                    "이 사례에는 직군 코딩 템플릿으로 시험할 기능형 재료가 없습니다. 다른 사례를 골라 주세요"
                )
            messages, provenance = state.incident_messages, state.incident_provenance
            state.reset_incident()
            state.incident_messages, state.incident_provenance = messages, provenance
            state.review_note = None  # 앞 사례로 만든 과제의 수정 요청 — 새 사례에 넣지 않는다
            if chosen is None:
                state.stage = "incident_interview"
            else:
                state.incident_source = source
                # 다른 공고의 기준 답안이 남아 있어도 싣지 않는다 (회사 사례는 등록 때 이미 뺀다)
                state.incident_draft = chosen.model_copy(update={"known_incident_cases": []})
                state.stage = "incident_review"
            return await self._save(row, revision, state, None)

    async def _confirm_chosen(
        self,
        row: InterviewRow,
        revision: int,
        state: InterviewState,
        incident: PublicIncident,
        reviewer: str | None,
    ) -> dict[str, Any]:
        """공개·회사 사례 확인 — 고치지 않는다 (고치려면 인터뷰 사례로). 기준 답안 검증이 없다."""
        draft = state.incident_draft
        if draft is None:
            raise InterviewInvalid("사례를 먼저 골라 주세요")
        # 저장된 원본(공개 사례 파일·회사 사례 DB)과 비교한다 — 상태의 draft 만 보면 그 draft 가 바뀐 경우를 못 막는다
        if state.incident_source == "company":
            original = await self._company_case(draft.incident_id, state)
        else:
            found = incident_by_id(self.public_incidents or {}, draft.incident_id)
            if found is None:
                raise InterviewInvalid("이 직군의 공개 사례가 아닙니다")
            original = found
        if incident != draft or draft != original.model_copy(update={"known_incident_cases": []}):
            raise InterviewInvalid(
                "공개·회사 사례는 고치지 않습니다. 고치려면 인터뷰 사례로 다시 작성해 주세요"
            )
        if reviewer is not None and not reviewer.strip():
            raise InterviewInvalid("사례 확인자를 입력해 주세요")
        state.incident_confirmed = draft
        state.incident_confirmation = IncidentConfirmation(
            reviewer=reviewer.strip() if reviewer is not None else None,
            at=self.now().isoformat(),
            revision=revision,
            sha256=incident_digest(draft),
        )
        state.stage = "competencies"
        return await self._save(row, revision, state, None, steps=(STEP_REVIEW,))

    # ================================================================ ⑨ 승인 (D2b, #266) — 그래프 graph/interview_approval.py 가 부른다
    def _registrable(self, incident: PublicIncident) -> None:
        """회사 사례로 등록할 수 있는 내용인가 (연락처·링크) — 승인 때 막히지 않게 과제를 만들 때 먼저 본다.
        기능형 재료(feature)는 LLM 이 쓴 글이라 다시 받으면 되고(OutputRejected), 확인한 사례 쪽이면 사례를 고쳐야 한다."""
        found = blocked_findings(incident, self.detect)
        if any(f.field == "feature" for f in found):
            raise OutputRejected("기능형 재료에 연락처·링크 형식이 있습니다")
        if found:
            raise TaskGenerationFailed(
                "확인한 사례에 연락처·링크 형식이 있어 회사 사례로 등록할 수 없습니다. 사례를 고쳐 주세요"
            )

    async def _approval_check(
        self, row: InterviewRow
    ) -> tuple[list[str], StoredCommon | None, str | None]:
        """(승인할 수 없는 사유, 지금 승인된 인터뷰 과제, 공고 회사 id) — 읽기만."""
        state = row.state
        spec = state.spec
        stored = current = None
        if spec is not None and self.common_store is not None:
            stored = await self.common_store.reusable(
                CommonKey(spec.posting_id, spec.ontology_version, interview_ref(row.id))
            )
            current = await self.common_store.interview_task(spec.posting_id, spec.ontology_version)
        company = company_key(await self.postings.company(state.posting_id))
        interview = state.incident_source == "known_problem"
        blocked = (
            await asyncio.to_thread(blocked_findings, spec.coding_incident, self.detect)
            if spec is not None and interview
            else []
        )
        source_problem = None
        if spec is not None and state.incident_source == "company":
            try:
                await self._company_case(spec.coding_incident.incident_id, state)
            except InterviewInvalid:
                source_problem = "고른 회사 사례를 더 쓸 수 없습니다(보관·반려). 다른 사례를 골라 과제를 다시 만들어 주세요"
        framed_missing = (
            spec is not None
            and spec.trap_frame is not None
            and stored is not None
            and self.common_store is not None
            and await self.common_store.framed(stored.id) is None
        )
        problems = approval_problems(
            state, stored, company, blocked, source_problem, framed_missing=framed_missing
        )
        if self.approvals is None or self.common_store is None:
            problems.append("승인 저장소가 준비되지 않았습니다")
        if self._generating(
            row
        ):  # 과제를 다시 만드는 중 — 끝나면 판본이 바뀌어 지금 과제는 낡는다 (#280)
            problems.append(_GENERATING)
        return problems, current, company

    async def approval_context(self, interview_id: str, revision: int) -> dict[str, Any] | None:
        """승인 대기 내용 (interrupt 앞 — 읽기만). 그 판본이 지금 과제 검토 단계가 아니면 None (stale).
        인터뷰·과제 본문은 싣지 않는다 — 체크포인트에 사본을 두지 않는다 (화면은 GET /interviews/{id} 로 본다)."""
        row = await self.store.get(interview_id)
        if row is None or row.revision != revision or row.state.stage != "task_review":
            return None
        problems, current, company = await self._approval_check(row)
        spec = row.state.spec
        return {
            "interview_id": interview_id,
            "revision": revision,
            "posting_id": row.state.posting_id,
            "ontology_version": spec.ontology_version if spec else None,
            "common_id": spec.common_task.task_id if spec else None,
            "incident_id": spec.coding_incident.incident_id if spec else None,
            "company_id": company,
            "review_flag_count": len(spec.review_flags) if spec else 0,
            # 교체 대상 — 이 공고(같은 온톨로지 판)에 이미 승인된 인터뷰 과제. 그 과제로 진행한 지원자 수는 [미정]
            "current": {
                "common_id": current.id,
                "approved_by": current.approved_by,
                "approved_at": current.approved_at.isoformat() if current.approved_at else None,
            }
            if current is not None
            else None,
            "problems": problems,
        }

    async def finalize_approval(
        self, interview_id: str, revision: int, decision: InterviewApprovalDecision
    ) -> ApprovalOutcome:
        """승인 확정 — 인터뷰 잠금 안에서 다시 검사하고 한 트랜잭션으로 확정, 커밋 뒤 대응표를 지운다.
        판본이 바뀌었으면 stale, 교체 충돌·검사 실패면 사유를 붙여 다시 기다린다."""
        async with self._lock(interview_id):
            row = await self.store.get(interview_id)
            if row is None or row.revision != revision or row.state.stage != "task_review":
                return ApprovalOutcome(result="stale")
            problems, _, company = await self._approval_check(row)
            spec = row.state.spec
            interview = row.state.incident_source == "known_problem"
            if (
                problems
                or spec is None
                or (interview and company is None)
                or self.approvals is None
            ):
                return ApprovalOutcome(error="승인할 수 없습니다: " + "; ".join(problems[:3]))
            state = row.state.model_copy(deep=True)
            state.stage = "approved"
            state.review_note = None
            # 실패·중단한 다시 만들기의 산출물 사본을 닫힌 인터뷰에 남기지 않는다 (#280)
            state.generation = None
            state.approval = InterviewApprovalRecord(
                reviewer=decision.reviewer,
                at=decision.at.isoformat(),
                revision=revision,
                common_id=spec.common_task.task_id,
                incident_id=spec.coding_incident.incident_id,
                incident_registered=interview,
                replaced=decision.replaces,
                comment=decision.comment,
            )
            commit = ApprovalCommit(
                interview_id=interview_id,
                revision=revision,
                state=state,
                company_id=company if interview else None,
                # 기준 답안·인터뷰 본문(known_incident_cases)은 회사 사례에 넣지 않는다 — 채점 근거이고, 사례 모음·/incidents 로
                # 다른 공고 담당자에게 보일 이유가 없다 (빌더·다른 공고 생성은 쓰지 않음). 인터뷰 상태에는 남는다 (privacy-auditor M1)
                incident=spec.coding_incident.model_copy(update={"known_incident_cases": []})
                if interview
                else None,  # 공개·회사 사례는 이미 있다 — 새로 등록하지 않는다
                common_id=spec.common_task.task_id,
                reviewer=decision.reviewer,
                at=decision.at,
                replaces=decision.replaces,
            )
            try:
                ok = await self.approvals.commit(commit)
            except (
                InterviewTaskConflict
            ) as exc:  # 교체 미명시·그 사이 다른 승인 — 메시지는 과제 id 뿐
                return ApprovalOutcome(error=str(exc))
            except (
                KeyError,
                ValueError,
            ) as exc:  # 저장본 없음·사례 id 중복·교체된 과제 — 메시지는 고정 문구만
                logger.warning(
                    "interview_approve_rejected id=%s type=%s", interview_id, type(exc).__name__
                )
                return ApprovalOutcome(
                    error="공통 과제 저장본을 찾을 수 없습니다. 과제를 다시 생성해 주세요"
                    if isinstance(exc, KeyError)
                    else "승인을 저장하지 못했습니다 — 같은 사례가 이미 등록됐거나 교체된 과제입니다. 새로고침 후 다시 시도해 주세요"
                )
            if not ok:
                return ApprovalOutcome(result="stale")
            if row.state.generation is not None:
                await self._forget_generation(row.state.generation.thread_id)
            try:
                # 결정 4 — 승인하면 대응표 삭제
                await asyncio.to_thread(self.pseudo.delete, interview_id)
            except OSError as exc:  # 승인은 됐다 — 닫힌 인터뷰의 대응표는 정리 작업이 다시 지운다
                logger.error(
                    "interview_mapping_delete_failed id=%s type=%s",
                    interview_id,
                    type(exc).__name__,
                )
            logger.info(
                "interview_approved id=%s common=%s incident=%s replaced=%s",
                interview_id,
                spec.common_task.task_id,
                spec.coding_incident.incident_id,
                decision.replaces,
            )
            return ApprovalOutcome(result="approved", common_id=spec.common_task.task_id)

    async def reject_approval(
        self, interview_id: str, revision: int, decision: InterviewApprovalDecision
    ) -> InterviewApprovalResult:
        """반려 — 사유(가명화한 글)를 수정 요청(review_note)으로 남기고 과제 검토 단계 그대로. 판본이 올라가 새 승인 thread 가 된다."""
        async with self._lock(interview_id):
            row = await self.store.get(interview_id)
            if row is None or row.revision != revision or row.state.stage != "task_review":
                return "stale"
            state = row.state.model_copy(deep=True)
            state.review_note = decision.comment
            updated = await self.store.save(interview_id, revision, state)
            return "rejected" if updated is not None else "stale"

    async def pseudonymize_note(self, interview_id: str, text: str) -> str:
        """승인·반려 사유를 resume 전에 가명화한다 — 체크포인트에 원문이 남지 않게, 반려 사유는 다음 생성의 수정 요청으로 간다.
        새 토큰이 생기면 대응표를 바로 저장한다 (토큰은 더하기만)."""
        async with self._lock(interview_id):
            await self._load(interview_id)
            mapping = self._mapping(interview_id)
            out = await self._pseudonymize(text, mapping)
            await asyncio.to_thread(self.pseudo.save, interview_id, mapping)
            return out

    async def interview_build(
        self, interview_id: str, *, revision: int, requested_by: str | None
    ) -> dict[str, Any]:
        """승인한 과제의 시작 코드 빌드 (포크 POST /interviews/{id}/builds) — 이미 만든 빌드(검토·승인·배포)나
        진행 중 빌드가 있으면 그것. 승인과 따로 둔다 [확정: 2026-11-19 사용자 — 포크처럼 별도 요청]."""
        row = await self._load(interview_id, open_only=False)
        self._expect_revision(row, revision)
        approval = row.state.approval
        if row.state.stage != "approved" or approval is None:
            raise InterviewConflict("승인한 인터뷰에서만 빌드를 요청할 수 있습니다")
        if self.builds is None:
            raise InterviewNotReady("코딩 과제 빌더가 준비되지 않았습니다")
        ready = next(
            (
                b
                for b in await self.builds.builds(approval.common_id)
                if b.status in ("review", "approved", "published")
            ),
            None,
        )
        if ready is not None:
            build, created = ready, False
        else:
            try:
                build, created = await self.builds.request_build(
                    approval.common_id, requested_by or approval.reviewer
                )
            except (KeyError, BuildRuleError):
                raise InterviewConflict(
                    "이 과제는 빌드를 요청할 수 없습니다 — 다른 인터뷰 과제로 교체됐는지 확인해 주세요"
                ) from None
        return {
            "build_id": build.build_id,
            "status": build.status,
            "created": created,
        }

    # ================================================================ 앞 단계로·임시 저장·폐기
    async def revise(
        self,
        interview_id: str,
        *,
        revision: int,
        target: RevisionTarget,
        note: str,
        text: str | None = None,
        task: TaskItem | None = None,
        general_questions: list[GeneralQuestion] | None = None,
        targeted_questions: list[TargetedQuestion] | None = None,
    ) -> dict[str, Any]:
        if target == "project":
            if text is None:
                raise InterviewInvalid("프로젝트 수정 내용이 필요합니다")
            return await self.project(interview_id, text=text, revision=revision, note=note)
        async with self._lock(interview_id):
            row = await self._load(interview_id)
            self._expect_revision(row, revision)
            state = row.state.model_copy(deep=True)
            self._require(
                state,
                "incident_interview",
                "trap_interview",
                "success_interview",
                "incident_review",
                "competencies",
                "task_review",
            )
            mapping = self._mapping(interview_id)
            state.review_note = await self._pseudonymize(note, mapping)
            if target in ("common", "questions"):
                self._require(state, "competencies", "task_review")
                await self._revise_task(
                    state,
                    mapping,
                    task=task,
                    general=general_questions,
                    targeted=targeted_questions,
                    target=target,
                    interview_id=interview_id,
                )
                return await self._save(row, revision, state, mapping, steps=(STEP_TASK,))
            if target in ("trap", "success") and state.incident_source != "known_problem":
                raise InterviewInvalid(
                    "공개·회사 사례에는 처음 대응·성공 기준 단계가 없습니다. 인터뷰 사례로 바꾸려면 사례 출처를 고르세요"
                )
            state.unconfirm()
            if target == "success":
                state.answer_validation = None
                state.stage = "success_interview"
            elif target == "trap":
                state.answer_validation = None
                state.stage = "trap_interview"
            else:
                state.stage = "incident_review" if state.incident_draft else "incident_interview"
                # 초안 전(④ 에서 돌아옴)에는 ③ 의 마지막 글이 담당자 답이거나 ④ 안내라 답할 질문이 없다
                last = state.incident_messages[-1] if state.incident_messages else None
                if state.incident_draft is None and (
                    last is None or last.role != "assistant" or last.text == TRAP_RETRY_NOTICE
                ):
                    state.incident_messages = [
                        *state.incident_messages,
                        ChatMessage(role="assistant", text=CASE_REOPEN_QUESTION),
                    ]
            return await self._save(row, revision, state, mapping)

    async def save_drafts(
        self, interview_id: str, *, draft_revision: int, steps: dict[str, Any]
    ) -> dict[str, Any]:
        """화면의 단계별 임시 저장 (원문, 최대 하루). 업무 판본(revision)과 따로 센다."""
        row = await self._load(interview_id)
        updated = await self.store.save_drafts(
            row.id, draft_revision, steps, cutoff=self.now() - self.draft_ttl
        )
        if updated is None:
            raise InterviewConflict(
                "다른 화면에서 작성 내용이 변경됐습니다. 새로고침 후 확인해 주세요"
            )
        return {"ui_drafts": updated.ui_drafts, "draft_revision": updated.draft_revision}

    async def discard(self, interview_id: str) -> dict[str, Any]:
        """담당자가 인터뷰를 폐기한다 — 이름 대응표·임시 저장을 지운다. 가명화한 대화는 남는다."""
        async with self._lock(interview_id):
            row = await self._load(interview_id, open_only=False)
            self._ensure_idle(row)
            if row.state.stage == "approved":
                raise InterviewClosed(
                    "승인한 인터뷰는 폐기하지 않습니다. 과제를 바꾸려면 새 인터뷰로 교체해 주세요"
                )
            if row.state.stage in CLOSED_STAGES:
                return self.view(row)
            view = await self._close(row, "recruiter")
        return view

    async def delete(
        self,
        interview_id: str,
        *,
        forget_approvals: Callable[[int], Awaitable[None]] | None = None,
    ) -> int:
        """담당자가 인터뷰를 지운다 — 폐기한 것, 또는 진행 중인 것(폐기 정리를 먼저). 대응표 폴더·생성 thread·승인 thread·초안 판, 마지막에 행.

        승인한 인터뷰는 승인한 과제의 근거(누가 무엇을 보고 승인했는지)라 지우지 않는다 [확정: 2026-11-21 사용자].
        멱등인 정리를 먼저 하고 행은 마지막에 지운다 — 정리가 실패하면 500 이고 행이 남아 다시 지울 수 있다 (개인정보 점검 M3).
        forget_approvals(마지막 판본) 는 API 가 넘긴다 — 승인 thread 는 승인 그래프(API 계층)의 것이고 판본마다 하나다.
        지운 행의 마지막 판본을 돌려준다.
        """
        async with self._lock(interview_id):
            row = await self._load(interview_id, open_only=False)
            self._ensure_idle(row)
            if row.state.stage == "approved":
                raise InterviewClosed(
                    "승인한 인터뷰는 지우지 않습니다 — 승인한 과제의 근거입니다. "
                    "과제를 바꾸려면 새 인터뷰로 교체해 주세요"
                )
            if row.state.stage not in CLOSED_STAGES:
                await self._close(row, "recruiter")
                row = await self._load(interview_id, open_only=False)
            # 폐기 때 지운 것도 다시 — 그때 실패했을 수 있다 (없으면 아무것도 안 함). 생성 thread 는 생성 표시를 저장한 판본마다 하나
            try:
                await asyncio.to_thread(self.pseudo.delete, row.id)
                if self.generation is not None:
                    for revision in range(1, row.revision + 1):
                        await self.generation.forget(generation_thread_id(row.id, revision))
                if forget_approvals is not None:
                    await forget_approvals(row.revision)
                if self.common_store is not None:
                    await self.common_store.drop_interview_drafts(interview_ref(row.id))
            except Exception as exc:  # noqa: BLE001 — 어느 정리든 실패하면 행을 남긴다
                logger.error("interview_delete_failed id=%s type=%s", row.id, type(exc).__name__)
                raise InterviewDeleteFailed(
                    "인터뷰 정리를 끝내지 못했습니다. 인터뷰는 남아 있으니 다시 시도해 주세요"
                ) from None
            if not await self.store.delete(row.id, row.revision):
                raise InterviewConflict(_STALE)
        logger.info("interview_deleted id=%s revision=%d", row.id, row.revision)
        return row.revision

    async def _close(
        self, row: InterviewRow, reason: Literal["recruiter", "stale"]
    ) -> dict[str, Any]:
        state = row.state.model_copy(deep=True)
        state.stage = "discarded"
        state.discarded_at = self.now().isoformat()
        state.discard_reason = reason
        state.generation = None  # 실패·중단한 과제 만들기의 산출물 사본도 지운다 (#280 — 초안 판을 지우는 것과 같은 이유)
        updated = await self.store.save(row.id, row.revision, state, clear_all_drafts=True)
        if updated is None:
            raise InterviewConflict(_STALE)
        if row.state.generation is not None:
            await self._forget_generation(row.state.generation.thread_id)
        await asyncio.to_thread(self.pseudo.delete, row.id)
        if self.common_store is not None:
            # 폐기한 인터뷰의 과제 초안 판 — 가명화한 글뿐이지만 쓸 곳이 없다 (D2b [확정: 2026-11-19 사용자])
            try:
                await self.common_store.drop_interview_drafts(interview_ref(row.id))
            except Exception as exc:  # noqa: BLE001 — 폐기는 이미 됐다. 남은 초안은 지원자 실행이 쓰지 않는다
                logger.error(
                    "interview_drafts_drop_failed id=%s type=%s", row.id, type(exc).__name__
                )
        logger.info("interview_discarded id=%s reason=%s", row.id, reason)
        return self.view(updated)

    async def sweep(self) -> dict[str, int]:
        """보관 기간 정리 (결정 4): 하루 지난 원문 임시 저장, 30일 방치 인터뷰(폐기로 보고 대응표 삭제), 주인 없는 대응표."""
        now = self.now()
        drafts = await self.store.purge_drafts(now - self.draft_ttl)
        cutoff = now - self.mapping_ttl
        stale = 0
        for interview_id in await self.store.stale(cutoff):
            async with self._lock(interview_id):
                row = await self.store.get(interview_id)
                if row is None or row.state.stage in CLOSED_STAGES or row.updated_at >= cutoff:
                    continue
                try:
                    await self._close(row, "stale")
                    stale += 1
                except InterviewConflict:
                    continue  # 그사이 누가 진행했다 — 다음 정리 때 다시 본다
        orphans = 0
        for interview_id in await asyncio.to_thread(self.pseudo.mapped_ids):
            async with self._lock(interview_id):
                row = await self.store.get(interview_id)
                if row is None or row.state.stage in CLOSED_STAGES:
                    if await asyncio.to_thread(self.pseudo.delete, interview_id):
                        orphans += 1
        if drafts or stale or orphans:
            logger.info(
                "interview_sweep drafts=%d stale=%d orphan_mappings=%d", drafts, stale, orphans
            )
        return {"drafts": drafts, "stale": stale, "orphan_mappings": orphans}


async def sweep_forever(service: InterviewService, every: timedelta) -> None:
    """앱 수명 동안 주기적으로 정리한다 (lifespan 이 취소한다). 한 번 실패해도 다음 주기에 다시 한다."""
    while True:
        try:
            await service.sweep()
        except Exception as exc:  # noqa: BLE001 — 정리 실패로 앱을 멈추지 않는다
            logger.error("interview_sweep_failed type=%s", type(exc).__name__)
        await asyncio.sleep(every.total_seconds())
