"""받는 쪽이 가져가는 전달 (/pull) — SFTP(#116)로 밀어 보내는 대신, 채점·평가 서버가 HTTPS 와 토큰으로 묶음을 내려받는다.

받는 곳마다 SSH 계정·공개키 등록·방화벽 대신 토큰 하나 (2026-11-14 사용자). SFTP 전달은 그대로 둔다 — 받는 곳마다 고른다.

GET  /pull/builds                          : 이 토큰의 역할로 가져갈 수 있는 빌드 (승인·확정된 것) — 파일 목록·받았음 표시
GET  /pull/builds/{build_id}/files/{name}  : 묶음 파일 하나 (내려받기). `SHA256SUMS` 는 디스크가 아니라 DB 기록으로 만든다 —
                                             디스크에서 바뀐 파일은 받는 쪽 `sha256sum -c` 에서 걸린다
POST /pull/builds/{build_id}/done {sha256sums_sha256} : 다 받고 확인했음 → 전달 기록(sent)·첫 전달이면 published.
                                             받은 SHA256SUMS 의 해시가 DB 기록과 같아야 한다. 이미 기록됐으면 그 기록을 그대로 (200)

인증: `Authorization: Bearer <토큰>` — `PULL_TOKENS__<이름>` 의 이름이 DB delivery_target 이고, 그 대상의 역할 하나만 연다
(평가·채점 분리, #116). nginx 는 이 경로에 Basic 인증을 걸지 않는다 (docker/nginx/nginx.conf). 다시 내려받기는 막지 않는다 —
받는 쪽 파일이 깨졌을 때 다시 받을 수 있게. 기록(done)만 한 번.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from task_spec.api.deps import BuildsDep
from task_spec.api.errors import BuildRuleViolation, PullNotAvailable, PullUnauthorized
from task_spec.config import Settings
from task_spec.schemas.build_record import BuildArtifact, Delivery, DeliveryTarget
from task_spec.schemas.package import BUILD_ID, FILE_NAME, SHA256, ArtifactFile, ArtifactRole
from task_spec.starter.store import (
    DELIVERABLE,
    BuildRepo,
    BuildRuleError,
    check_artifact_current,
    role_dir,
    sha256sums,
)

router = APIRouter(prefix="/pull", tags=["pull"])

SUMS = "SHA256SUMS"
BuildIdPath = Annotated[str, Path(pattern=BUILD_ID)]


async def puller(
    request: Request,
    store: BuildsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> DeliveryTarget:
    """토큰 → 받는 곳 (이름·역할). 설정에 있어도 DB 에 등록되지 않은 이름이면 거부."""
    settings: Settings = request.app.state.settings
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise PullUnauthorized()
    name = next(
        (
            n
            for n, t in settings.pull_tokens.items()
            if hmac.compare_digest(t.get_secret_value().encode(), token.strip().encode())
        ),
        None,
    )
    target = next((t for t in await store.targets() if t.name == name), None) if name else None
    if target is None:
        raise PullUnauthorized()
    return target


PullerDep = Annotated[DeliveryTarget, Depends(puller)]


class PullBuild(BaseModel):
    build_id: str
    common_id: str
    role: ArtifactRole
    files: list[ArtifactFile] = Field(
        description="SHA256SUMS 한 줄씩과 같다. SHA256SUMS 자체도 내려받을 수 있다"
    )
    sha256sums_sha256: str = Field(description="SHA256SUMS 본문의 sha256 — done 에 그대로 돌려준다")
    delivered: bool = Field(description="이 받는 곳에 이미 받음으로 기록됐는가")
    approved_at: datetime | None


class DoneIn(BaseModel):
    sha256sums_sha256: str = Field(pattern=SHA256)


async def _deliverable(store: BuildRepo, build_id: str, role: ArtifactRole) -> BuildArtifact:
    """가져갈 수 있는 묶음 — 승인·확정된 빌드, 그 역할의 지금 묶음. 아니면 어느 경우든 같은 404."""
    b = await store.get(build_id)
    if b is None or b.status not in DELIVERABLE or b.packages_final_at is None:
        raise PullNotAvailable(build_id)
    artifact = next((a for a in await store.artifacts(build_id) if a.role == role), None)
    if artifact is None:
        raise PullNotAvailable(build_id)
    try:
        check_artifact_current(b, artifact)
    except BuildRuleError as e:
        raise PullNotAvailable(build_id) from e
    return artifact


def _sums_hash(files: list[ArtifactFile]) -> str:
    return hashlib.sha256(sha256sums(files).encode()).hexdigest()


@router.get("/builds")
async def list_pullable(store: BuildsDep, who: PullerDep) -> list[PullBuild]:
    out: list[PullBuild] = []
    for b in await store.recent(200):
        if b.status not in DELIVERABLE or b.packages_final_at is None:
            continue
        try:
            a = await _deliverable(store, b.build_id, who.role)
        except PullNotAvailable:
            continue
        done = any(
            d.role == who.role and d.target == who.name and d.status == "sent"
            for d in await store.deliveries(b.build_id)
        )
        out.append(
            PullBuild(
                build_id=b.build_id,
                common_id=b.common_id,
                role=who.role,
                files=a.files,
                sha256sums_sha256=_sums_hash(a.files),
                delivered=done,
                approved_at=b.approved_at,
            )
        )
    return out


@router.get("/builds/{build_id}/files/{name}", response_model=None)
async def pull_file(
    request: Request,
    build_id: BuildIdPath,
    name: Annotated[str, Path(pattern=FILE_NAME)],
    store: BuildsDep,
    who: PullerDep,
) -> Response:
    a = await _deliverable(store, build_id, who.role)
    if name == SUMS:
        return Response(sha256sums(a.files), media_type="text/plain; charset=utf-8")
    f = next((f for f in a.files if f.name == name), None)  # 기록된 이름만 — 경로를 받지 않는다
    if f is None:
        raise PullNotAvailable(build_id)
    settings: Settings = request.app.state.settings
    path = role_dir(settings.images_root, build_id, who.role) / f.name
    if not path.is_file() or path.stat().st_size != f.size_bytes:
        raise PullNotAvailable(build_id)
    return FileResponse(path, filename=f.name, media_type="application/octet-stream")


@router.post("/builds/{build_id}/done")
async def pull_done(
    build_id: BuildIdPath, body: DoneIn, store: BuildsDep, who: PullerDep
) -> Delivery:
    a = await _deliverable(store, build_id, who.role)
    if not hmac.compare_digest(body.sha256sums_sha256, _sums_hash(a.files)):
        raise BuildRuleViolation(f"{build_id}: 받은 SHA256SUMS 가 기록과 다르다 — 다시 받을 것")
    sent = next(
        (
            d
            for d in await store.deliveries(build_id)
            if d.role == who.role and d.target == who.name and d.status == "sent"
        ),
        None,
    )
    if sent is not None:
        return sent
    try:
        d = await store.request_delivery(build_id, who.role, who.name, f"pull:{who.name}")
        return await store.finish_delivery(d.delivery_id, True, datetime.now(UTC))
    except BuildRuleError as e:
        raise BuildRuleViolation(str(e)) from e
