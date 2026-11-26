"""채용담당자 인터뷰 과제 승인 (#266 D2b, 결정 2 [확정: 2026-11-19 사용자]).

승인은 `graph/interview_approval.py` 의 interrupt 로만 받는다 (불변식 9). 이 모듈은
- 승인 전 검사(읽기만, 포크 approve 검사 + 회사 사례 등록 검사)와
- 승인 확정을 **한 트랜잭션**으로 묶는 저장 계층이다:
  인터뷰 판본 확인·단계 approved·승인 기록 → 사례를 co: 로 등록(pending — 공유 승인은 /incidents 그래프가 따로)
  → 공통 과제 승인(이미 승인된 인터뷰 과제가 있으면 replaces 로 교체 명시, 결정 1) → 같은 인터뷰의 다른 초안 판 삭제.
  하나라도 실패하면 아무것도 바뀌지 않는다.
대응표 삭제(결정 4)는 커밋 뒤 서비스가 인터뷰 잠금 안에서 한다 (동시 수정이 지운 대응표를 되살리지 않게).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from task_spec.interview.answers import validation_current
from task_spec.interview.incident import incident_digest
from task_spec.interview.store import MemoryInterviewStore, save_in
from task_spec.interview.task import task_kind_for
from task_spec.nodes.pseudonymize.detectors import detect_patterns
from task_spec.schemas import PublicIncident
from task_spec.schemas.interview_state import InterviewState
from task_spec.spec.company_incidents import (
    Detect,
    Finding,
    InMemoryCompanyIncidents,
    insert_incident,
    scan_incident,
)
from task_spec.spec.store import (
    INTERVIEW_REF_PREFIX,
    InMemoryCommonStore,
    StoredCommon,
    approve_interview_task,
    drop_interview_drafts,
)


def interview_ref(interview_id: str) -> str:
    return f"{INTERVIEW_REF_PREFIX}{interview_id}"


def pattern_detect(text: str) -> list[str]:
    """연락처·링크 같은 패턴만 본다 (이름 탐지기 없이) — 회사 사례 등록을 막는 범주(CONTACT·LINK)는 패턴 탐지기로 충분하고,
    LLM 이 쓴 기능형 재료에 이름 탐지기를 쓰면 오탐으로 막힌다."""
    return [s.category for s in detect_patterns(text)] if text.strip() else []


def blocked_findings(incident: PublicIncident, detect: Detect = pattern_detect) -> list[Finding]:
    """회사 사례 등록을 막는 내용 — LLM 입력으로 가는 칸의 연락처·링크 (`/incidents` 등록과 같은 규칙)."""
    return [f for f in scan_incident(incident, detect) if f.blocks]


def approval_problems(
    state: InterviewState,
    stored: StoredCommon | None,
    company_id: str | None,
    blocked: list[Finding],
    source_problem: str | None = None,
) -> list[str]:
    """승인할 수 없는 사유 (비면 승인 가능). 포크 approve 의 검사 + 회사 사례 등록 검사. 사유는 고정 문구·칸 이름만.
    인터뷰 사례(known_problem)만 기준 답안 검증·회사 사례 등록 검사를 본다 — 공개·회사 사례는 고른 사례가 아직 쓸 수 있는지(source_problem)."""
    spec = state.spec
    if spec is None:
        return ["과제를 먼저 생성해 주세요"]
    problems: list[str] = []
    task = spec.common_task
    if (state.task_kind or task_kind_for(state.posting_context.sub)) == "feature":
        if (
            task.task_kind != "feature"
            or not spec.template_id
            or spec.coding_incident.feature is None
            or task.source_incident != spec.coding_incident.incident_id
        ):
            problems.append(
                "기능형 과제·템플릿·기능형 재료가 맞지 않습니다. 과제를 다시 생성해 주세요"
            )
    elif (
        task.task_kind != "incident"
        or spec.template_id is not None
        or task.source_incident != spec.coding_incident.incident_id
    ):  # 사례형 (명세가 곧 과제) — 템플릿·기능형 재료 없이 확인한 사례 그대로
        problems.append("사례형 과제·재료가 맞지 않습니다. 과제를 다시 생성해 주세요")
    if stored is None or stored.id != task.task_id or stored.status != "draft":
        problems.append("공통 과제 저장본이 지금 과제와 다릅니다. 과제를 다시 생성해 주세요")
    interview = state.incident_source == "known_problem"
    incident = state.incident_confirmed
    if (
        incident is None
        or state.incident_confirmation is None
        or state.incident_confirmation.sha256 != incident_digest(incident)
        or spec.incident != incident
        or spec.incident_source != state.incident_source
        or spec.incident_confirmation != state.incident_confirmation
        or (
            interview
            and (
                spec.answer_validation != state.answer_validation
                or not validation_current(incident, state)
            )
        )
    ):
        problems.append("확인한 사례·기준 답안으로 과제를 다시 생성해 주세요")
    if not all(q.answer for q in spec.targeted_questions):
        problems.append("짚은 질문의 답을 모두 정해 주세요")
    if (
        not spec.general_questions
        or not spec.targeted_questions
        or not spec.hidden_requirements
        or not spec.evaluation_criteria
    ):
        problems.append("자료 질문·숨은 요구·평가 기준을 모두 확인해야 합니다")
    if interview and company_id is None:
        problems.append(
            "공고에 회사가 없어 사례를 회사 사례로 등록할 수 없습니다. 공고의 회사를 입력해 주세요"
        )
    if interview:
        problems.extend(f"회사 사례로 등록할 수 없는 내용 — {f.text()}" for f in blocked)
    if source_problem:
        problems.append(source_problem)
    return problems


@dataclass(frozen=True)
class ApprovalCommit:
    """승인 확정 한 건 — state 는 승인 기록을 담은 새 인터뷰 상태 (단계 approved)."""

    interview_id: str
    revision: int
    state: InterviewState
    company_id: str | None
    incident: (
        PublicIncident | None
    )  # 새로 등록할 인터뷰 사례 (co:…) — 공개·회사 사례에서 골랐으면 None
    common_id: str
    reviewer: str
    at: datetime
    replaces: str | None

    @property
    def source(self) -> dict[str, Any]:
        return {"kind": "interview", "interview_id": self.interview_id, "revision": self.revision}


@dataclass(frozen=True)
class ApprovalOutcome:
    result: str | None = None  # approved · stale (None = 다시 기다림)
    error: str | None = None  # 다시 기다릴 때 승인자에게 보일 사유
    common_id: str | None = None


class ApprovalCommitter(Protocol):
    async def commit(self, c: ApprovalCommit) -> bool:
        """한 트랜잭션으로 확정한다. 인터뷰 판본이 바뀌었으면 False (아무것도 바뀌지 않음).
        교체 충돌은 InterviewTaskConflict, 사례 id 중복·과제 규칙 위반은 ValueError·KeyError — 모두 아무것도 바뀌지 않는다."""
        ...


class _Stale(Exception):
    pass


class PgApprovalCommitter:
    """같은 DB 의 세 표(recruiter_interview·company_incident·common_task)를 한 연결·트랜잭션으로."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def commit(self, c: ApprovalCommit) -> bool:
        try:
            async with self._pool.connection() as conn, conn.transaction():
                if not await save_in(conn, c.interview_id, c.revision, c.state):
                    raise _Stale
                if c.incident is not None:
                    assert c.company_id is not None
                    await insert_incident(
                        conn, c.company_id, c.incident, source=c.source, created_by=c.reviewer
                    )
                await approve_interview_task(
                    conn, c.common_id, c.reviewer, c.at, replaces=c.replaces
                )
                await drop_interview_drafts(conn, interview_ref(c.interview_id), keep=c.common_id)
        except _Stale:
            return False
        return True


class MemoryApprovalCommitter:
    """테스트·단독 실행용 — 실패하면 세 저장소를 처음 상태로 되돌린다 (PgApprovalCommitter 의 트랜잭션과 같은 결과)."""

    def __init__(
        self,
        interviews: MemoryInterviewStore,
        common: InMemoryCommonStore,
        incidents: InMemoryCompanyIncidents,
    ) -> None:
        self.interviews, self.common, self.incidents = interviews, common, incidents

    async def commit(self, c: ApprovalCommit) -> bool:
        row = self.interviews.rows.get(c.interview_id)
        if row is None or row.revision != c.revision:
            return False
        saved = (copy.deepcopy(self.common.rows), dict(self.incidents.rows), row)
        try:
            if c.incident is not None:
                assert c.company_id is not None
                await self.incidents.submit(
                    c.company_id, c.incident, source=c.source, created_by=c.reviewer
                )
            await self.common.approve_interview(c.common_id, c.reviewer, c.at, replaces=c.replaces)
            await self.common.drop_interview_drafts(interview_ref(c.interview_id), keep=c.common_id)
            if (
                await self.interviews.save(
                    c.interview_id, c.revision, c.state, clear_all_drafts=True
                )
                is None
            ):
                raise _Stale
        except _Stale:
            self._restore(c.interview_id, saved)
            return False
        except BaseException:
            self._restore(c.interview_id, saved)
            raise
        return True

    def _restore(self, interview_id: str, saved: tuple[Any, ...]) -> None:
        self.common.rows, self.incidents.rows = saved[0], saved[1]
        self.interviews.rows[interview_id] = saved[2]
