"""공통 과제 빌드 API — 지원자에게 보이는 공개 정보만.

GET /builds/{build_id}/readme : 지원자 README (응시용 이미지의 작업 경로 README.md 와 같은 본문) + README 검토 표시
숨은 테스트·참조 구현·숨은 계획·평가 키트는 내주지 않는다. 검사·검증을 마친 빌드(review·approved·published)만 —
README 누설은 빌드 전 정적 검사가 막고, 막지 못한 것(승인된 명세 문장에서 나온 것)은 검토 표시로 승인자에게 간다.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from task_spec.api.deps import BuildsDep
from task_spec.api.errors import BuildNotFound, ReadmeNotAvailable
from task_spec.schemas.build import BuildStatus
from task_spec.starter.render import README

router = APIRouter(prefix="/builds", tags=["builds"])

_READABLE: tuple[BuildStatus, ...] = ("review", "approved", "published")


class ReadmeView(BaseModel):
    build_id: str
    status: BuildStatus
    path: str
    sha256: str
    size: int
    content: str
    review_flags: list[str]  # README 에서 나온 검토 표시 — 승인자가 판단할 것


@router.get("/{build_id}/readme", response_model=ReadmeView)
async def get_readme(build_id: str, builds: BuildsDep) -> ReadmeView:
    build = await builds.get(build_id)
    if build is None:
        raise BuildNotFound(build_id)
    if build.status not in _READABLE:
        raise ReadmeNotAvailable(build_id, f"상태 {build.status}")
    readme = await builds.readme(build_id)
    if readme is None:
        raise ReadmeNotAvailable(build_id, "저장한 README 없음")
    flags = build.outcome.review_flags if build.outcome else []
    return ReadmeView(
        build_id=build_id,
        status=build.status,
        path=readme.path,
        sha256=readme.sha256,
        size=readme.size,
        content=readme.content,
        review_flags=[f for f in flags if f.startswith(README)],
    )
