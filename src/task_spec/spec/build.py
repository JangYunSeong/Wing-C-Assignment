"""공통 과제 골격 + 서술 입력 만들기 (노드와 평가 스크립트가 같이 쓴다). 공고·온톨로지만 읽는다 (불변식 3)."""

from __future__ import annotations

from dataclasses import dataclass, field

from task_spec.ontology.queries import criteria_detail, field_labels, language_labels
from task_spec.ontology.relatedness import Relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.schemas import (
    CompetencyEvidence,
    MaterialInterface,
    NcsCriterion,
    NcsKsa,
    PostingProfile,
    PublicIncident,
    TaskItem,
    TaskKind,
)
from task_spec.spec.common import (
    FLAG_NO_INCIDENT,
    TIME_LIMIT_MIN,
    common_tech_env,
    difficulty,
    plan_common,
    targets,
)
from task_spec.spec.draft import DraftContext
from task_spec.spec.incidents import feature_incident, interface_for

KSA_PER_COMP = 3


@dataclass
class CommonDraft:
    task: TaskItem | None = None  # 골격 (역량을 못 고르면 None)
    ctx: DraftContext | None = None  # 서술 입력 (온톨로지·수행준거가 없으면 None)
    flags: list[str] = field(default_factory=list)  # 명세 단위 review_flags


def material_for(
    incidents: dict[str, PublicIncident] | None, sub: str, posting_id: str, task_kind: TaskKind
) -> tuple[PublicIncident | None, MaterialInterface | None]:
    """명세 맥락의 사례와 재료 인터페이스. 기능형은 직군 템플릿이 시험할 수 있는 사례(spec/incidents.feature_incident)와 그 재료가
    정한 공개 인터페이스(#256 — 명세가 계약과 같은 형식·칸 이름을 쓰게), 그 밖은 직군 기본 사례만."""
    if task_kind == "feature":
        incident = feature_incident(incidents, sub, posting_id)
        return incident, interface_for(incidents, incident)
    return (incidents or {}).get(sub), None


async def build_common(
    profile: PostingProfile,
    evidence: list[CompetencyEvidence],
    *,
    task_id: str,
    ontology_version: str,
    ontology: OntologyRepository | None = None,
    rel: Relatedness | None = None,
    incidents: dict[str, PublicIncident] | None = None,
    task_kind: TaskKind = "incident",
) -> CommonDraft:
    """task_kind 는 부르는 쪽이 정한다 (spec/kind.py 규칙 · 승인된 판 · 승인자 지정 — nodes/spec.py)."""
    plan = plan_common(profile, evidence, rel)
    out = CommonDraft(flags=list(plan.flags))
    if not plan.competencies:
        return out
    tcs = targets(plan, evidence)
    crit_all: list[NcsCriterion] = []
    ksa_all: list[NcsKsa] = []
    if ontology is not None:
        filled = []
        for t in tcs:
            crit, ksa = await criteria_detail(ontology, t.ncs_unit_ids)
            filled.append(
                t.model_copy(update={"ncs_criterion_ids": [c.criterion_id for c in crit]})
            )
            crit_all += [c for c in crit if c not in crit_all]
            ksa_all += ksa[:KSA_PER_COMP]
        tcs = filled
    env, env_flags = common_tech_env(profile)
    out.task = TaskItem(
        task_id=task_id,
        track="common",
        task_kind=task_kind,
        target_competencies=tcs,
        tech_env=env,
        difficulty_target=difficulty(plan),  # type: ignore[arg-type]
        time_limit_min=TIME_LIMIT_MIN,
        review_flags=[FLAG_NO_INCIDENT, *env_flags],
    )
    if ontology is not None and crit_all:
        sub_label, ind_label = await field_labels(ontology, profile.sub, profile.industry_id)
        incident, interface = material_for(
            incidents, profile.sub or "", profile.posting_id, task_kind
        )
        out.ctx = DraftContext(
            sub_label=sub_label,
            industry_label=ind_label,
            targets=tcs,
            criteria=crit_all,
            ksa=ksa_all,
            env=env,
            difficulty=out.task.difficulty_target,
            time_limit_min=out.task.time_limit_min,
            ontology_version=ontology_version,
            language_labels=await language_labels(ontology),
            sub=profile.sub,
            incident=incident,
            interface=interface,
        )
    return out
