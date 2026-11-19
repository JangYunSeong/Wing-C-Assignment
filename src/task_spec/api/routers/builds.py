"""공통 과제 빌드·전달 묶음 조회 API (이슈 #85) [제안] + 과제 생성 요청 (#128).

GET  /builds/{build_id}/approval  : 빌드(시작 코드) 승인 (#130) — review 빌드면 승인 thread 를 시작(interrupt 까지)하고 검토 표시·사유를,
                                   끝난 빌드면 결과(approved·rejected)를 준다. 시작은 승인이 아니다 (`POST /runs` 와 같은 자리)
POST /builds/{build_id}/approve   : 승인·반려 — 그래프 resume 만 (불변식 9). 판단이 빠진 표시·저장소 규칙 위반이면 그래프가 다시 기다리고
                                   422 + 사유. 묶음을 approved 로 다시 쓰고 평가 키트를 만드는 일은 builder [확정: 2026-11-12 사용자, ③ A]
POST /common-tasks/{common_id}/builds : 과제 생성 요청 — 명세 승인과 과제 생성은 따로다 [확정: 2026-11-12 사용자].
                                   큐에 넣기만 한다 (API 는 빌드하지 않음, builder 가 가져감). approved·기능형 공통 과제만(아니면 409),
                                   진행 중 빌드가 있으면 새로 만들지 않고 그 빌드를 돌려준다 (200, 새로 만들면 202).

GET  /builds[?common_id=]        : 공통 과제의 빌드 목록 (없으면 최근 빌드 — 화면 목록용, 제목·공고 id 함께)
GET  /builds/{build_id}          : 상태·검증 요약·검토 표시(+승인자 판단)·파일 수·생성기 호출 요약(프롬프트 판·모델별 호출 수·비용, #233)
GET  /builds/{build_id}/artifacts: 역할별 묶음 파일(이름·sha256·크기)·image_id
GET  /builds/{build_id}/deliveries: 전달 기록
POST /builds/{build_id}/deliveries {role, target, requested_by} : SFTP 전달 요청 (#116) — 승인·확정된 빌드만, 기록(requested)을
                                   만들고 202 로 바로 돌려준다. 보내기는 API 프로세스의 백그라운드(delivery/sender.py), 결과는 GET 으로 폴링
                                   (sent·failed + 사유 코드). 같은 대상으로 보내는 중이거나 보낸 역할은 다시 보내지 않는다 (409).
                                   접속 설정이 없는 대상이면 409 delivery_target_not_configured
GET  /builds/{build_id}/readme   : 지원자 README 본문(응시용 이미지 작업 경로의 README.md 와 같음) + README 검토 표시 —
                                   지원자에게 보이는 공개 정보라 본문을 내준다. 검사·검증을 마친 빌드(review·approved·published)만.
                                   문제 분석 판은 `full` 에 원래 판 README.full.md(요구·공개 수용 기준·계약 규칙 그대로) — 승인 화면에서 나란히
GET  /builds/{build_id}/facilitator: 진행자(응시 플랫폼) 보기 — 요청하면 주는 자료(제목·본문), 질문 답(주제·매칭 낱말·구체·모호한 답),
                                   1단계 제출 뒤 공개할 2단계. 자료 역할·reveals·숨은 요구·허용 설계는 응답 모델에 없다 (starter/facilitator.py).
                                   문제 분석 판(사실이 있는 명세)은 배경 글·요청 글·사실(주제·열림 조건 ask_when·두 층 답)과 열림 조건을 보는
                                   새 규칙 글 — 평가 쪽 LLM 이 항목마다 열림 조건으로 열지 정한다 (2026-11-17 사용자).
                                   진행자 서버용 — 통째로 지원자 화면에 내리지 않는다. README 와 같은 상태의 빌드만
GET  /builds/{build_id}/cases      : 승인자 보기 — 문제를 쓸지 정하는 사람이 보는 테스트 정보 (2026-11-11 사용자): 숨은 케이스(무엇을
                                   하고 무엇이면 통과·의도한 실패 사유)·공개 테스트(무엇을 확인)·숨김·공개 수용 기준·허용 설계·대상별 케이스 결과.
                                   테스트 코드·참조 구현 본문은 없다. 숨은 요구 그 자체라 지원자·평가 쪽 UI 에 내주지 않는다
GET  /delivery-targets · POST /delivery-targets : 전달 대상(이름 → 받을 수 있는 역할 하나)

응답에 넣지 않는 것: 계약 원문·숨은 계획 원문(조절값·시드 행)·오버레이 파일 본문(특히 참조 구현·테스트 코드) — 응답 모델에 칸 자체가 없다.
예외는 /cases: 문제를 쓸지 정하는 승인자에게 숨은 케이스의 상황·통과 조건·의도한 사유와 허용 설계를 보인다 [확정: 2026-11-11 사용자].
사용자별 권한이 아직 없으므로(nginx 기본 인증뿐, docs/deploy.md §5) 이 화면·API 는 내부 승인자만 접근한다는 전제다. 승인은 그래프 interrupt 로만 받고(불변식 9), 전달은 승인·확정된 빌드만 받는다 (저장소 규칙).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request, Response
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel, Field

from task_spec.api.deps import BuildApprovalDep, BuildsDep, DeliveryDep, OntologyDep
from task_spec.api.errors import (
    BuildApprovalInvalid,
    BuildNotFound,
    BuildRuleViolation,
    CasesNotAvailable,
    CommonTaskNotFound,
    DeliveryTargetNotConfigured,
    ErrorResponse,
    FacilitatorNotAvailable,
    ReadmeNotAvailable,
)
from task_spec.graph.build_approval import outcome_key, thread_id
from task_spec.postings.career import PostingCareer, posting_careers
from task_spec.postings.company import PostingCompany, company_id, posting_companies
from task_spec.schemas import CareerLevel, CareerSource
from task_spec.schemas.build import BuildStatus
from task_spec.schemas.build_approval import BuildApprovalDecision, BuildApprovalState
from task_spec.schemas.build_record import (
    TARGET_NAME,
    BuildProvenance,
    BuildRecord,
    Delivery,
    DeliveryTarget,
    StageMark,
)
from task_spec.schemas.package import (
    BUILD_ID,
    COMMON_ID,
    ArtifactFile,
    ArtifactRole,
    CaseResult,
    ReviewVerdict,
    VerifySummary,
    flag_id,
)
from task_spec.starter.facilitator import FacilitatorView, facilitator_view
from task_spec.starter.render import README
from task_spec.starter.store import BuildRepo, BuildRuleError, TaskLabel

router = APIRouter(tags=["builds"])

BuildIdPath = Annotated[str, Path(pattern=BUILD_ID)]


class BuildSummary(BaseModel):
    build_id: str
    common_id: str
    seq: int
    status: str
    ontology_version: str
    created_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    template_ref: str | None = None
    title: str | None = None  # 공통 과제 제목 (명세 본문은 넣지 않는다)
    posting_id: str | None = None
    # 회사 — GET /runs 와 같은 규칙 (postings/company.py). 공고·회사를 모르면 셋 다 null
    company_id: str | None = None
    company: str | None = None
    company_known: bool | None = None
    # 신입·경력 — 공고에 붙은 값 (postings/career.py, GET /postings 와 같음). 사람이 정한 값(confirmed)이 먼저, 없으면
    # 공고 문구 추정(posting_text), 둘 다 없으면 null
    career_level: CareerLevel | None = None
    career_level_source: CareerSource | None = None
    # 큐 (#128) — 생성 요청으로 만든 빌드만 채워진다
    requested_by: str | None = None
    requested_at: datetime | None = None
    stage: str | None = None
    attempt: int = 0
    cost_usd: float = 0.0
    error: str | None = None  # 실패 사유 (원문 없이 짧은 코드·문장)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    packages_final_at: datetime | None = (
        None  # 묶음 확정 (#130 — builder 가 approved 묶음·평가 키트를 쓴 시각)
    )
    stage_log: list[StageMark] = []  # 단계 진행 기록 — 단계·시각·시도·비용만 (화면 진행 막대)


class FlagView(BaseModel):
    flag_id: str
    text: str
    verdict: ReviewVerdict | None = None  # 승인자 판단 (없으면 아직)


class LlmCallSummary(BaseModel):
    """생성기 LLM 호출 요약 한 줄 (#233, 불변식 8) — 단계·시도·프롬프트 판·모델마다 호출 수와 비용 합계.
    호출마다의 provenance(호출 id·설정·few-shot id·토큰)는 빌드 기록(DB)에만. 프롬프트·응답 본문은 provenance 에도 없다."""

    stage: str  # contract · build
    attempt: int  # builder 가 가져간 회차 (죽은 작업 복구 뒤 다시 가져가면 2)
    prompt: str  # <task>@<version>
    prompt_sha256: str  # 그 판 프롬프트 파일의 해시
    model: str  # <provider>:<model>
    calls: int
    cost_usd: float | None = None  # 단가를 모르는 모델이면 None


class BuildDetail(BuildSummary):
    tree_sha256: str | None = None
    grader_sha256: str | None = None
    verification: dict[str, VerifySummary] = Field(default_factory=dict)
    review_flags: list[FlagView] = Field(default_factory=list)
    repairs: int = 0
    files: dict[str, int] = Field(default_factory=dict)  # 오버레이 종류별 파일 수 (본문 없음)
    # 시작 코드·테스트를 만든 프롬프트 판·모델 (#233) — 승인자가 본다. 실패한 빌드도 그때까지 부른 것
    llm_calls: list[LlmCallSummary] = Field(default_factory=list)


class ArtifactView(BaseModel):
    role: ArtifactRole
    files: list[ArtifactFile]
    image_id: str | None = None
    created_at: datetime


class FullReadmeView(BaseModel):
    """문제 분석 판의 원래 판 README(README.full.md) — 요구 사항·공개 수용 기준·계약 규칙 설명이 그대로. 승인 화면에서 지원자 README 와 나란히."""

    path: str
    sha256: str
    size: int
    content: str


class ReadmeView(BaseModel):
    build_id: str
    status: BuildStatus
    path: str
    sha256: str
    size: int
    content: str
    review_flags: list[FlagView]  # README 에서 나온 검토 표시 — 승인자가 판단할 것
    full: FullReadmeView | None = None  # 문제 분석 판만 — 예전 판은 지원자 README 가 곧 원래 판


_README_READABLE: tuple[BuildStatus, ...] = ("review", "approved", "published")


class BuildApprovalView(BaseModel):
    """빌드 승인 화면 — 검토 표시(문장·판단)와 대기 상태. 숨은 테스트·참조 구현 본문은 없다 (테스트 정보는 /cases)."""

    build_id: str
    status: BuildStatus  # 빌드 상태
    state: Literal[
        "awaiting", "approved", "rejected", "closed"
    ]  # 승인 대기·승인됨·반려됨·그 밖에 끝남
    flags: list[FlagView] = Field(default_factory=list)
    error: str | None = None  # 다시 기다리는 사유 (판단이 빠진 표시·저장소 규칙)
    approved_by: str | None = None
    approved_at: datetime | None = None


class BuildRequestIn(BaseModel):
    requested_by: str = Field(min_length=1, max_length=80)  # 요청한 사람 (승인자 이름과 같은 칸)


class BuildRequestOut(BaseModel):
    created: bool  # False 면 이미 진행 중인 빌드
    build: BuildSummary


class DeliveryIn(BaseModel):
    role: ArtifactRole
    target: str = Field(pattern=TARGET_NAME)  # delivery_target.name — 접속 정보는 API 설정에만
    requested_by: str = Field(min_length=1, max_length=80)


class TargetIn(BaseModel):
    name: str = Field(pattern=TARGET_NAME)  # 설정 키 — 주소·자격 증명은 받지 않는다
    role: ArtifactRole


def _summary(
    b: BuildRecord,
    label: TaskLabel | None = None,
    company: PostingCompany | None = None,
    career: PostingCareer | None = None,
) -> BuildSummary:
    return BuildSummary(
        build_id=b.build_id,
        common_id=b.common_id,
        seq=b.seq,
        status=b.status,
        ontology_version=b.ontology_version,
        created_at=b.created_at,
        approved_by=b.approved_by,
        approved_at=b.approved_at,
        template_ref=b.outcome.template_ref if b.outcome else None,
        title=label.title if label else None,
        posting_id=label.posting_id if label else None,
        company_id=company.company_id if company else None,
        company=company.name if company else None,
        company_known=company.known if company else None,
        career_level=career.level if career else None,
        career_level_source=career.source if career else None,
        requested_by=b.requested_by,
        requested_at=b.requested_at,
        stage=b.stage,
        attempt=b.attempt,
        cost_usd=b.cost_usd,
        error=b.error,
        started_at=b.started_at,
        finished_at=b.finished_at,
        packages_final_at=b.packages_final_at,
        stage_log=b.stage_log,
    )


def _llm_calls(calls: list[BuildProvenance]) -> list[LlmCallSummary]:
    """(단계, 시도, 프롬프트 판·sha, 모델)마다 호출 수·비용 합계 — 처음 부른 순서대로."""
    groups: dict[tuple[str, int, str, str, str], list[float | None]] = {}
    for c in calls:
        p = c.provenance
        groups.setdefault((c.stage, c.attempt, p.prompt, p.prompt_sha256, p.model), []).append(
            p.cost_usd
        )
    return [
        LlmCallSummary(
            stage=stage,
            attempt=attempt,
            prompt=prompt,
            prompt_sha256=sha,
            model=model,
            calls=len(costs),
            cost_usd=None
            if all(x is None for x in costs)
            else round(sum(x or 0.0 for x in costs), 4),
        )
        for (stage, attempt, prompt, sha, model), costs in groups.items()
    ]


async def _build(store: BuildRepo, build_id: str) -> BuildRecord:
    b = await store.get(build_id)
    if b is None:
        raise BuildNotFound(build_id)
    return b


async def _companies(
    request: Request, ontology: OntologyDep, labels: dict[str, TaskLabel]
) -> dict[str, PostingCompany]:
    """공통 과제 id → 회사 (공통 과제가 가리키는 공고의 회사)."""
    ctx = getattr(request.app.state, "postings", None)
    found = await posting_companies(
        {lb.posting_id for lb in labels.values()},
        ontology=ontology,
        store=ctx.store if ctx else None,
    )
    return {cid: found[lb.posting_id] for cid, lb in labels.items() if lb.posting_id in found}


async def _careers(
    request: Request, ontology: OntologyDep, labels: dict[str, TaskLabel]
) -> dict[str, PostingCareer]:
    """공통 과제 id → 신입·경력 (공통 과제가 가리키는 공고의 값)."""
    ctx = getattr(request.app.state, "postings", None)
    found = await posting_careers(
        {lb.posting_id for lb in labels.values()},
        ontology=ontology,
        store=ctx.store if ctx else None,
    )
    return {cid: found[lb.posting_id] for cid, lb in labels.items() if lb.posting_id in found}


@router.get("/builds")
async def list_builds(
    request: Request,
    store: BuildsDep,
    ontology: OntologyDep,
    common_id: Annotated[str | None, Query(pattern=COMMON_ID)] = None,
    company: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> list[BuildSummary]:
    """최근 빌드부터, limit 이 없으면 전부. `company` 는 회사 id 또는 표기 (GET /runs 와 같은 규칙)."""
    if common_id:
        builds = await store.builds(common_id)
    else:
        builds = await store.recent(None if company else limit)
    labels = await store.task_labels({b.common_id for b in builds})
    companies = await _companies(request, ontology, labels)
    if company:
        wanted = company_id(company)
        builds = [
            b for b in builds if (c := companies.get(b.common_id)) and c.company_id == wanted
        ][:limit]
    careers = await _careers(request, ontology, labels)
    return [
        _summary(b, labels.get(b.common_id), companies.get(b.common_id), careers.get(b.common_id))
        for b in builds
    ]


def _pending(snapshot: Any) -> dict[str, Any] | None:
    for task in snapshot.tasks:
        for itr in task.interrupts:
            if isinstance(itr.value, dict):
                return itr.value
    return None


def _closed_view(b: BuildRecord) -> BuildApprovalView:
    verdicts = {d.flag_id: d.verdict for d in b.review_decisions}
    flags = [
        FlagView(flag_id=flag_id(f), text=f, verdict=verdicts.get(flag_id(f)))
        for f in (b.outcome.review_flags if b.outcome else [])
    ]
    if b.status in ("approved", "published"):
        state: Literal["approved", "rejected", "closed"] = "approved"
    elif b.status == "failed" and (b.error or "").startswith("승인자 반려"):
        state = "rejected"
    else:
        state = "closed"
    return BuildApprovalView(
        build_id=b.build_id,
        status=b.status,
        state=state,
        flags=flags,
        error=b.error if state == "rejected" else None,
        approved_by=b.approved_by,
        approved_at=b.approved_at,
    )


async def _approval_view(graph: Any, b: BuildRecord, start: bool) -> BuildApprovalView | None:
    """review 빌드의 승인 thread — 없으면(start) 시작해 interrupt 까지. 기다리는 중이 아니면 None."""
    cfg = {"configurable": {"thread_id": thread_id(b)}}
    snap = await graph.aget_state(cfg)
    if not snap.values and start:
        state = BuildApprovalState(build_id=b.build_id, outcome_key=outcome_key(b))
        await graph.ainvoke(state.model_dump(), cfg)
        snap = await graph.aget_state(cfg)
    payload = _pending(snap)
    if payload is None:
        return None
    return BuildApprovalView(
        build_id=b.build_id,
        status=b.status,
        state="awaiting",
        flags=[FlagView(flag_id=f["flag_id"], text=f["text"]) for f in payload.get("flags", [])],
        error=payload.get("error"),
    )


@router.get("/builds/{build_id}/approval")
async def get_build_approval(
    build_id: BuildIdPath, store: BuildsDep, graph: BuildApprovalDep
) -> BuildApprovalView:
    b = await _build(store, build_id)
    if b.status != "review" or b.outcome is None:
        return _closed_view(b)
    view = await _approval_view(graph, b, start=True)
    if view is None:  # thread 가 끝났는데 빌드는 그대로 — 다른 요청이 처리 중이거나 낡은 thread
        raise BuildRuleViolation(f"{build_id}: 승인 대기 중이 아니다 — 다시 열어 보세요")
    return view


@router.post("/builds/{build_id}/approve", responses={422: {"model": ErrorResponse}})
async def approve_build(
    build_id: BuildIdPath,
    body: BuildApprovalDecision,  # TODO(auth): reviewer 는 인증 전까지 요청 본문에서 받는다
    store: BuildsDep,
    graph: BuildApprovalDep,
) -> BuildApprovalView:
    """그래프 resume 만 한다. 승인·반려가 끝나면 결과, 그래프가 다시 기다리면 422 + 사유."""
    b = await _build(store, build_id)
    if b.status != "review" or b.outcome is None:
        raise BuildRuleViolation(f"{build_id}: review 상태에서만 승인·반려 (지금 {b.status})")
    if await _approval_view(graph, b, start=True) is None:
        raise BuildRuleViolation(f"{build_id}: 승인 대기 중이 아니다")
    cfg: RunnableConfig = {"configurable": {"thread_id": thread_id(b)}}
    await graph.ainvoke(Command(resume=body.model_dump(mode="json")), cfg)
    after = await _build(store, build_id)
    if after.status == "review":
        waiting = await _approval_view(graph, after, start=False)
        raise BuildApprovalInvalid(
            (waiting.error if waiting is not None else None)
            or "승인이 적용되지 않았다 — 다시 열어 보세요"
        )
    return _closed_view(after)


@router.post(
    "/common-tasks/{common_id}/builds",
    status_code=202,
    responses={200: {"model": BuildRequestOut, "description": "이미 진행 중인 빌드"}},
)
async def request_build(
    common_id: Annotated[str, Path(pattern=COMMON_ID)],
    body: BuildRequestIn,
    store: BuildsDep,
    response: Response,
) -> BuildRequestOut:
    try:
        b, created = await store.request_build(common_id, body.requested_by)
    except KeyError as e:
        raise CommonTaskNotFound(common_id) from e
    except BuildRuleError as e:
        raise BuildRuleViolation(str(e)) from e
    if not created:
        response.status_code = 200
    label = (await store.task_labels([common_id])).get(common_id)
    return BuildRequestOut(created=created, build=_summary(b, label))


@router.get("/builds/{build_id}")
async def get_build(
    build_id: BuildIdPath, request: Request, store: BuildsDep, ontology: OntologyDep
) -> BuildDetail:
    b = await _build(store, build_id)
    o = b.outcome
    verdicts = {d.flag_id: d.verdict for d in b.review_decisions}
    flags = [
        FlagView(flag_id=flag_id(f), text=f, verdict=verdicts.get(flag_id(f)))
        for f in (o.review_flags if o else [])
    ]
    kinds = Counter[str](f.kind for f in await store.files(build_id))
    labels = await store.task_labels([b.common_id])
    companies = await _companies(request, ontology, labels)
    careers = await _careers(request, ontology, labels)
    return BuildDetail(
        **_summary(
            b, labels.get(b.common_id), companies.get(b.common_id), careers.get(b.common_id)
        ).model_dump(),
        tree_sha256=o.tree_sha256 if o else None,
        grader_sha256=o.grader_sha256 if o else None,
        verification=o.verification if o else {},
        review_flags=flags,
        repairs=len(o.repairs) if o else 0,
        files=dict(kinds),
        llm_calls=_llm_calls(b.provenance),
    )


@router.get("/builds/{build_id}/artifacts")
async def list_artifacts(build_id: BuildIdPath, store: BuildsDep) -> list[ArtifactView]:
    await _build(store, build_id)
    return [
        ArtifactView(role=a.role, files=a.files, image_id=a.image_id, created_at=a.created_at)
        for a in await store.artifacts(build_id)
    ]


@router.get("/builds/{build_id}/deliveries")
async def list_deliveries(build_id: BuildIdPath, store: BuildsDep) -> list[Delivery]:
    await _build(store, build_id)
    return await store.deliveries(build_id)


@router.post("/builds/{build_id}/deliveries", status_code=202)
async def request_delivery(
    build_id: BuildIdPath, body: DeliveryIn, store: BuildsDep, sender: DeliveryDep
) -> Delivery:
    b = await _build(store, build_id)
    if not sender.has_target(body.target):
        raise DeliveryTargetNotConfigured(body.target)
    label = (await store.task_labels([b.common_id])).get(b.common_id)
    if label is None or label.revision is None:
        raise BuildRuleViolation(
            f"{build_id}: 공통 과제 판을 알 수 없다 — 원격 경로를 만들 수 없음"
        )
    try:
        d = await store.request_delivery(build_id, body.role, body.target, body.requested_by)
    except BuildRuleError as e:
        raise BuildRuleViolation(str(e)) from e
    sender.launch(d, b.common_id, label.revision)
    return d


@router.get("/builds/{build_id}/readme")
async def get_readme(build_id: BuildIdPath, store: BuildsDep) -> ReadmeView:
    b = await _build(store, build_id)
    if b.status not in _README_READABLE:
        raise ReadmeNotAvailable(build_id, f"상태 {b.status}")
    readme = await store.readme(build_id)
    if readme is None:
        raise ReadmeNotAvailable(build_id, "저장한 README 없음")
    verdicts = {d.flag_id: d.verdict for d in b.review_decisions}
    flags = [
        FlagView(flag_id=flag_id(f), text=f, verdict=verdicts.get(flag_id(f)))
        for f in (b.outcome.review_flags if b.outcome else [])
        if f.startswith(README)
    ]
    full = await store.full_readme(build_id)
    return ReadmeView(
        build_id=build_id,
        status=b.status,
        path=readme.path,
        sha256=readme.sha256,
        size=readme.size,
        content=readme.content,
        review_flags=flags,
        full=None
        if full is None
        else FullReadmeView(
            path=full.path, sha256=full.sha256, size=full.size, content=full.content
        ),
    )


@router.get("/builds/{build_id}/facilitator")
async def get_facilitator(build_id: BuildIdPath, store: BuildsDep) -> FacilitatorView:
    b = await _build(store, build_id)
    if b.status not in _README_READABLE:
        raise FacilitatorNotAvailable(build_id, f"상태 {b.status}")
    task = await store.task(b.common_id)
    if task is None:
        raise FacilitatorNotAvailable(build_id, "공통 과제 명세 없음")
    return facilitator_view(b.common_id, task)


class CaseView(BaseModel):
    """케이스 하나 — 숨은 케이스는 상황·통과 조건·의도한 실패 사유, 공개 테스트는 확인하는 것."""

    id: str
    kind: str  # hidden · public
    ac_id: str
    trap_id: str | None = None
    scenario: str | None = None
    expect: str
    intended_reason: str | None = None
    setup: list[str] = Field(default_factory=list)


class AcceptanceView(BaseModel):
    ac_id: str
    text: str
    trap_id: str | None = None


class DesignView(BaseModel):
    design_id: str
    summary: str
    conditions: str
    trap_ids: list[str]


class BuildCasesView(BaseModel):
    build_id: str
    status: BuildStatus
    cases: list[CaseView]
    acceptance_hidden: list[AcceptanceView]
    acceptance_public: list[AcceptanceView]
    accepted_designs: list[DesignView]
    results: dict[str, list[CaseResult]]  # 검증 대상 → 케이스별 결과 (예전 빌드는 비어 있음)


@router.get("/builds/{build_id}/cases")
async def get_cases(build_id: BuildIdPath, store: BuildsDep) -> BuildCasesView:
    b = await _build(store, build_id)
    if b.status not in _README_READABLE or b.outcome is None:
        raise CasesNotAvailable(build_id, f"상태 {b.status}")
    plan, contract = b.outcome.hidden_plan, b.outcome.contract
    task = await store.task(b.common_id)
    cases = [
        CaseView(
            id=c.case_id,
            kind="hidden",
            ac_id=c.ac_id,
            trap_id=c.trap_id,
            scenario=c.scenario,
            expect=c.expect,
            intended_reason=c.intended_reason,
            setup=list(c.setup),
        )
        for c in plan.cases
    ] + [
        CaseView(id=t.test_id, kind="public", ac_id=t.ac_id, expect=t.checks)
        for t in contract.public_tests
    ]
    return BuildCasesView(
        build_id=build_id,
        status=b.status,
        cases=cases,
        acceptance_hidden=[
            AcceptanceView(ac_id=a.ac_id, text=a.text, trap_id=a.trap_id)
            for a in (task.acceptance_hidden if task else [])
        ],
        acceptance_public=[
            AcceptanceView(ac_id=a.ac_id, text=a.text)
            for a in (task.acceptance_public if task else [])
        ],
        accepted_designs=[
            DesignView(
                design_id=d.design_id,
                summary=d.summary,
                conditions=d.conditions,
                trap_ids=list(d.trap_ids),
            )
            for d in (task.accepted_designs if task else [])
        ],
        results={name: list(v.cases) for name, v in b.outcome.verification.items()},
    )


@router.get("/delivery-targets")
async def list_targets(store: BuildsDep) -> list[DeliveryTarget]:
    return await store.targets()


@router.post("/delivery-targets", status_code=201)
async def add_target(body: TargetIn, store: BuildsDep) -> DeliveryTarget:
    try:
        return await store.add_target(body.name, body.role)
    except BuildRuleError as e:
        raise BuildRuleViolation(str(e)) from e
