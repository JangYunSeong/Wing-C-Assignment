"""공통 과제 빌드 저장 모델 — `common_task_build` · `starter_file` · `build_artifact` · `artifact_delivery` (#52·#85) [제안].

- 빌드 1건 = 공통 과제 판(`common_task.id` = `ct-…`, 판마다 새 id) 위의 시작 코드·숨은 테스트·참조 구현 생성 1회.
  같은 판을 다시 빌드하면 새 빌드(`-b2`)다 — 다시 빌드한 이미지는 `image_id` 가 달라지므로 승인된 빌드의 파일은 바꾸지 않는다.
- 오버레이 본문은 `starter_file` 에만. 그래프 State(`BuildState`)에는 `FileRef`(본문 없음)만.
- 전달은 승인(시작 코드 승인 — 그래프 interrupt 로 받은 결과)된 빌드만. 승인은 검토 표시마다 승인자 판단이 있어야 한다.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.build import BuildStatus, FileKind, HiddenPlan, RepairRecord, TaskContract
from task_spec.schemas.package import (
    BUILD_ID,
    COMMON_ID,
    IMAGE_ID,
    SHA256,
    TEMPLATE_REF,
    ArtifactFile,
    ArtifactRole,
    ReviewDecision,
    VerifySummary,
)

DeliveryStatus = Literal["requested", "sent", "failed"]
TARGET_NAME = r"^[a-z0-9][a-z0-9_.-]{0,62}$"
_VARIANT = (
    r"^$|^[a-z0-9][a-z0-9_-]{0,39}$"  # 참조 구현 라벨 (naive-all · d1) — starter·hidden 은 빈 값
)


class BuildOutcome(Schema):
    """생성·검증이 끝난 뒤 빌드에 남기는 것 (본문 없음)."""

    template_ref: str = Field(pattern=TEMPLATE_REF)
    template_sha256: str | None = Field(default=None, pattern=SHA256)
    tree_sha256: str = Field(pattern=SHA256)
    grader_sha256: str = Field(pattern=SHA256)
    contract: TaskContract
    hidden_plan: HiddenPlan
    verification: dict[str, VerifySummary] = Field(default_factory=dict)
    review_flags: list[str] = Field(default_factory=list)
    repairs: list[RepairRecord] = Field(default_factory=list)


class BuildRecord(Schema):
    """`common_task_build` 행."""

    build_id: str = Field(pattern=BUILD_ID)
    common_id: str = Field(pattern=COMMON_ID)
    seq: int = Field(ge=1)
    status: BuildStatus
    ontology_version: str = Field(min_length=1)
    outcome: BuildOutcome | None = None
    review_decisions: list[ReviewDecision] = Field(default_factory=list)
    created_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    retain_until: datetime | None = (
        None  # 재채점 기간 끝 — 그 전에는 묶음 파일을 지우지 않는다 [미정: 기간]
    )

    @model_validator(mode="after")
    def _ids_match(self) -> BuildRecord:
        if self.build_id != f"{self.common_id}-b{self.seq}":
            raise ValueError(f"build_id {self.build_id} ≠ {self.common_id}-b{self.seq}")
        if self.status in ("approved", "published") and (
            self.outcome is None or self.approved_by is None
        ):
            raise ValueError("승인된 빌드에 outcome·approved_by 가 없다")
        return self


class StarterFile(Schema):
    """`starter_file` 행 — 오버레이 원본 (템플릿에 더하거나 바꾼 파일만). 이미지는 이것으로 다시 만든다."""

    build_id: str = Field(pattern=BUILD_ID)
    kind: FileKind
    variant: str = Field(
        default="", pattern=_VARIANT
    )  # ref_* 는 참조 구현마다 같은 경로가 있으므로 라벨로 나눈다
    path: str = Field(min_length=1, max_length=300)
    content: str
    sha256: str = Field(pattern=SHA256)
    size: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> StarterFile:
        p = self.path
        if p.startswith("/") or "\\" in p or any(part in ("", ".", "..") for part in p.split("/")):
            raise ValueError(f"오버레이 경로는 정규화된 상대 경로만: {p!r}")
        is_ref = self.kind in ("ref_naive", "ref_accepted")
        if is_ref != bool(self.variant):
            raise ValueError("variant 는 ref_* 에만, ref_* 에는 반드시")
        data = self.content.encode("utf-8")
        if hashlib.sha256(data).hexdigest() != self.sha256 or len(data) != self.size:
            raise ValueError(f"{p}: sha256·size 가 본문과 다르다")
        return self

    @classmethod
    def of(
        cls, build_id: str, kind: FileKind, path: str, content: str, variant: str = ""
    ) -> StarterFile:
        data = content.encode("utf-8")
        return cls(
            build_id=build_id,
            kind=kind,
            variant=variant,
            path=path,
            content=content,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )


class BuildArtifact(Schema):
    """`build_artifact` 행 — 역할 폴더 하나 (`data/images/<build_id>/<role>/`). 한 번 쓰면 바꾸지 않는다."""

    build_id: str = Field(pattern=BUILD_ID)
    role: ArtifactRole
    files: list[ArtifactFile] = Field(min_length=1)  # SHA256SUMS 와 같은 목록
    image_id: str | None = Field(default=None, pattern=IMAGE_ID)
    # 이 묶음을 만든 원본 — 빌드의 현재 outcome 과 같아야 등록·승인·전달된다 (수리 뒤 낡은 묶음 방지)
    tree_sha256: str = Field(pattern=SHA256)
    grader_sha256: str = Field(pattern=SHA256)
    created_at: datetime

    @model_validator(mode="after")
    def _check(self) -> BuildArtifact:
        if (self.role == "eval_kit") != (self.image_id is None):
            raise ValueError("image_id 는 candidate·grader 에만, 둘에는 반드시")
        names = [f.name for f in self.files]
        if len(set(names)) != len(names):
            raise ValueError("같은 파일 이름이 두 번")
        descriptor = {
            "candidate": "candidate.json",
            "grader": "grader.json",
            "eval_kit": "kit.json",
        }[self.role]
        if descriptor not in names:
            raise ValueError(f"{self.role} 폴더에 {descriptor} 가 없다")
        if self.role != "eval_kit" and f"{self.build_id}-{self.role}.tar.zst" not in names:
            raise ValueError(
                f"{self.role} 폴더에 이미지 파일 {self.build_id}-{self.role}.tar.zst 가 없다"
            )
        return self


class DeliveryTarget(Schema):
    """`delivery_target` 행 — 보낼 곳 하나와 받을 수 있는 역할 하나. SFTP 에는 계정별 pull 권한이 없으므로 저장소가 막는다."""

    name: str = Field(pattern=TARGET_NAME)  # 설정 키 — 주소·자격 증명은 넣지 않는다
    role: ArtifactRole
    created_at: datetime


class Delivery(Schema):
    """`artifact_delivery` 행 — 보낸 기록 (레지스트리 pull 기록 대신). 이의 제기 때 '그 판의 그 파일'을 증명한다."""

    delivery_id: str = Field(pattern=r"^dl-[0-9a-f]{12}$")
    build_id: str = Field(pattern=BUILD_ID)
    role: ArtifactRole
    target: str = Field(pattern=TARGET_NAME)  # delivery_target.name — 주소·자격 증명은 넣지 않는다
    requested_by: str = Field(min_length=1)
    requested_at: datetime
    status: DeliveryStatus = "requested"
    files: list[ArtifactFile] = Field(min_length=1)  # 요청 시점의 파일 목록·해시 (보낸 것)
    sent_at: datetime | None = None
    error: str | None = Field(default=None, max_length=500)  # 원문·자격 증명 없이 사유만
