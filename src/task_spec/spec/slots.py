"""확장 슬롯 — 공통 과제 위에서 이어지는 "추가로 구현·분석할 부분" [확정: 2026-11-03 사용자].

공고당 한 번 만들어 공통 과제와 함께 저장·승인한다 (spec/store.py). 그 공고 지원자 모두 같은 슬롯 본문을 본다.
지원자마다 다른 것은 어떤 슬롯을 배정받는지와 면접 질문뿐 (PR-B). 지원자 문서는 읽지 않는다 (불변식 3).

슬롯 대상 고르기 (결정적): 공고 역량 중 수행준거가 있는 셀 범위 NCS 단위가 있는 것.
  공통 과제가 다루지 않은 역량 먼저 → 그다음 공통 과제 역량 (다른 각도). 각 묶음 안은 공통 과제 골격과 같은 정렬. 최대 MAX_SLOTS.
생성: 슬롯마다 draft_slot (공통 과제와 같은 생성·검증·재요청 루프 — spec/draft.py draft_task), 동시에.
  추가 검사: 공통 과제의 핵심 원인 개념이 슬롯의 보이는 부분에 나오면 안 된다 (공통 과제 정답 노출).
  실패한 슬롯은 빼고 공통 과제 review_flags 에 남긴다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field

from task_spec.llm import LLMClient
from task_spec.ontology.queries import criteria_detail
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry
from task_spec.schemas import (
    CompetencyEvidence,
    DraftTaskOutput,
    NodeError,
    PostingCompetency,
    PostingProfile,
    Provenance,
    TargetCompetency,
    TaskItem,
)
from task_spec.spec.common import _key
from task_spec.spec.draft import DraftContext, _norm, _phrase, apply, draft_task
from task_spec.spec.verify import item_difficulty

logger = logging.getLogger(__name__)

TASK = "draft_slot"
MAX_SLOTS = 4
SLOT_TIME_MIN = 30
SLOT_MAX_CRITERIA = 8
SLOT_KSA = 3
FLAG_SLOT_FAILED = "확장 슬롯 생성 실패 — 이 역량은 슬롯 없이 면접 질문만: "


def plan_slots(
    profile: PostingProfile,
    evidence: list[CompetencyEvidence],
    common_ids: set[str],
    max_slots: int = MAX_SLOTS,
) -> list[tuple[PostingCompetency, list[str]]]:
    """(역량, 근거 능력단위) — 공통 과제가 다루지 않은 역량 먼저. 수행준거가 있는 단위만 근거로 친다."""
    units = {
        e.competency_id: [u.unit_cl_cd for u in e.ncs_units if u.n_criteria > 0] for e in evidence
    }
    pool = [c for c in profile.competencies if units.get(c.competency_id)]
    outside = sorted((c for c in pool if c.competency_id not in common_ids), key=_key)
    inside = sorted((c for c in pool if c.competency_id in common_ids), key=_key)
    return [(c, units[c.competency_id]) for c in [*outside, *inside][:max_slots]]


def common_visible(common: TaskItem) -> str:
    """지원자가 이미 본 공통 과제 — 제목·상황·자료 목록(이름만). 자료 본문·함정·정답은 넣지 않는다."""
    lines = [common.title or "", common.scenario or ""]
    if common.artifacts:
        lines.append(
            "자료 목록: " + ", ".join(f"{a.artifact_id} {a.title}" for a in common.artifacts)
        )
    return "\n".join(x for x in lines if x).strip() or "(공통 과제 서술 없음)"


def common_secrets(common: TaskItem, ctx: DraftContext) -> list[str]:
    """슬롯 본문에 나오면 공통 과제 정답이 새는 말: 공통 과제의 핵심 개념 + 원 사례 원인 용어."""
    out = [o.value for o in common.observables if o.kind == "first_mention_of"]
    if ctx.incident is not None:
        out += ctx.incident.hidden_terms
    return [x for x in dict.fromkeys(out) if len(_norm(x)) >= 2]


def leak_check(secrets: list[str]) -> object:
    def check(out: DraftTaskOutput) -> list[str]:
        arts = list(getattr(out, "artifacts", []) or [])
        visible = "\n".join([out.title, out.scenario, *out.deliverables, *(a.title for a in arts)])
        nv = _norm(visible)
        shown = [w for w in secrets if _norm(w) in nv or _phrase(w).search(visible)]
        return (
            [
                f"공통 과제의 핵심 개념이 슬롯 본문에 드러남 (공통 과제 정답 노출): {', '.join(shown)}"
            ]
            if shown
            else []
        )

    return check


@dataclass
class SlotsResult:
    slots: list[TaskItem] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)
    errors: list[NodeError] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # 공통 과제 review_flags 에 붙일 것


async def build_slots(
    profile: PostingProfile,
    evidence: list[CompetencyEvidence],
    common: TaskItem,
    base_ctx: DraftContext,
    *,
    ontology: OntologyRepository,
    llm: LLMClient,
    prompts: PromptRegistry,
    reviewer_note: str | None = None,
    max_slots: int = MAX_SLOTS,
) -> SlotsResult:
    """공통 과제 하나에 붙는 슬롯들. 근거 조회는 차례로, LLM 생성은 동시에."""
    common_ids = {t.competency_id for t in common.target_competencies}
    picked = plan_slots(profile, evidence, common_ids, max_slots)
    secrets = common_secrets(common, base_ctx)
    extra = {"common_task": common_visible(common), "forbidden": ", ".join(secrets) or "(없음)"}
    esco = {e.competency_id: (e.esco[0].uri if e.esco else None) for e in evidence}

    prepared: list[tuple[TaskItem, DraftContext]] = []
    for i, (comp, units) in enumerate(picked, 1):
        crit, ksa = await criteria_detail(ontology, units, max_criteria=SLOT_MAX_CRITERIA)
        if not crit:
            continue
        level = (
            max(comp.levels, key=lambda x: {"수행": 3, "활용": 2, "이해": 1}.get(x, 0))
            if comp.levels
            else None
        )
        target = TargetCompetency(
            competency_id=comp.competency_id,
            label=comp.label,
            level=level,
            esco_uri=esco.get(comp.competency_id),
            ncs_unit_ids=units,
            ncs_criterion_ids=[c.criterion_id for c in crit],
        )
        diff = item_difficulty(comp.levels)
        base = TaskItem(
            task_id=f"s{i}",
            track="extension_slot",
            target_competencies=[target],
            tech_env=common.tech_env,
            difficulty_target=diff,  # type: ignore[arg-type]
            time_limit_min=SLOT_TIME_MIN,
            parent_task_id=common.task_id,
        )
        ctx = dataclasses.replace(
            base_ctx,
            targets=[target],
            criteria=crit,
            ksa=ksa[:SLOT_KSA],
            difficulty=diff,
            time_limit_min=SLOT_TIME_MIN,
        )
        prepared.append((base, ctx))

    results = await asyncio.gather(
        *(
            draft_task(
                ctx,
                llm=llm,
                prompts=prompts,
                reviewer_note=reviewer_note,
                task=TASK,
                extra_input=extra,
                extra_check=leak_check(secrets),  # type: ignore[arg-type]
            )
            for _, ctx in prepared
        )
    )
    out = SlotsResult()
    for (base, _), res in zip(prepared, results, strict=True):
        out.provenance += res.provenance
        out.errors += res.errors
        if res.output is None:
            out.flags.append(FLAG_SLOT_FAILED + base.target_competencies[0].label)
            continue
        # 슬롯에는 원 사례 표시(공개 사례 기반)를 공통 과제에서 이미 하므로 슬롯 자체 review_flags 는 생성 결과만
        slot = apply(base, dataclasses.replace(res, incident_id=None))
        out.slots.append(slot)
    logger.info(
        "slots built planned=%d made=%d failed=%d", len(prepared), len(out.slots), len(out.flags)
    )
    return out
