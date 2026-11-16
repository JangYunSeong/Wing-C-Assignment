"""평가 키트 — 승인된 명세에서 단계별 진단 기준(관찰 항목·자료 역할·질문이 단서가 되는 함정)을 뽑고(`kit_stages`),
같은 빌드의 응시용·채점용 descriptor 와 묶어 `EvalKit` 을 만든다(`build_kit`).

진행자 보기(facilitator.py)는 지원자에게 줄 칸만 고르고 역할·함정을 빼며, 이 모듈은 그 반대로 평가 쪽에 필요한 숨은 칸만 고른다
(자료 본문·답 문장은 넣지 않는다 — 진행자 쪽에 있다). 2026-11-12 사용자 결정 A: 2단계는 1단계와 같은 함정의 증상으로 진단 과정을 본다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from task_spec.schemas.domain import Artifact, TaskItem
from task_spec.schemas.package import (
    CandidateDescriptor,
    EvalKit,
    GraderDescriptor,
    KitAcceptance,
    KitCase,
    KitClarification,
    KitFact,
    KitImages,
    KitMaterial,
    KitPublicAcceptance,
    KitRequirement,
    KitStage,
    ReviewDecision,
    VerifySummary,
)
from task_spec.starter.artifacts import merged_flags


def _kit_materials(arts: list[Artifact]) -> list[KitMaterial]:
    """자료의 역할만 (본문 없음) — 진행자 보기의 facilitator._materials 는 반대로 본문만."""
    return [KitMaterial(artifact_id=a.artifact_id, title=a.title, role=a.role) for a in arts]


def _analysis_fields(task: TaskItem) -> dict[str, Any]:
    """문제 분석 판(analysis-stage.md §6·§8)의 채점 연결 — 명세에 사실이 있을 때만. 사실의 답 문장·매칭 낱말은 넣지 않는다(진행자 쪽)."""
    if not task.facts:
        return {}
    return {
        "request_mentions": list(task.request_mentions),
        "requirements": [KitRequirement(req_id=r.req_id, text=r.text) for r in task.requirements],
        "facts": [
            KitFact(fact_id=f.fact_id, covers=list(f.covers), topic=f.topic) for f in task.facts
        ],
        "acceptance_public": [
            KitPublicAcceptance(ac_id=a.ac_id, req_ids=list(a.req_ids))
            for a in task.acceptance_public
        ],
    }


def kit_stages(task: TaskItem) -> tuple[KitStage | None, KitStage | None]:
    """(1단계, 2단계). 1단계는 평가할 것(함정·관찰 항목·자료·질문·사실)이 하나도 없을 때만 None, 2단계는 명세에 debug_stage 가 없을 때 None."""
    s1 = KitStage(
        trap_ids=[t.trap_id for t in task.traps],
        observables=list(task.observables),
        materials=_kit_materials(task.artifacts),
        max_requests=task.max_requests,
        clarifications=[
            KitClarification(
                clarification_id=c.clarification_id, topic=c.topic, reveals=list(c.reveals)
            )
            for c in task.clarifications
        ],
        max_clarifications=task.max_clarifications,
        **_analysis_fields(task),
    )
    stage1 = (
        s1
        if (s1.trap_ids or s1.observables or s1.materials or s1.clarifications or s1.facts)
        else None
    )
    d = task.debug_stage
    stage2 = (
        None
        if d is None
        else KitStage(
            trap_ids=list(d.trap_ids),
            observables=list(d.observables),
            materials=_kit_materials(d.artifacts),
            max_requests=d.max_requests,
        )
    )
    return stage1, stage2


def build_kit(
    task: TaskItem,
    cand: CandidateDescriptor,
    grad: GraderDescriptor,
    cases: Sequence[Mapping[str, Any]],
    verification: dict[str, VerifySummary],
    reasons: Mapping[str, str],
    build_flags: Sequence[str],
    review_decisions: Sequence[ReviewDecision] = (),
) -> EvalKit:
    """평가 키트 — 스키마 모델로 만들어 검증한다 (빠진 칸·스키마 밖 칸이 있으면 쓰기 전에 멈춤).

    cases: 채점 설정(config.json)의 케이스 — 숨은 케이스(kind=hidden)만 키트에 넣는다.
    verification: 제출물마다 검증 결과. reasons: 템플릿 실패 사유 코드 → 설명. build_flags: 빌드 단계의 검토 표시.
    2026-11-12 eval/starter/split_delivery.py 에서 옮김 (#126) — 승인 뒤 키트 등록(#130)이 같은 코드를 쓴다."""
    stage1, stage2 = kit_stages(task)
    traps = {x.trap_id: x for x in task.traps}
    header = cand.model_dump(
        include={
            "mock",
            "approval",
            "note",
            "common_id",
            "revision",
            "build_id",
            "template_ref",
            "template_sha256",
            "ontology_version",
            "tree_sha256",
            "platform",
            "title",
        }
    )
    hidden = [x for x in cases if x.get("kind") == "hidden"]
    return EvalKit(
        **header,
        images=KitImages(candidate=cand.image.image_id, grader=grad.image.image_id),
        grader_sha256=grad.grader_sha256,
        cases=[
            KitCase(
                id=x["id"],
                trap_id=x.get("trap_id") or "",
                intended_reason=x.get("intended_reason") or "",
                reason_description=reasons.get(x.get("intended_reason") or "", ""),
                hidden_requirement=(
                    traps[x["trap_id"]].description if x.get("trap_id") in traps else ""
                ),
            )
            for x in hidden
        ],
        # 숨김 수용 기준은 스키마 칸만 — 실패 지정(items·faults)은 채점용 이미지 안에서 처리된다
        acceptance_hidden=[
            KitAcceptance(ac_id=a.ac_id, trap_id=a.trap_id, text=a.text)
            for a in task.acceptance_hidden
        ],
        accepted_designs=list(task.accepted_designs),
        behavioral_anchors=task.behavioral_anchors,
        follow_up_questions=list(task.follow_up_questions),
        stage1=stage1,
        stage2=stage2,
        target_competencies=list(task.target_competencies),
        verification=verification,
        verification_note=(
            "starter-only = 시작 코드 그대로, naive-all = 흔한 구현, d* = 허용 설계별 참조 구현 (참조 구현 자체는 넣지 않음)"
        ),
        review_flags=merged_flags(task.review_flags, build_flags),
        review_decisions=list(
            review_decisions
        ),  # 승인된(목 아닌) 키트는 표시마다 판단이 있어야 한다
    )
