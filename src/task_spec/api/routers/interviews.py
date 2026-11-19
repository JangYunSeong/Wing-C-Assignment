"""채용담당자 사례 인터뷰 API (#262 D1). 업무는 interview/service.py — 이 라우터는 요청 모델과 응답만.

요청 모델은 Wing-C-Interview 포크(9ef2f50)의 D1 부분과 같다 (화면 호환). 기존 공고에서만 시작한다 —
채용 방향 문답·공고 초안(포크 ①)과 역량·과제·승인은 D2·D3. 인증이 없으니 회사 범위 확인은 앞단(Wing 서버)이 한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field, StrictBool, StrictStr, model_validator

from task_spec.api.deps import InterviewsDep
from task_spec.interview.errors import InterviewError
from task_spec.schemas import PublicIncident

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
    target: Literal["project", "incident", "trap", "success"]
    revision: int = Field(ge=1)
    note: str = Field(min_length=1, max_length=1000)
    text: str | None = Field(default=None, max_length=20000)


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


@router.post("/{interview_id}/revise")
async def revise(
    interview_id: str, body: RevisionRequest, interviews: InterviewsDep
) -> dict[str, Any]:
    return await interviews.revise(
        interview_id, revision=body.revision, target=body.target, note=body.note, text=body.text
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
