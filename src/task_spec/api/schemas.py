"""API 요청/응답 모델. 원문·가명화 매핑은 담지 않는다."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from task_spec.schemas import ApprovalSummary, CareerLevel, CareerSource, NodeError, TaskSpec

# interrupted = 체크포인트가 중간인데 이 프로세스에서 돌지 않음 (재시작·배포로 끊김, #280 E) — POST /runs/{id}/resume 로 잇는다
RunStatus = Literal["running", "awaiting_approval", "interrupted", "done", "rejected", "failed"]
# 이어서 하기 전까지 바뀌지 않는 상태 — SSE 를 끝낸다 (interrupted·failed 도 이어서 하면 다시 running)
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "rejected", "failed", "interrupted"})


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
    common_id: str | None = (
        None  # 실행 만들 때 담당자가 고른 문제 (POST /runs 의 common_id) — 없으면 공고 기본 과제
    )
    revision: int | None = None
    spec: TaskSpec | None = None  # awaiting_approval 일 때만 (승인 대기 초안)
    approval_error: str | None = None  # 직전 결정이 거부된 사유 (interrupt payload.error)
    # 승인자가 먼저 볼 요약 (spec/summary.py approval_summary). 승인 대기 = interrupt payload.summary,
    # done·rejected = 저장된 State 에서 다시 계산 (확정 뒤에도 대조·배정을 보게). 주장 인용 없음.
    approval_summary: ApprovalSummary | None = None
    errors: list[NodeError] = []
    # failed 일 때 멈춘 노드의 오류 종류 이름 (예: CommonTaskMissing) — 메시지는 싣지 않는다 (입력 조각이 섞일 수 있다).
    # 멈춘 노드는 current_node (이어서 하면 거기부터)
    failure: str | None = None


class RunListItem(BaseModel):
    """실행 목록 한 줄 (GET /runs) — 상태·공고·회사·시각만. 명세 초안·승인 요약은 GET /runs/{id} 로."""

    run_id: str
    status: RunStatus
    current_node: str | None = None
    revision: int | None = None
    posting_id: str | None = None
    # 회사 (postings/company.py) — id 는 표기에서 공백·법인 표기를 뺀 값이라 온톨로지에 없는 새 회사도 생긴다.
    # 표시 이름은 온톨로지 회사면 그 표기. company_known = 온톨로지 company 표에 있는 회사. 공고·회사를 모르면 셋 다 null
    company_id: str | None = None
    company: str | None = None
    company_known: bool | None = None
    # 신입·경력 (postings/career.py) — 사람이 정한 값(confirmed)이 먼저, 없으면 공고 문구 추정(posting_text), 둘 다 없으면 null
    career_level: CareerLevel | None = None
    career_level_source: CareerSource | None = None
    started_at: datetime | None = None  # thread 의 첫 체크포인트
    updated_at: datetime | None = None  # 마지막 체크포인트


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
