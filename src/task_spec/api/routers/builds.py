"""공통 과제 빌드·전달 묶음 조회 API (이슈 #85) [제안].

GET  /builds[?common_id=]        : 공통 과제의 빌드 목록 (없으면 최근 빌드 — 화면 목록용, 제목·공고 id 함께)
GET  /builds/{build_id}          : 상태·검증 요약·검토 표시(+승인자 판단)·파일 수
GET  /builds/{build_id}/artifacts: 역할별 묶음 파일(이름·sha256·크기)·image_id
GET  /builds/{build_id}/deliveries: 전달 기록
GET  /builds/{build_id}/readme   : 지원자 README 본문(응시용 이미지 작업 경로의 README.md 와 같음) + README 검토 표시 —
                                   지원자에게 보이는 공개 정보라 본문을 내준다. 검사·검증을 마친 빌드(review·approved·published)만
GET  /delivery-targets · POST /delivery-targets : 전달 대상(이름 → 받을 수 있는 역할 하나)

응답에 넣지 않는 것: 계약 원문·숨은 계획(HiddenPlan)·오버레이 파일 본문(특히 참조 구현) — 사용자별 권한이 없으므로
응답 모델에 칸 자체가 없다. 승인은 그래프 interrupt 로만 받고(불변식 9), 전달 요청은 승인 경로와 함께 추가한다.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from task_spec.api.deps import BuildsDep
from task_spec.api.errors import BuildNotFound, BuildRuleViolation, ReadmeNotAvailable
from task_spec.schemas.build import BuildStatus
from task_spec.schemas.build_record import TARGET_NAME, BuildRecord, Delivery, DeliveryTarget
from task_spec.schemas.package import (
    BUILD_ID,
    COMMON_ID,
    ArtifactFile,
    ArtifactRole,
    ReviewVerdict,
    VerifySummary,
    flag_id,
)
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


class FlagView(BaseModel):
    flag_id: str
    text: str
    verdict: ReviewVerdict | None = None  # 승인자 판단 (없으면 아직)


class BuildDetail(BuildSummary):
    tree_sha256: str | None = None
    grader_sha256: str | None = None
    verification: dict[str, VerifySummary] = Field(default_factory=dict)
    review_flags: list[FlagView] = Field(default_factory=list)
    repairs: int = 0
    files: dict[str, int] = Field(default_factory=dict)  # 오버레이 종류별 파일 수 (본문 없음)


class ArtifactView(BaseModel):
    role: ArtifactRole
    files: list[ArtifactFile]
    image_id: str | None = None
    created_at: datetime


class ReadmeView(BaseModel):
    build_id: str
    status: BuildStatus
    path: str
    sha256: str
    size: int
    content: str
    review_flags: list[FlagView]  # README 에서 나온 검토 표시 — 승인자가 판단할 것


_README_READABLE: tuple[BuildStatus, ...] = ("review", "approved", "published")


class TargetIn(BaseModel):
    name: str = Field(pattern=TARGET_NAME)  # 설정 키 — 주소·자격 증명은 받지 않는다
    role: ArtifactRole


def _summary(b: BuildRecord, label: TaskLabel | None = None) -> BuildSummary:
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
    )


async def _build(store: BuildRepo, build_id: str) -> BuildRecord:
    b = await store.get(build_id)
    if b is None:
        raise BuildNotFound(build_id)
    return b


@router.get("/builds")
async def list_builds(
    store: BuildsDep,
    common_id: Annotated[str | None, Query(pattern=COMMON_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[BuildSummary]:
    builds = await store.builds(common_id) if common_id else await store.recent(limit)
    labels = await store.task_labels({b.common_id for b in builds})
    return [_summary(b, labels.get(b.common_id)) for b in builds]


@router.get("/builds/{build_id}")
async def get_build(build_id: BuildIdPath, store: BuildsDep) -> BuildDetail:
    b = await _build(store, build_id)
    o = b.outcome
    verdicts = {d.flag_id: d.verdict for d in b.review_decisions}
    flags = [
        FlagView(flag_id=flag_id(f), text=f, verdict=verdicts.get(flag_id(f)))
        for f in (o.review_flags if o else [])
    ]
    kinds = Counter[str](f.kind for f in await store.files(build_id))
    label = (await store.task_labels([b.common_id])).get(b.common_id)
    return BuildDetail(
        **_summary(b, label).model_dump(),
        tree_sha256=o.tree_sha256 if o else None,
        grader_sha256=o.grader_sha256 if o else None,
        verification=o.verification if o else {},
        review_flags=flags,
        repairs=len(o.repairs) if o else 0,
        files=dict(kinds),
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
    return ReadmeView(
        build_id=build_id,
        status=b.status,
        path=readme.path,
        sha256=readme.sha256,
        size=readme.size,
        content=readme.content,
        review_flags=flags,
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
