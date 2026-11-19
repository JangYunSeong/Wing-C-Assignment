"""채용담당자 사례 인터뷰 API (#262 D1). 업무는 interview/service.py — 이 라우터는 요청 모델과 응답만.

요청 모델은 Wing-C-Interview 포크(9ef2f50)의 D1 부분과 같다 (화면 호환). 기존 공고에서만 시작한다 —
역량·과제(D2a)까지. 승인 그래프는 D2b, 채용 방향 문답·공고 초안(포크 ①)은 D3. 인증이 없으니 회사 범위 확인은 앞단(Wing 서버)이 한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel, Field, StrictBool, StrictStr, model_validator

from task_spec.api.deps import InterviewApprovalDep, InterviewsDep
from task_spec.graph.interview_approval import thread_id as approval_thread
from task_spec.interview.errors import InterviewConflict, InterviewError, InterviewInvalid
from task_spec.postings.company import company_id as company_key
from task_spec.schemas import InterviewApprovalState, PublicIncident, TaskItem
from task_spec.schemas.interview_approval import COMMON_ID
from task_spec.schemas.interview_state import (
    ANSWER_MAX,
    GeneralQuestion,
    IncidentSource,
    Stage,
    TargetedQuestion,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/interviews", tags=["recruiter-interviews"])


class StartRequest(BaseModel):
    posting_id: str = Field(min_length=1, max_length=500)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    revision: int = Field(ge=1)
    message_id: UUID | None = None


class ProjectRequest(BaseModel):
    text: str = Field(max_length=20000)
    revision: int = Field(ge=1)


class TrapRequest(BaseModel):
    revision: int = Field(ge=1)
    attempted_fix: str = Field(default="", max_length=2000)
    observed_result: str = Field(default="", max_length=2000)
    why_failed: str = Field(default="", max_length=2000)
    unknown: bool = False


class SuccessRequest(BaseModel):
    revision: int = Field(ge=1)
    reproduction: str = Field(min_length=1, max_length=2000)
    expected_result: str = Field(min_length=1, max_length=2000)
    invariants: str = Field(min_length=1, max_length=2000)
    verification: str = Field(min_length=1, max_length=2000)
    alternatives: str = Field(default="", max_length=2000)
    clarification_answers: dict[str, Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=dict, max_length=30
    )


class IncidentValidateRequest(BaseModel):
    incident: PublicIncident
    revision: int = Field(ge=1)


class IncidentConfirmRequest(IncidentValidateRequest):
    reviewer: str | None = Field(default=None, min_length=1, max_length=200)


class RevisionRequest(BaseModel):
    target: Literal["project", "incident", "trap", "success", "common", "questions"]
    revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)
    text: str | None = Field(default=None, max_length=20000)
    task: TaskItem | None = None  # target=common — 고친 공통 과제
    general_questions: list[GeneralQuestion] | None = Field(default=None, max_length=10)
    targeted_questions: list[TargetedQuestion] | None = Field(default=None, max_length=10)


class SelectionRequest(BaseModel):
    competency_ids: list[str] = Field(min_length=2, max_length=3)
    revision: int = Field(ge=1)


class AnswersRequest(BaseModel):
    # 짚은 질문 id → 답 (원문 — 서비스가 가명화한 뒤 다시 길이를 본다)
    answers: dict[
        Annotated[str, Field(pattern=r"^q\d{1,2}$")],
        Annotated[str, Field(min_length=1, max_length=ANSWER_MAX)],
    ] = Field(min_length=1, max_length=20)
    revision: int = Field(ge=1)


class TargetedAnswerDraftRequest(BaseModel):
    revision: int = Field(ge=1)
    question: str = Field(
        min_length=1, max_length=ANSWER_MAX
    )  # 지원자가 물을 법한 질문 (담당자 예시)


DraftText = Annotated[StrictStr, Field(max_length=40000)]
DraftValue = DraftText | StrictBool | list[Annotated[StrictStr, Field(max_length=1000)]]
DraftKey = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_:.-]{0,199}$")]


class StepDraft(BaseModel):
    values: dict[DraftKey, DraftValue] = Field(max_length=500)
    applied: dict[DraftKey, DraftValue] = Field(default_factory=dict, max_length=500)


class DraftRequest(BaseModel):
    draft_revision: int = Field(ge=0)
    steps: dict[Annotated[str, Field(pattern=r"^[1-9]$")], StepDraft] = Field(max_length=9)

    @model_validator(mode="after")
    def bounded_payload(self) -> DraftRequest:
        if len(self.model_dump_json().encode()) > 400_000:
            raise ValueError("임시 저장할 내용이 너무 깁니다")
        return self


def _message_id(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


class InterviewApprovalSummary(BaseModel):
    reviewer: str
    at: str


class InterviewSummaryView(BaseModel):
    """인터뷰 목록 한 줄 — 공고 메타데이터·단계·시각만 (대화·사례·답안 없음)."""

    interview_id: str
    company: str
    company_id: str | None
    posting_id: str
    title: str
    stage: Stage
    revision: int
    created_at: datetime
    updated_at: datetime
    approval: InterviewApprovalSummary | None = None
    discarded_at: str | None = None
    discard_reason: Literal["recruiter", "stale"] | None = None


@router.get("")
async def list_interviews(
    interviews: InterviewsDep,
    # 필수 — 인증 전에는 회사 없이 전부 보지 않는다 (목록의 id 로 상세를 열 수 있다, recruiter-interview.md 한계) [확정: 2026-11-20 사용자]
    company: Annotated[str, Query(min_length=1, max_length=200)],
    stage: Stage | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[InterviewSummaryView]:
    """최근 진행 순. 승인·폐기된 인터뷰도 넣고, 방치 30일이 지난 열린 인터뷰는 discarded(사유 stale)로 보인다."""
    rows = await interviews.summaries(company=company, stage=stage, limit=limit)
    return [
        InterviewSummaryView(
            interview_id=r.id,
            company=r.company,
            company_id=company_key(r.company),
            posting_id=r.posting_id,
            title=r.title,
            stage=r.stage,
            revision=r.revision,
            created_at=r.created_at,
            updated_at=r.updated_at,
            approval=(
                InterviewApprovalSummary(reviewer=r.reviewer, at=r.approved_at)
                if r.reviewer is not None and r.approved_at is not None
                else None
            ),
            discarded_at=r.discarded_at
            or ((r.updated_at + interviews.mapping_ttl).isoformat() if r.stale else None),
            discard_reason="stale" if r.stale else r.discard_reason,
        )
        for r in rows
    ]


@router.post("", status_code=201)
async def start(body: StartRequest, interviews: InterviewsDep) -> dict[str, Any]:
    return await interviews.create(body.posting_id)


@router.get("/{interview_id}")
async def get_interview(interview_id: str, interviews: InterviewsDep) -> dict[str, Any]:
    return await interviews.get(interview_id)


@router.post("/{interview_id}/messages")
async def message(
    interview_id: str, body: MessageRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.message(
        interview_id,
        text=body.text,
        revision=body.revision,
        message_id=_message_id(body.message_id),
    )


async def _stream_input(interview_id: str, body: MessageRequest, interviews: InterviewsDep) -> str:
    return await interviews.precheck_message(
        interview_id,
        text=body.text,
        revision=body.revision,
        message_id=_message_id(body.message_id),
    )


@router.post("/{interview_id}/messages/stream", response_class=EventSourceResponse)
async def stream_message(
    interview_id: str,
    body: MessageRequest,
    interviews: InterviewsDep,
    accepted: Annotated[str, Depends(_stream_input)],
) -> AsyncIterator[ServerSentEvent]:
    """보낸 답을 바로 보여주고(accepted, 가명화한 글), 모델이 쓰는 질문 조각을 assistant 로 갱신한 뒤 저장 결과를 done 으로."""
    yield ServerSentEvent(event="accepted", data={"text": accepted})
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue(maxsize=32)

    async def emit(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    async def process() -> None:
        try:
            result = await interviews.message(
                interview_id,
                text=body.text,
                revision=body.revision,
                message_id=_message_id(body.message_id),
                emit=emit,
            )
            key = "scope_messages" if result["stage"] == "scope_interview" else "incident_messages"
            messages = result.get(key) or []
            if messages and messages[-1]["role"] == "assistant":
                await emit("assistant", {"text": messages[-1]["text"]})
            await emit("done", result)
        except InterviewError as exc:
            await emit("error", {"code": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001 — 스트림 안 오류는 이벤트로 알린다
            logger.error("interview stream failed type=%s", type(exc).__name__)
            await emit(
                "error",
                {
                    "code": "service_unavailable",
                    "message": "답변을 받지 못했습니다. 입력 내용을 확인하고 다시 보내 주세요.",
                },
            )
        finally:
            current = asyncio.current_task()
            if current is not None and not current.cancelling():
                await queue.put(None)

    task = asyncio.create_task(process())
    try:
        while (item := await queue.get()) is not None:
            yield ServerSentEvent(event=item[0], data=item[1])
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@router.post("/{interview_id}/project")
async def set_project(
    interview_id: str, body: ProjectRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.project(interview_id, text=body.text, revision=body.revision)


@router.post("/{interview_id}/trap")
async def record_trap(
    interview_id: str, body: TrapRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.trap(
        interview_id,
        revision=body.revision,
        attempted_fix=body.attempted_fix,
        observed_result=body.observed_result,
        why_failed=body.why_failed,
        unknown=body.unknown,
    )


@router.post("/{interview_id}/success")
async def record_success(
    interview_id: str, body: SuccessRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.success(
        interview_id,
        revision=body.revision,
        reproduction=body.reproduction,
        expected_result=body.expected_result,
        invariants=body.invariants,
        verification=body.verification,
        alternatives=body.alternatives,
        clarification_answers=body.clarification_answers,
    )


@router.post("/{interview_id}/incident/validate")
async def validate_answer(
    interview_id: str, body: IncidentValidateRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.validate(interview_id, revision=body.revision, incident=body.incident)


@router.post("/{interview_id}/incident/confirm")
async def confirm_incident(
    interview_id: str, body: IncidentConfirmRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.confirm(
        interview_id, revision=body.revision, incident=body.incident, reviewer=body.reviewer
    )


class IncidentSourceRequest(BaseModel):
    revision: int = Field(ge=1)
    source: (
        IncidentSource  # known_problem(인터뷰 사례) · public_fixed(공개 사례) · company(회사 사례)
    )
    # 공개 사례는 없으면 직군 기본 사례(포크와 같음), 회사 사례는 필수
    incident_id: str | None = Field(default=None, pattern=r"^(?:pub|co):[a-z_.]+:[a-z0-9-]+$")


@router.get("/{interview_id}/incident/sources")
async def incident_sources(interview_id: str, interviews: InterviewsDep) -> dict[str, Any]:
    """고를 수 있는 사례 — 직군의 공개 사례와 같은 회사의 공유 승인된 사례 (D2c)."""
    return await interviews.incident_options(interview_id)


@router.post("/{interview_id}/incident/source")
async def select_incident_source(
    interview_id: str, body: IncidentSourceRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    """사례 출처 고르기 (포크와 같은 경로). 공개·회사 사례는 확인 단계에 그대로 올라가고 고치지 않는다."""
    return await interviews.select_incident_source(
        interview_id, revision=body.revision, source=body.source, incident_id=body.incident_id
    )


@router.post("/{interview_id}/revise")
async def revise(
    interview_id: str, body: RevisionRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.revise(
        interview_id,
        revision=body.revision,
        target=body.target,
        note=body.note,
        text=body.text,
        task=body.task,
        general_questions=body.general_questions,
        targeted_questions=body.targeted_questions,
    )


@router.get("/{interview_id}/competencies")
async def competency_preview(interview_id: str, interviews: InterviewsDep) -> dict[str, Any]:
    """고를 수 있는 공고 역량(수행준거 근거 있음)·권하는 역량·직군 기본 코딩 템플릿 (#266 D2a)."""
    return await interviews.competency_preview(interview_id)


@router.post("/{interview_id}/competencies")
async def select_competencies(
    interview_id: str, body: SelectionRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.select_competencies(
        interview_id, revision=body.revision, competency_ids=body.competency_ids
    )


@router.post("/{interview_id}/generate")
async def generate(
    interview_id: str, body: SelectionRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    """확인된 사례로 공고 공통 과제(기능형)와 지원자 질문을 만든다. interview_feature_seed CURRENT 가 없으면 503."""
    return await interviews.generate(
        interview_id, revision=body.revision, competency_ids=body.competency_ids
    )


@router.post("/{interview_id}/answers")
async def set_answers(
    interview_id: str, body: AnswersRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.set_answers(interview_id, revision=body.revision, answers=body.answers)


@router.post("/{interview_id}/answers/{question_id}/draft")
async def draft_targeted_answer(
    interview_id: str, question_id: str, body: TargetedAnswerDraftRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    """짚은 질문에 줄 답의 초안 (저장하지 않음). interview_targeted_answer CURRENT 가 없으면 503."""
    return await interviews.draft_targeted_answer(
        interview_id, question_id, revision=body.revision, question=body.question
    )


@router.patch("/{interview_id}/drafts")
async def save_step_drafts(
    interview_id: str, body: DraftRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.save_drafts(
        interview_id,
        draft_revision=body.draft_revision,
        steps={step: draft.model_dump() for step, draft in body.steps.items()},
    )


@router.post("/{interview_id}/discard")
async def discard(interview_id: str, interviews: InterviewsDep) -> dict[str, Any]:
    """폐기 — 이름 대응표·원문 임시 저장을 지운다. 되돌릴 수 없다 (새 인터뷰를 시작한다)."""
    return await interviews.discard(interview_id)


# ================================================================ ⑨ 승인 (#266 D2b) — 그래프 thread 를 시작하고 resume 만 한다 (불변식 9)
class InterviewApproveRequest(BaseModel):
    revision: int = Field(ge=1)  # 화면이 본 인터뷰 판본 — 다르면 409
    status: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=200)  # TODO(auth): 인증 전까지 요청 본문에서
    at: datetime | None = None  # 없으면 서버 시각
    comment: str | None = Field(default=None, max_length=1000)  # 반려면 필수 — resume 전에 가명화
    replaces: str | None = Field(default=None, pattern=COMMON_ID)  # 화면이 보여 준 지금 인터뷰 과제


class InterviewBuildRequest(BaseModel):
    revision: int = Field(ge=1)
    requested_by: str | None = Field(default=None, min_length=1, max_length=200)  # 없으면 승인자


def _pending(snap: Any) -> dict[str, Any] | None:
    for task in snap.tasks:
        for itr in task.interrupts:
            if isinstance(itr.value, dict):
                return itr.value
    return None


def _closed(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "interview_id": view["interview_id"],
        "revision": view["revision"],
        "state": "closed",
        "stage": view["stage"],
        "approval": view.get("approval"),
    }


async def _waiting(
    graph: Any, interview_id: str, revision: int, *, start: bool
) -> dict[str, Any] | None:
    """그 판본의 승인 thread — 없으면(start) 시작해 interrupt 까지. 기다리는 중이 아니면 None."""
    cfg: RunnableConfig = {"configurable": {"thread_id": approval_thread(interview_id, revision)}}
    snap = await graph.aget_state(cfg)
    if not snap.values and start:
        state = InterviewApprovalState(interview_id=interview_id, revision=revision)
        await graph.ainvoke(state.model_dump(), cfg)
        snap = await graph.aget_state(cfg)
    return _pending(snap)


@router.get("/{interview_id}/approval")
async def get_approval(
    interview_id: str, interviews: InterviewsDep, graph: InterviewApprovalDep
) -> dict[str, Any]:
    """승인 대기 내용 — 교체 대상(지금 승인된 인터뷰 과제)·승인할 수 없는 사유. 승인한 인터뷰는 닫힌 상태만."""
    view = await interviews.get(interview_id)
    if view["stage"] == "approved":
        return _closed(view)
    if view["stage"] != "task_review":
        raise InterviewConflict("과제 검토 단계에서만 승인할 수 있습니다")
    payload = await _waiting(graph, interview_id, view["revision"], start=True)
    if payload is None:
        raise InterviewConflict("인터뷰 판본이 바뀌었습니다. 새로고침 후 다시 시도해 주세요")
    return {**payload, "state": "awaiting"}


@router.post("/{interview_id}/approve")
async def approve(
    interview_id: str,
    body: InterviewApproveRequest,
    interviews: InterviewsDep,
    graph: InterviewApprovalDep,
) -> dict[str, Any]:
    """승인·반려 — 그래프 resume 만 한다. 승인되면 닫힌 상태, 반려되면 인터뷰(과제 검토 단계·수정 요청),
    그래프가 다시 기다리면(교체 미명시·승인할 수 없음·형식 오류) 422 + 사유."""
    view = await interviews.get(interview_id)
    if view["stage"] == "approved":
        raise InterviewConflict("이미 승인한 인터뷰입니다")
    if view["revision"] != body.revision or view["stage"] != "task_review":
        raise InterviewConflict("인터뷰 판본이 바뀌었습니다. 새로고침 후 다시 시도해 주세요")
    if await _waiting(graph, interview_id, body.revision, start=True) is None:
        raise InterviewConflict("인터뷰 판본이 바뀌었습니다. 새로고침 후 다시 시도해 주세요")
    comment = (
        await interviews.pseudonymize_note(interview_id, body.comment)
        if body.comment and body.comment.strip()
        else None
    )
    decision = {
        "status": body.status,
        "reviewer": body.reviewer,
        "at": (body.at or datetime.now(UTC)).isoformat(),
        "comment": comment,
        "replaces": body.replaces,
    }
    cfg: RunnableConfig = {
        "configurable": {"thread_id": approval_thread(interview_id, body.revision)}
    }
    await graph.ainvoke(Command(resume=decision), cfg)
    # 결과는 인터뷰 행으로 판정한다 — 같은 thread 에 결정이 동시에 들어오면(두 번 누름·두 화면) 확정은 잠금·판본으로 한 번뿐이지만
    # 체크포인트는 마지막에 쓴 쪽이 이겨, 승인한 요청이 stale 을 읽을 수 있다 (code review W2)
    after = await interviews.get(interview_id)
    if after["stage"] == "approved":
        if body.status == "approved":
            return _closed(after)
        raise InterviewConflict("그 사이 승인된 인터뷰입니다")
    if (
        body.status == "rejected"
        and after["revision"] > body.revision
        and after.get("review_note") == comment
    ):
        return after
    snap = await graph.aget_state(cfg)
    waiting = _pending(snap)
    if waiting is not None:
        raise InterviewInvalid(
            waiting.get("error") or "결정이 적용되지 않았습니다 — 다시 불러오세요"
        )
    raise InterviewConflict("인터뷰 판본이 바뀌었습니다. 새로고침 후 다시 시도해 주세요")


@router.post("/{interview_id}/builds", status_code=202)
async def request_build(
    interview_id: str, body: InterviewBuildRequest, interviews: InterviewsDep, response: Response
) -> dict[str, Any]:
    """승인한 과제의 시작 코드 빌드 요청 (포크와 같음) — 이미 만든 빌드나 진행 중 빌드가 있으면 그것(200)."""
    out = await interviews.interview_build(
        interview_id, revision=body.revision, requested_by=body.requested_by
    )
    if not out["created"]:
        response.status_code = 200
    return out
