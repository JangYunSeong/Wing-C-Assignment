"""회사 결정적 사건 — 등록·조회·공유 승인·보관 [확정: 2026-11-19 사용자] (spec/company_incidents.py).

- 등록(pending)한 사례는 공유 승인을 받아야(shared) 같은 회사 공고의 공통 과제 생성에 쓰인다. 다른 회사에는 쓰지 않는다.
- 공유 승인은 interrupt 그래프로만 (graph/incident_share.py, 불변식 9) — 여기서는 thread 를 시작하고 resume 만 한다.
- 연락처·링크로 보이는 표현이 있으면 등록하지 않는다 (가명화와 같은 로컬 탐지기). 응답·오류에는 탐지한 값을 넣지 않는다.
- 공개 사례(incidents/public_v1)는 여기서 다루지 않는다 — 파일로만 둔다 [2026-11-19 사용자].
- TODO(auth): 회사별 열람 제한은 인증 뒤. 지금은 다른 API 처럼 인증이 없다 — 공고의 회사도 사람이 입력한 값을 그대로 믿는다
  [확정: 2026-11-19 사용자] (postings/company.py). 인증 때 입력자·열람자를 회사와 묶는다.
- 지금은 손으로 등록한다(source=manual). 채용담당자 인터뷰를 들여오면 인터뷰 승인이 사례를 등록한다 (docs/design/recruiter-interview.md).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import AwareDatetime, BaseModel, Field

from task_spec.api.deps import CommonStoreDep, CompanyIncidentsDep, IncidentShareDep
from task_spec.api.errors import (
    ErrorResponse,
    IncidentContentBlocked,
    IncidentInvalid,
    IncidentNotFound,
    IncidentNotPending,
    IncidentShareInvalid,
)
from task_spec.graph.incident_share import thread_id
from task_spec.postings.company import company_id
from task_spec.schemas import IncidentShareDecision, IncidentShareState, PublicIncident
from task_spec.schemas.base import Schema
from task_spec.spec.company_incidents import (
    CompanyIncident,
    CompanyIncidentRepo,
    CompanyIncidentStatus,
    new_incident_id,
    scan_incident,
)
from task_spec.spec.store import CommonTaskRepo

router = APIRouter(prefix="/incidents", tags=["incidents"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
Text = Annotated[str, Field(min_length=1, max_length=4000)]
Item = Annotated[str, Field(min_length=1, max_length=500)]


class IncidentSubmit(Schema):
    """회사 사례 등록 — 공개 사례와 같은 칸 (incidents/public_v1/README.md 작성 규칙). 출처 칸(조직·URL)은 없다."""

    company: str = Field(
        min_length=1, max_length=200
    )  # 회사 표기 — 공고와 같은 규칙으로 id 로 바꾼다
    sub: str = Field(pattern=r"^[a-z_.]+$", max_length=60)  # 직군 id (예: sw.backend)
    title: str = Field(min_length=1, max_length=200)
    symptom: Text
    naive_fix: Text
    root_cause: Text
    why_naive_fails: Text
    key_insight: Text
    deep_structure: Text
    surface: list[Item] = Field(default_factory=list, max_length=20)
    tech: list[Item] = Field(default_factory=list, max_length=20)
    hypotheses: list[Item] = Field(default_factory=list, max_length=20)
    avoid_terms: list[Item] = Field(default_factory=list, max_length=40)
    hidden_terms: list[Item] = Field(default_factory=list, max_length=40)
    accepted_fixes: list[Item] = Field(default_factory=list, max_length=20)
    source_literals: list[Item] = Field(default_factory=list, max_length=40)
    submitted_by: str = Field(min_length=1, max_length=200)  # TODO(auth): 인증 전까지 요청 본문에서


class IncidentRetire(Schema):
    reviewer: str = Field(min_length=1, max_length=200)  # TODO(auth)
    reason: str = Field(min_length=1, max_length=1000)
    at: AwareDatetime


class IncidentView(BaseModel):
    incident_id: str
    company_id: str
    sub: str
    title: str
    status: CompanyIncidentStatus
    source: str  # manual · interview
    created_by: str
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_comment: str | None = None
    retired_by: str | None = None
    retired_at: datetime | None = None
    retire_reason: str | None = None
    used_by: int = 0  # 이 사례로 공통 과제가 승인된 공고 수


class IncidentDetail(IncidentView):
    incident: PublicIncident


class IncidentShareView(BaseModel):
    incident_id: str
    status: CompanyIncidentStatus
    state: Literal["awaiting", "closed"]
    flags: list[str] = Field(default_factory=list)  # 칸별 탐지 개수 (값 없음)
    error: str | None = None


def _view(rec: CompanyIncident, used: dict[str, int]) -> dict[str, Any]:
    return {
        "incident_id": rec.incident_id,
        "company_id": rec.company_id,
        "sub": rec.sub,
        "title": rec.incident.title,
        "status": rec.status,
        "source": str(rec.source.get("kind", "manual")),
        "created_by": rec.created_by,
        "created_at": rec.created_at,
        "decided_by": rec.decided_by,
        "decided_at": rec.decided_at,
        "decision_comment": rec.decision_comment,
        "retired_by": rec.retired_by,
        "retired_at": rec.retired_at,
        "retire_reason": rec.retire_reason,
        "used_by": used.get(rec.incident_id, 0),
    }


async def _usage(common: CommonTaskRepo | None, ids: list[str]) -> dict[str, int]:
    if common is None or not ids:
        return {}
    return await common.incident_usage(ids, exclude_posting="")


async def _get(store: CompanyIncidentRepo, incident_id: str) -> CompanyIncident:
    rec = await store.get(incident_id)
    if rec is None:
        raise IncidentNotFound(incident_id)
    return rec


def _company(name: str) -> str:
    cid = company_id(name)
    if cid is None:
        raise IncidentInvalid("회사 표기에서 회사를 알 수 없습니다 (법인 표기·공백만 있음)")
    return cid


@router.post("", status_code=201, responses=_ERRORS)
async def submit_incident(
    body: IncidentSubmit, request: Request, store: CompanyIncidentsDep
) -> IncidentDetail:
    """회사 사례 등록 (pending). 공유 승인 전에는 과제 생성에 쓰지 않는다."""
    cid = _company(body.company)
    ctx = getattr(request.app.state, "postings", None)
    subs = getattr(getattr(ctx, "lists", None), "subs", None)
    if subs and body.sub not in subs:
        raise IncidentInvalid(f"온톨로지에 없는 직군입니다: {body.sub}")
    incident = PublicIncident(
        incident_id=new_incident_id(body.sub),
        sub=body.sub,
        title=body.title,
        org="",  # 회사는 company_id 칸에만 — 내용(LLM 입력 후보)에는 두지 않는다
        url="",
        published=datetime.now(UTC).date().isoformat(),
        source_lang="ko",
        symptom=body.symptom,
        naive_fix=body.naive_fix,
        root_cause=body.root_cause,
        why_naive_fails=body.why_naive_fails,
        key_insight=body.key_insight,
        deep_structure=body.deep_structure,
        surface=body.surface,
        tech=body.tech,
        verified=False,
        hypotheses=body.hypotheses,
        avoid_terms=body.avoid_terms,
        hidden_terms=body.hidden_terms,
        accepted_fixes=body.accepted_fixes,
        source_literals=body.source_literals,
    )
    found = await asyncio.to_thread(scan_incident, incident)
    blocked = [f.text() for f in found if f.blocks]
    if blocked:
        raise IncidentContentBlocked(blocked)
    rec = await store.submit(cid, incident, source={"kind": "manual"}, created_by=body.submitted_by)
    return IncidentDetail(**_view(rec, {}), incident=rec.incident)


@router.get("", responses=_ERRORS)
async def list_incidents(
    store: CompanyIncidentsDep,
    common: CommonStoreDep,
    company: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    status: CompanyIncidentStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[IncidentView]:
    """최근 등록 순. `company` 는 회사 id 또는 표기 (같은 규칙으로 맞춘다)."""
    rows = await store.find(
        company_id=_company(company) if company else None, status=status, limit=limit
    )
    used = await _usage(common, [r.incident_id for r in rows])
    return [IncidentView(**_view(r, used)) for r in rows]


@router.get("/{incident_id}", responses=_ERRORS)
async def get_incident(
    incident_id: str, store: CompanyIncidentsDep, common: CommonStoreDep
) -> IncidentDetail:
    rec = await _get(store, incident_id)
    used = await _usage(common, [rec.incident_id])
    return IncidentDetail(**_view(rec, used), incident=rec.incident)


def _pending(snap: Any) -> dict[str, Any] | None:
    for task in snap.tasks:
        for itr in task.interrupts:
            if isinstance(itr.value, dict):
                return itr.value
    return None


async def _share_view(graph: Any, rec: CompanyIncident, start: bool) -> IncidentShareView | None:
    """pending 사례의 공유 승인 thread — 없으면(start) 시작해 interrupt 까지. 기다리는 중이 아니면 None."""
    cfg: RunnableConfig = {"configurable": {"thread_id": thread_id(rec)}}
    snap = await graph.aget_state(cfg)
    if not snap.values and start:
        state = IncidentShareState(incident_id=rec.incident_id, content_sha256=rec.content_sha256)
        await graph.ainvoke(state.model_dump(), cfg)
        snap = await graph.aget_state(cfg)
    payload = _pending(snap)
    if payload is None:
        return None
    return IncidentShareView(
        incident_id=rec.incident_id,
        status=rec.status,
        state="awaiting",
        flags=[str(f) for f in payload.get("flags", [])],
        error=payload.get("error"),
    )


def _closed(rec: CompanyIncident) -> IncidentShareView:
    return IncidentShareView(incident_id=rec.incident_id, status=rec.status, state="closed")


@router.get("/{incident_id}/share", responses=_ERRORS)
async def get_share(
    incident_id: str, store: CompanyIncidentsDep, graph: IncidentShareDep
) -> IncidentShareView:
    """공유 승인 대기 내용 — pending 이 아니면 닫힌 상태만."""
    rec = await _get(store, incident_id)
    if rec.status != "pending":
        return _closed(rec)
    view = await _share_view(graph, rec, start=True)
    if view is None:
        raise IncidentNotPending("공유 승인 대기 중이 아닙니다 — 다시 불러오세요")
    return view


@router.post("/{incident_id}/share", responses=_ERRORS)
async def decide_share(
    incident_id: str,
    body: IncidentShareDecision,  # TODO(auth): reviewer 는 인증 전까지 요청 본문에서 받는다
    store: CompanyIncidentsDep,
    graph: IncidentShareDep,
) -> IncidentShareView:
    """그래프 resume 만 한다. 결정이 적용되면 닫힌 상태, 그래프가 다시 기다리면 422 + 사유."""
    rec = await _get(store, incident_id)
    if rec.status != "pending":
        raise IncidentNotPending(f"pending 상태에서만 공유 승인·반려 (지금 {rec.status})")
    if await _share_view(graph, rec, start=True) is None:
        raise IncidentNotPending("공유 승인 대기 중이 아닙니다 — 다시 불러오세요")
    cfg: RunnableConfig = {"configurable": {"thread_id": thread_id(rec)}}
    await graph.ainvoke(Command(resume=body.model_dump(mode="json")), cfg)
    after = await _get(store, incident_id)
    if after.status == "pending":
        waiting = await _share_view(graph, after, start=False)
        raise IncidentShareInvalid(
            (waiting.error if waiting is not None else None)
            or "결정이 적용되지 않았습니다 — 다시 불러오세요"
        )
    if after.status != ("shared" if body.status == "approved" else "rejected"):
        # 그 사이 보관됨 등 — 이 결정은 적용되지 않았다 (200 으로 돌려주면 승인된 줄 안다)
        raise IncidentNotPending(
            f"결정이 적용되지 않았습니다 — 그 사이 상태가 {after.status} 로 바뀜"
        )
    return _closed(after)


@router.post("/{incident_id}/retire", responses=_ERRORS)
async def retire_incident(
    incident_id: str, body: IncidentRetire, store: CompanyIncidentsDep, common: CommonStoreDep
) -> IncidentDetail:
    """보관 — 새 공고에 더는 고르지 않는다 (밖으로 샜거나 더 쓰지 않을 때). 이미 만든 과제는 그대로다."""
    await _get(store, incident_id)
    if not await store.retire(incident_id, body.reviewer, body.at, body.reason):
        raise IncidentNotPending("등록·공유 상태에서만 보관할 수 있습니다")
    rec = await _get(store, incident_id)
    used = await _usage(common, [rec.incident_id])
    return IncidentDetail(**_view(rec, used), incident=rec.incident)
