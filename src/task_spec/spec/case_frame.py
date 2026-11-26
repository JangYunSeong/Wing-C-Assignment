"""공유 회사 사례의 함정 틀 — 일반 파이프라인(지원자 실행)이 회사 사례(co:) 기능형 재료로 공통 과제를 만들 때도 인터뷰처럼 재료 분석·
함정 틀을 거친다 (설계 검사 #332 S-F8, 사용자 결정 2026-11-26 — #318 D1 '회사 사례를 고른 공고도 틀이 빌드에 닿는다').

틀을 만드는 쪽은 인터뷰 서비스다 (가명화 길이 인터뷰의 공유 재료와 같아야 한다 — InterviewService.frame_shared_case). 명세 노드는 이
타입만 안다 (nodes/spec.make_draft_spec(case_framer=)).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from task_spec.schemas import FramedMaterial, PostingProfile, Provenance, PublicIncident, TrapFrame


@dataclass(frozen=True)
class CaseFrame:
    """틀을 거친 사례 — incident 는 틀 적용 재료로 기능형 칸을 바꾼 사본(틀이 없거나 실패하면 원 재료 그대로)."""

    incident: PublicIncident
    frame: TrapFrame | None = None
    framed: FramedMaterial | None = None  # 공통 과제 행에 둔다 — 빌드 계약 단계가 원 재료 대신 쓴다
    flags: list[str] = field(default_factory=list)  # 재료 분석·틀 표시 (막지 않음)
    provenance: list[Provenance] = field(default_factory=list)


CaseFramer = Callable[[PublicIncident, PostingProfile], Awaitable[CaseFrame]]

# 틀 만들기가 예상 밖으로 실패했을 때 (막지 않음 — 원 재료로 만든다, 개인정보 점검 M1)
FLAG_CASE_FRAME_FAILED = "함정 틀: 공유 회사 사례의 틀을 만들지 못함 — 원 재료로 만듦"
