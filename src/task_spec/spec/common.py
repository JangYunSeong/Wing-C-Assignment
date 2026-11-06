"""공통 과제 골격 (결정적, LLM 없음, 지원자 문서 없음 — 불변식 3) [확정: 2026-11-02].

역량 고르기: 공고 **주** 역량 중 셀 범위 NCS 근거(수행준거가 있는 능력단위)가 있는 것 → 2~3개.
  순서 = 요구 수준(수행 > 활용 > 이해 > 언급) → 담당업무 우선 → 공고 문장 수 → id.
  1위와 근접·기반 관계(Relatedness.pairs)인 역량을 먼저 묶는다 (한 과제로 자연스러움).
  주 역량이 2개 미만이면 보조 역량으로 채우고 review_flags 에 남긴다.
난이도: 고른 역량 중 최고 요구 수준 — 수행 → high, 활용 → medium, 그 밖 → low. 제한 시간 기본 60분.
기술 환경: 공고 언어만 (담당업무·자격요건에 나온 언어 먼저).
함정·심층 구조는 부서 사례(source_incident)가 없으면 LLM 제안 → review_flags 에 '부서 사례 없음'.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from task_spec.ontology.relatedness import Relatedness
from task_spec.schemas import (
    CompetencyEvidence,
    PostingCompetency,
    PostingProfile,
    TargetCompetency,
    TechEnv,
)

LEVEL_RANK = {"수행": 3, "활용": 2, "이해": 1, "언급": 0}
SECTION_RANK = {"duties": 0, "required": 1, "preferred": 2}
DIFFICULTY = {3: "high", 2: "medium"}
MIN_COMPS, MAX_COMPS = 2, 3
TIME_LIMIT_MIN = 60
# 기능 개발형(feature): 1단계 구현 → 2단계 디버깅 (합 = TIME_LIMIT_MIN) [확정: 2026-11-06 사용자]
FEATURE_BUILD_MIN, FEATURE_DEBUG_MIN = 40, 20
SCOPE_ORDER = {"primary": 0, "secondary": 1, "sub_fallback": 2}

FLAG_NO_INCIDENT = "부서 결정적 사건 없음 — 함정·심층 구조는 LLM 제안, 승인자 검토 필요"
FLAG_PUBLIC_INCIDENT = (
    "공개 사례 기반 — 부서 결정적 사건 아님. 원리만 가져오고 소재는 바꿈, 승인자 검토 필요"
)
FLAG_SUPPORTING = "주 역량이 2개 미만 — 우대(보조) 역량으로 채움"
FLAG_NO_EVIDENCE = "NCS 근거가 있는 공고 역량 없음 — 공통 과제를 만들 수 없음"
FLAG_FALLBACK = "셀에 NCS 목록이 없어 직군 단위 NCS 로 대체한 근거 사용 (덜 정밀)"
FLAG_NO_LANGUAGE = "공고에 언어가 없음 — 기술 환경을 승인자가 정해야 함"


def _max_level(c: PostingCompetency) -> int:
    return max((LEVEL_RANK.get(lv, 0) for lv in c.levels), default=0)


def _key(c: PostingCompetency) -> tuple[int, int, int, str]:
    sec = min((SECTION_RANK[s] for s in c.sections), default=3)
    return (-_max_level(c), sec, -len(c.line_ids), c.competency_id)


@dataclass
class CommonPlan:
    competencies: list[PostingCompetency] = field(default_factory=list)
    unit_ids: dict[str, list[str]] = field(default_factory=dict)  # 역량 → 근거 능력단위 (범위 순)
    flags: list[str] = field(default_factory=list)

    @property
    def max_level(self) -> int:
        return max((_max_level(c) for c in self.competencies), default=0)


def plan_common(
    profile: PostingProfile,
    evidence: list[CompetencyEvidence],
    rel: Relatedness | None = None,
) -> CommonPlan:
    # 수행준거가 있는 단위만 근거로 친다 (요소만 있고 수행준거가 빈 단위가 온톨로지에 있음)
    ev = {
        e.competency_id: [u for u in e.ncs_units if u.n_criteria > 0]
        for e in evidence
        if any(u.n_criteria > 0 for u in e.ncs_units)
    }
    prim = sorted(
        (c for c in profile.competencies if c.primary and c.competency_id in ev), key=_key
    )
    supp = sorted(
        (c for c in profile.competencies if not c.primary and c.competency_id in ev), key=_key
    )
    plan = CommonPlan()
    if not prim and not supp:
        plan.flags.append(FLAG_NO_EVIDENCE)
        return plan
    pool = prim or supp
    first, rest = pool[0], pool[1:]
    linked = rel.pairs.get(first.competency_id, set()) if rel else set()
    rest = sorted(rest, key=lambda c: (c.competency_id not in linked, _key(c)))
    chosen = [first, *rest[: MAX_COMPS - 1]]
    if len(chosen) < MIN_COMPS and prim:
        chosen += supp[: MIN_COMPS - len(chosen)]
    if any(not c.primary for c in chosen):
        plan.flags.append(FLAG_SUPPORTING)
    plan.competencies = chosen
    for c in chosen:
        units = sorted(ev[c.competency_id], key=lambda u: (SCOPE_ORDER[u.scope], u.unit_cl_cd))
        plan.unit_ids[c.competency_id] = [u.unit_cl_cd for u in units]
        if any(u.scope == "sub_fallback" for u in units) and FLAG_FALLBACK not in plan.flags:
            plan.flags.append(FLAG_FALLBACK)
    return plan


def targets(plan: CommonPlan, evidence: list[CompetencyEvidence]) -> list[TargetCompetency]:
    ev = {e.competency_id: e for e in evidence}
    out = []
    for c in plan.competencies:
        e = ev.get(c.competency_id)
        out.append(
            TargetCompetency(
                competency_id=c.competency_id,
                label=c.label,
                level=max(c.levels, key=lambda lv: LEVEL_RANK.get(lv, 0)) if c.levels else None,
                esco_uri=e.esco[0].uri if e and e.esco else None,
                ncs_unit_ids=plan.unit_ids.get(c.competency_id, []),
            )
        )
    return out


def difficulty(plan: CommonPlan) -> str:
    return DIFFICULTY.get(plan.max_level, "low")


def common_tech_env(profile: PostingProfile) -> tuple[TechEnv, list[str]]:
    langs = sorted(
        (t for t in profile.tools if t.kind == "language"),
        key=lambda t: (min((SECTION_RANK[s] for s in t.sections), default=3), t.label),
    )
    env = TechEnv(languages=[t.label for t in langs], basis="공고 언어")
    return env, ([] if langs else [FLAG_NO_LANGUAGE])
