"""실행 생성·조회·승인·SSE. 그래프만 호출한다 (노드 직접 호출 금지)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langgraph.types import Command

from task_spec.api.deps import (
    CommonStoreDep,
    GraphDep,
    LauncherDep,
    OntologyDep,
    RunViewDep,
    SettingsDep,
)
from task_spec.api.errors import (
    ApprovalNotPending,
    ApprovalRevisionMismatch,
    CommonTaskGone,
    ErrorResponse,
    PostingNotFound,
    RunNotFound,
    SpecNotFound,
)
from task_spec.api.launcher import RunBusy
from task_spec.api.run_view import list_runs, load_run_view
from task_spec.api.schemas import (
    TERMINAL_STATUSES,
    RunAccepted,
    RunEvent,
    RunListItem,
    RunStatus,
    RunView,
)
from task_spec.api.uploads import store_uploads
from task_spec.ontology.queries import posting_exists
from task_spec.postings.career import PostingCareer, posting_careers
from task_spec.postings.company import PostingCompany, posting_companies
from task_spec.postings.store import is_provisional
from task_spec.schemas import ApprovalDecision, DocumentKind, RunInput, TaskSpec

router = APIRouter(prefix="/runs", tags=["runs"])

_ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}}


@router.get("")
async def get_runs(
    request: Request,
    graph: GraphDep,
    launcher: LauncherDep,
    ontology: OntologyDep,
    status: Annotated[list[RunStatus] | None, Query()] = None,
    company: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> list[RunListItem]:
    """최근에 시작한 실행부터, limit 이 없으면 전부. `status` 를 여러 번 주면 그 상태들만
    (예: ?status=running&status=awaiting_approval). `company` 는 회사 id 또는 표기 — 같은 규칙(공백·법인 표기 빼기)으로 맞춘다."""
    ctx = getattr(request.app.state, "postings", None)

    async def companies(ids: set[str]) -> dict[str, PostingCompany]:
        return await posting_companies(ids, ontology=ontology, store=ctx.store if ctx else None)

    async def careers(ids: set[str]) -> dict[str, PostingCareer]:
        return await posting_careers(ids, ontology=ontology, store=ctx.store if ctx else None)

    return await list_runs(
        graph,
        launcher,
        limit=limit,
        statuses=frozenset(status or ()),
        companies=companies,
        company=company,
        careers=careers,
    )


@router.post("", status_code=202, responses={413: {"model": ErrorResponse}})
async def create_run(
    request: Request,
    settings: SettingsDep,
    ontology: OntologyDep,
    launcher: LauncherDep,
    posting_id: Annotated[str, Form(min_length=1, max_length=200)],
    # 문서 종류는 필드 이름으로 구분한다: resumes / cover_letters (각 0개 이상).
    resumes: Annotated[list[UploadFile] | None, File()] = None,
    cover_letters: Annotated[list[UploadFile] | None, File()] = None,
) -> RunAccepted:
    if is_provisional(posting_id):
        ctx = getattr(request.app.state, "postings", None)
        if ctx is None or await ctx.store.get_provisional(posting_id) is None:
            raise PostingNotFound(posting_id)
    elif ontology is not None and not await posting_exists(ontology, posting_id):
        raise PostingNotFound(posting_id)
    run_id = uuid.uuid4().hex
    # 자기소개서를 쓰지 않으면 저장하지도 않는다 (개인정보 최소 처리) [확정: 2026-11-03]
    use_cl = settings.use_cover_letters
    ignored = [] if use_cl else [f"cover_letter#{i}" for i, _ in enumerate(cover_letters or [], 1)]
    uploads: list[tuple[DocumentKind, UploadFile]] = [
        *(("resume", f) for f in resumes or []),
        *(("cover_letter", f) for f in (cover_letters or []) if use_cl),
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
    return RunAccepted(run_id=run_id, status="running", ignored=ignored)


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
    graph: GraphDep,
    launcher: LauncherDep,
    common_store: CommonStoreDep,
    # 승인자가 보고 있는 판 — 지금 승인 대기 판과 다르면 409 (#247). 결정(ApprovalDecision)은 체크포인트에
    # 실리는 모델이라 칸을 더하지 않고 요청에만 둔다. 없으면 판을 확인하지 않는다 (예전 클라이언트)
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> RunAccepted:
    # 상태를 읽기 전에 — 읽은 뒤 다른 결정이 이 실행을 띄웠으면 launch 가 거부한다 (#247)
    seen = launcher.launches(run_id)
    view = await load_run_view(graph, launcher, run_id)
    if view is None:
        raise RunNotFound(run_id)
    if view.status != "awaiting_approval":
        raise ApprovalNotPending(f"승인 대기 상태가 아닙니다 (현재: {view.status})", run_id=run_id)
    if revision is not None and view.revision != revision:
        raise ApprovalRevisionMismatch(revision, view.revision, run_id=run_id)
    # 확정(finalize)은 명세가 가리키는 공통 과제 판을 승인한다 — 판이 없으면 실행이 실패하므로 승인 전에 막는다
    if decision.status == "approved" and common_store is not None and view.spec is not None:
        gone = await common_store.missing([r.common_id for r in view.spec.common_refs])
        if gone:
            raise CommonTaskGone(gone, run_id=run_id)
    try:
        launcher.launch(run_id, Command(resume=decision.model_dump(mode="json")), after=seen)
    except RunBusy:
        # 같은 실행에 결정이 겹쳤다 — 먼저 들어온 결정이 도는 중이거나 이미 끝나, 이 결정이 본 판은 지나갔다
        raise ApprovalNotPending(
            "승인 대기 상태가 아닙니다 — 다른 결정이 먼저 들어왔습니다. 실행 상태를 다시 확인하세요",
            run_id=run_id,
        ) from None
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
