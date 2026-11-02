"""실행 생성·조회·승인·SSE. 그래프만 호출한다 (노드 직접 호출 금지)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langgraph.types import Command

from task_spec.api.deps import GraphDep, LauncherDep, OntologyDep, RunViewDep, SettingsDep
from task_spec.api.errors import (
    ApprovalNotPending,
    ErrorResponse,
    PostingNotFound,
    RunNotFound,
    SpecNotFound,
)
from task_spec.api.run_view import load_run_view
from task_spec.api.schemas import TERMINAL_STATUSES, RunAccepted, RunEvent, RunView
from task_spec.api.uploads import store_uploads
from task_spec.ontology.queries import posting_exists
from task_spec.schemas import ApprovalDecision, DocumentKind, RunInput, TaskSpec

router = APIRouter(prefix="/runs", tags=["runs"])

_ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}}


@router.post("", status_code=202, responses={413: {"model": ErrorResponse}})
async def create_run(
    settings: SettingsDep,
    ontology: OntologyDep,
    launcher: LauncherDep,
    posting_id: Annotated[str, Form(min_length=1, max_length=200)],
    # 문서 종류는 필드 이름으로 구분한다: resumes / cover_letters (각 0개 이상).
    resumes: Annotated[list[UploadFile] | None, File()] = None,
    cover_letters: Annotated[list[UploadFile] | None, File()] = None,
) -> RunAccepted:
    if ontology is not None and not await posting_exists(ontology, posting_id):
        raise PostingNotFound(posting_id)
    run_id = uuid.uuid4().hex
    uploads: list[tuple[DocumentKind, UploadFile]] = [
        *(("resume", f) for f in resumes or []),
        *(("cover_letter", f) for f in cover_letters or []),
    ]
    documents = await store_uploads(
        run_id=run_id,
        data_dir=settings.data_dir,
        max_bytes=settings.upload_max_bytes,
        uploads=uploads,
    )
    graph_input = RunInput(
        run_id=run_id,
        # 실제로 쓰는 온톨로지 버전 (dev 별칭이 아니라 적재 버전 문자열 — 불변식 4)
        ontology_version=ontology.version.version if ontology else settings.ontology_version,
        posting_id=posting_id,
        documents=documents,
    )
    launcher.launch(run_id, graph_input.model_dump(mode="json"))
    return RunAccepted(run_id=run_id, status="running")


@router.get("/{run_id}", responses=_ERRORS)
async def get_run(view: RunViewDep) -> RunView:
    return view


@router.get("/{run_id}/events", response_class=EventSourceResponse, responses=_ERRORS)
async def run_events(
    view: RunViewDep,  # 없는 run 은 스트림 시작 전에 404
    graph: GraphDep,
    launcher: LauncherDep,
    settings: SettingsDep,
) -> AsyncIterable[ServerSentEvent]:
    """상태(또는 현재 노드·리비전)가 바뀔 때마다 이벤트. 종결 상태면 마지막 이벤트 후 끝."""
    last: tuple[object, ...] | None = None
    seq = 0
    current: RunView | None = view
    while current is not None:
        key = (current.status, current.current_node, current.revision)
        if key != last:
            last = key
            seq += 1
            event = RunEvent(
                run_id=current.run_id,
                status=current.status,
                current_node=current.current_node,
                revision=current.revision,
            )
            yield ServerSentEvent(data=event.model_dump(), event="status", id=str(seq))
            if current.status in TERMINAL_STATUSES:
                return
        await asyncio.sleep(settings.sse_poll_seconds)
        current = await load_run_view(graph, launcher, view.run_id)


@router.post("/{run_id}/approve", status_code=202, responses=_ERRORS)
async def approve_run(
    run_id: str,
    decision: ApprovalDecision,  # TODO(auth): reviewer 는 인증 전까지 요청 본문에서 받는다
    view: RunViewDep,
    launcher: LauncherDep,
) -> RunAccepted:
    if view.status != "awaiting_approval":
        raise ApprovalNotPending(f"승인 대기 상태가 아닙니다 (현재: {view.status})", run_id=run_id)
    launcher.launch(run_id, Command(resume=decision.model_dump(mode="json")))
    return RunAccepted(run_id=run_id, status="running")


@router.get("/{run_id}/spec", responses=_ERRORS)
async def get_spec(run_id: str, view: RunViewDep, graph: GraphDep) -> TaskSpec:
    """승인 확정본만. 초안·반려·실패는 404."""
    if view.status != "done":
        raise SpecNotFound(run_id)
    snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
    spec: TaskSpec | None = (snapshot.values or {}).get("spec")
    if spec is None or spec.approval is None:
        raise RunNotFound(run_id)
    return spec
