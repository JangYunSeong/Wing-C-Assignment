"""검증할 주장 → 확장 슬롯 배정 (결정적, LLM 없음) [확정: 2026-11-03 사용자].

순서: compare 의 검증 후보 중 pick_targets 로 고른 주장(점수 순, 최대 2) 마다
  1. 승인자 지정(reassign)이 있으면 그것 (슬롯 id 또는 None)
  2. 주장의 역량과 같은 역량의 슬롯
  3. 온톨로지 근접·기반 관계(Relatedness.pairs)로 이어진 역량의 슬롯
  4. 없으면 슬롯 없음 → 면접 질문만
한 슬롯은 한 주장에만 (먼저 고른 = 점수 높은 주장이 가져간다). 근거(basis)를 남긴다 — 승인자가 따라갈 수 있게.
"""

from __future__ import annotations

from task_spec.ontology.relatedness import Relatedness
from task_spec.schemas import SlotAssignment, TaskItem, VerifyTarget

BASIS_REVIEWER = "승인자 지정"
BASIS_NONE = "맞는 슬롯 없음"


def assign_slots(
    targets: list[VerifyTarget],
    slots: list[TaskItem],
    rel: Relatedness | None = None,
    reassign: dict[str, str | None] | None = None,
) -> list[SlotAssignment]:
    reassign = reassign or {}
    slot_ids = {s.task_id for s in slots}
    comp_of = {
        s.task_id: s.target_competencies[0].competency_id for s in slots if s.target_competencies
    }
    used: set[str] = set()
    out: list[SlotAssignment] = []
    for t in targets:
        sid: str | None = None
        basis = BASIS_NONE
        if t.claim_id in reassign:
            want = reassign[t.claim_id]
            sid = want if want in slot_ids else None
            basis = (
                BASIS_REVIEWER
                if want is None or want in slot_ids
                else f"{BASIS_REVIEWER} (없는 슬롯 {want} — 무시)"
            )
        else:
            same = [s for s, c in comp_of.items() if c in t.competency_ids and s not in used]
            if same:
                sid = same[0]
                basis = f"역량 같음: {comp_of[sid]}"
            elif rel is not None:
                for comp in t.competency_ids:
                    near = sorted(rel.pairs.get(comp, set()))
                    hit = [s for s, c in comp_of.items() if c in near and s not in used]
                    if hit:
                        sid = hit[0]
                        basis = f"근접·기반 관계: {comp} ↔ {comp_of[sid]}"
                        break
        if sid is not None:
            used.add(sid)
        out.append(
            SlotAssignment(
                claim_id=t.claim_id, competency_ids=list(t.competency_ids), slot_id=sid, basis=basis
            )
        )
    return out
