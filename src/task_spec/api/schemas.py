"""API 요청/응답 모델. 원문·가명화 매핑은 담지 않는다."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from task_spec.schemas import ApprovalSummary, NodeError, TaskSpec

RunStatus = Literal["running", "awaiting_approval", "done", "rejected", "failed"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "rejected", "failed"})


class RunAccepted(BaseModel):
    run_id: str
    status: RunStatus
    # 받았지만 저장·처리하지 않은 문서 (예: USE_COVER_LETTERS=false 일 때 자기소개서) — 파일 이름은 넣지 않는다
    ignored: list[str] = []


class RunView(BaseModel):
    """체크포인터 스냅샷에서 계산한 실행 상태."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: RunStatus
    current_node: str | None = None
    ontology_version: str | None = None
    revision: int | None = None
    spec: TaskSpec | None = None  # awaiting_approval 일 때만 (승인 대기 초안)
    approval_error: str | None = None  # 직전 결정이 거부된 사유 (interrupt payload.error)
    # 승인자가 먼저 볼 요약 (interrupt payload.summary — nodes/spec.py approval_summary). 주장 인용 없음.
    approval_summary: ApprovalSummary | None = None
    errors: list[NodeError] = []


class RunEvent(BaseModel):
    run_id: str
    status: RunStatus
    current_node: str | None = None
    revision: int | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "error", "not_applicable"]
    ontology_version: str  # 설정값 (dev 별칭 포함)
    ontology_resolved: str | None = None  # 실제로 쓰는 적재 버전
    llm_keys: dict[str, bool]  # 설정 여부만. 값은 노출하지 않는다.
