"""승인 요약 (ApprovalSummary) — 승인 interrupt 와 API 실행 조회가 같이 쓴다.

노드(approval)는 승인 대기 때 이 요약을 interrupt 에 싣고, API(run_view)는 승인이 끝난 실행(done·rejected)도
저장된 State 에서 같은 요약을 계산해 돌려준다 — 확정된 실행을 다시 열어도 대조·배정 근거를 볼 수 있게 (2026-11-04).
"""

from __future__ import annotations

from task_spec.schemas import (
    ApprovalSummary,
    AssignmentBrief,
    Comparison,
    ComparisonBrief,
    CompetencyMatch,
    OutsideBrief,
    PostingCheckBrief,
    RunState,
    TaskBrief,
    TaskItem,
    VerifyBrief,
)


def approval_summary(state: RunState) -> ApprovalSummary:
    """승인자가 먼저 볼 것. 원문·가명화 매핑·주장 인용은 넣지 않는다 (인증 전 — privacy-auditor W1)."""
    spec = state.spec
    assert spec is not None

    def brief(t: TaskItem) -> TaskBrief:
        return TaskBrief(
            task_id=t.task_id,
            track=t.track,
            title=t.title,
            competencies=[c.label for c in t.target_competencies],
            difficulty=t.difficulty_target,
            time_limit_min=t.time_limit_min,
            n_traps=len(t.traps),
            has_anchors=t.behavioral_anchors is not None,
            review_flags=t.review_flags,
        )

    tasks = [brief(t) for t in [*spec.common_tasks, *spec.verification_tasks]]
    slots = [brief(x) for t in spec.common_tasks for x in t.slots]
    c = state.comparison
    comparison = (
        ComparisonBrief(
            overlap=[m.label for m in c.overlap],
            related=[m.label for m in c.related],
            posting_only=[m.label for m in c.posting_only],
            applicant_only=[
                OutsideBrief(label=m.label, relatedness=m.relatedness) for m in c.applicant_only
            ],
            uncovered_claims=len(c.uncovered),
            claims_to_verify=[
                VerifyBrief(claim_id=v.claim_id, score=v.score, reasons=v.reasons)
                for v in c.claims_to_verify
            ],
            check_status=c.check_status,
            posting_checks=posting_checks(state, c),
        )
        if c is not None
        else None
    )
    return ApprovalSummary(
        review_flags=spec.review_flags,
        tasks=tasks,
        slots=slots,
        assignments=[
            AssignmentBrief(
                claim_id=a.claim_id,
                slot_id=a.slot_id,
                basis=a.basis,
                n_questions=len(a.follow_up_questions),
                review_flags=a.review_flags,
            )
            for a in spec.slot_assignments
        ],
        common=spec.common_refs,
        total_time_min=sum(t.time_limit_min for t in tasks),
        comparison=comparison,
    )


def posting_checks(state: RunState, c: Comparison) -> list[PostingCheckBrief]:
    """공고 역량마다 대조 결과 한 줄 (공고 프로필 순서). 근거는 주장 id 만."""

    def brief(m: CompetencyMatch, status: str) -> PostingCheckBrief:
        return PostingCheckBrief(
            competency_id=m.competency_id,
            label=m.label,
            primary=m.primary,
            status=status,  # type: ignore[arg-type]
            source=m.overlap_source,
            check_label=m.check_label,
            claim_ids=list(m.check_claim_ids or m.claim_ids),
            role=m.role,
            downgraded=m.downgraded,
            select_only=m.select_only,
            promoted=m.promoted,
        )

    rows = {
        m.competency_id: brief(m, status)
        for status, ms in (
            ("overlap", c.overlap),
            ("related", c.related),
            ("posting_only", c.posting_only),
        )
        for m in ms
    }
    order = state.posting_profile.competency_ids if state.posting_profile else []
    return [rows[k] for k in order if k in rows] + [r for k, r in rows.items() if k not in order]
