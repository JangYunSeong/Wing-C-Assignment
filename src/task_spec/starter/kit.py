"""평가 키트의 과정 평가 기준 — 승인된 명세에서 단계별 진단 기준(관찰 항목·자료 역할·질문이 단서가 되는 함정)을 뽑는다.

진행자 보기(facilitator.py)는 지원자에게 줄 칸만 고르고 역할·함정을 빼며, 이 모듈은 그 반대로 평가 쪽에 필요한 숨은 칸만 고른다
(자료 본문·답 문장은 넣지 않는다 — 진행자 쪽에 있다). 2026-11-12 사용자 결정 A: 2단계는 1단계와 같은 함정의 증상으로 진단 과정을 본다.
"""

from __future__ import annotations

from task_spec.schemas.domain import Artifact, TaskItem
from task_spec.schemas.package import KitClarification, KitMaterial, KitStage


def _kit_materials(arts: list[Artifact]) -> list[KitMaterial]:
    """자료의 역할만 (본문 없음) — 진행자 보기의 facilitator._materials 는 반대로 본문만."""
    return [KitMaterial(artifact_id=a.artifact_id, title=a.title, role=a.role) for a in arts]


def kit_stages(task: TaskItem) -> tuple[KitStage | None, KitStage | None]:
    """(1단계, 2단계). 1단계는 평가할 것(함정·관찰 항목·자료·질문)이 하나도 없을 때만 None, 2단계는 명세에 debug_stage 가 없을 때 None."""
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
    )
    stage1 = s1 if (s1.trap_ids or s1.observables or s1.materials or s1.clarifications) else None
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
