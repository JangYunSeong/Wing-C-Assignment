"""지원자 문서 참조와 가명화 결과 참조.

State 에는 `data/` 안의 **참조(경로·해시)** 만 둔다. 원문 텍스트·가명화 매핑은 여기 넣지 않는다
(체크포인터가 State 를 디스크에 쓰기 때문, pipeline-graph 스킬 State 규칙).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema

DocumentKind = Literal["resume", "cover_letter"]

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class DocumentRef(Schema):
    """업로드 원본 위치. 경로는 `data/raw/<run_id>/` 아래."""

    doc_id: str = Field(min_length=1)
    kind: DocumentKind
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)


class PseudoCheck(Schema):
    """가명화 누락 검사 결과. 잔여 값 자체는 남기지 않는다 (pseudonymization 스킬)."""

    passed: bool
    residual_count: int = Field(ge=0)
    detectors: list[str] = Field(default_factory=list)
    # 실패 사유 코드. residual_pii(누락 검사) / ingest_required(텍스트 추출 전 형식) / read_error
    reason: Literal["residual_pii", "ingest_required", "read_error"] | None = None
    # 범주별 치환 건수 (값은 남기지 않는다)
    counts: dict[str, int] = Field(default_factory=dict)


class PseudoDocRef(Schema):
    """가명화본 위치 + 검사 결과. 매핑(토큰 → 원 값)은 State 밖 `data/pseudonymized/<run_id>/`."""

    doc_id: str = Field(min_length=1)
    kind: DocumentKind
    path: str | None = None  # 검사 실패 시 가명화본을 쓰지 않을 수 있다
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    check: PseudoCheck
