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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal, Protocol, TypeVar

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
from task_spec.interview.errors import (
    InterviewClosed,
    InterviewConflict,
    InterviewError,
    InterviewInvalid,
    InterviewNotFound,
    InterviewNotReady,
    InterviewUnavailable,
    OutputRejected,
    PostingMissing,
)
from task_spec.interview.incident import (
    align_incident_evidence,
    attach_interview_cases,
    edited_incident,
    incident_digest,
    prepare_incident,
    validate_incident,
)
from task_spec.interview.store import CLOSED_STAGES, InterviewRepo, InterviewRow
from task_spec.interview.stream import QuestionStream
from task_spec.interview.text import (
    CASE_OPENING,
    SCOPE_QUESTIONS,
    UNSET,
    numbers,
    parse_project,
    posting_team_context,
)
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
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import PostingProfile, PublicIncident
from task_spec.schemas.interview_answer import AnswerValidationRecord
from task_spec.schemas.interview_incident import IncidentInterviewInput, IncidentInterviewOutput
from task_spec.schemas.interview_state import (
    ChatMessage,
    IncidentConfirmation,
    InterviewState,
    PostingContext,
    Stage,
    SuccessAnswers,
    TrapAnswers,
)
from task_spec.schemas.pseudo_text import PseudonymizedText
from task_spec.spec.company_incidents import new_incident_id

logger = logging.getLogger(__name__)

INCIDENT_PROMPT = "interview_incident_extended"
D1_PROMPTS = (INCIDENT_PROMPT, ANSWER_PROMPT, CHECK_PROMPT)
_LLM_ERRORS = (SchemaViolation, EmptyResponse, TransientLLMError, ValueError)
# 화면의 9단계 번호 (포크 DESIGN.md) — 그 단계를 제출하면 그 단계의 원문 임시 저장을 지운다
STEP_CHAT, STEP_TRAP, STEP_SUCCESS, STEP_REVIEW = "3", "4", "5", "6"
SUCCESS_FIELDS = ("reproduction", "expected_result", "invariants", "verification", "alternatives")
TRAP_FIELDS = ("attempted_fix", "observed_result", "why_failed")
RevisionTarget = Literal["project", "incident", "trap", "success"]
_STALE = "인터뷰 판본이 바뀌었습니다. 새로고침 후 다시 시도해 주세요"
NOTE_TRAP_UNKNOWN = "실제 실패 시도는 확인되지 않음. 흔한 대응은 가상 함정 후보입니다."
T = TypeVar("T")
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


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
        }

    async def _load(self, interview_id: str, *, open_only: bool = True) -> InterviewRow:
        row = await self.store.get(interview_id)
        if row is None:
            raise InterviewNotFound("인터뷰를 찾을 수 없습니다")
        if open_only and row.state.stage in CLOSED_STAGES:
            raise InterviewClosed("폐기된 인터뷰입니다. 새 인터뷰를 시작해 주세요")
        return row

    @staticmethod
    def _expect_revision(row: InterviewRow, revision: int) -> None:
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
    ) -> dict[str, Any]:
        self._expect_revision(row, revision)
        if mapping is not None:
            # 대응표를 DB 보다 먼저 저장한다. 대응표는 토큰을 더하기만 하고 같은 인터뷰 요청은 잠금으로 한 줄이라, 이 대응표는
            # 디스크의 것을 포함한다 — DB 저장이 충돌·취소·실패해도 쓰지 않는 토큰이 남을 뿐이다. 반대 순서면 DB 만 저장되고
            # 대응표가 빠진 채 끊길 수 있고, 그 뒤 모든 LLM 단계가 '없는 토큰'으로 막힌다 (code review W1).
            await asyncio.to_thread(self.pseudo.save, row.id, mapping)
        updated = await self.store.save(row.id, revision, state, clear_steps=steps)
        if updated is None:
            raise InterviewConflict(_STALE)
        return self.view(updated)

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
                self._require_ready()
        answer = await self._pseudonymize(text, mapping)
        if not answer:
            raise InterviewInvalid("빈 답변은 저장할 수 없습니다")
        return answer

    async def _incident_turn(
        self, state: InterviewState, mapping: RunMapping, *, on_delta: TextCallback | None = None
    ) -> None:
        """대화 전체를 모델에 보내 다음 질문 또는 사례 초안을 받는다. 근거 인용이 담당자 말에 없으면 그 칸을 다시 묻는다."""
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
        conversation = [{"role": m.role, "text": m.text} for m in state.incident_messages]
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
                draft = attach_interview_cases(draft, state)
        except _LLM_ERRORS as exc:
            logger.warning("interview_incident_rejected type=%s", type(exc).__name__)
            raise InterviewUnavailable(
                "사례를 정리하지 못했습니다. 마지막 저장 상태에서 답변을 보완해 다시 시도해 주세요"
            ) from None
        only_trap_missing = set(missing) <= {"naive_fix", "why_naive_fails"}
        needs_trap = state.trap_answers is None and (draft is not None or only_trap_missing)
        if needs_trap:
            question = ""
        elif draft is None and only_trap_missing:
            raise InterviewUnavailable(
                "처음 시도한 대응을 사례에 반영하지 못했습니다. 다시 시도해 주세요"
            )
        state.reset_answers()
        state.incident_draft = draft
        state.incident_source = "known_problem"
        state.incident_evidence = evidence
        state.incident_missing = missing
        state.stage = (
            "trap_interview"
            if needs_trap
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
            mapping = self._mapping(interview_id)
            raw = {
                "attempted_fix": attempted_fix,
                "observed_result": observed_result,
                "why_failed": why_failed,
            }
            answers = {k: await self._pseudonymize(v, mapping) for k, v in raw.items()}
            if not unknown and not all(answers.values()):
                raise InterviewInvalid("처음 시도한 대응·관찰 결과·실패 이유를 모두 입력해 주세요")
            state.trap_answers = TrapAnswers(**answers, unknown=unknown)
            state.reset_answers()
            if state.incident_draft is not None:
                incident = state.incident_draft
                # '모름' 비고는 한 번만 두고, 실제 시도로 바꾸면 지운다 (code review W5)
                notes = "\n".join(
                    line for line in incident.notes.split("\n") if line != NOTE_TRAP_UNKNOWN
                ).strip()
                if unknown:
                    incident = incident.model_copy(
                        update={"notes": f"{notes}\n{NOTE_TRAP_UNKNOWN}".strip()}
                    )
                else:
                    incident = incident.model_copy(
                        update={
                            "naive_fix": answers["attempted_fix"],
                            "why_naive_fails": answers["why_failed"],
                            "notes": notes,
                        }
                    )
                state.incident_draft = attach_interview_cases(incident, state)
                state.stage = "success_interview"
            else:
                await self._incident_turn(state, mapping)
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

    # ================================================================ 앞 단계로·임시 저장·폐기
    async def revise(
        self,
        interview_id: str,
        *,
        revision: int,
        target: RevisionTarget,
        note: str,
        text: str | None = None,
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
            )
            mapping = self._mapping(interview_id)
            state.review_note = await self._pseudonymize(note, mapping)
            state.unconfirm()
            if target == "success":
                state.answer_validation = None
                state.stage = "success_interview"
            elif target == "trap":
                state.answer_validation = None
                state.stage = "trap_interview"
            else:
                state.stage = "incident_review" if state.incident_draft else "incident_interview"
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
            if row.state.stage in CLOSED_STAGES:
                return self.view(row)
            view = await self._close(row, "recruiter")
        return view

    async def _close(
        self, row: InterviewRow, reason: Literal["recruiter", "stale"]
    ) -> dict[str, Any]:
        state = row.state.model_copy(deep=True)
        state.stage = "discarded"
        state.discarded_at = self.now().isoformat()
        state.discard_reason = reason
        updated = await self.store.save(row.id, row.revision, state, clear_all_drafts=True)
        if updated is None:
            raise InterviewConflict(_STALE)
        await asyncio.to_thread(self.pseudo.delete, row.id)
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
