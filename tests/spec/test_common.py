"""공통 과제 골격 (결정적) — 가상 공고 프로필·근거."""

from __future__ import annotations

from task_spec.ontology.relatedness import Relatedness
from task_spec.schemas import (
    CompetencyEvidence,
    NcsUnitRef,
    PostingCompetency,
    PostingProfile,
    PostingTool,
)
from task_spec.spec.common import (
    FLAG_FALLBACK,
    FLAG_NO_EVIDENCE,
    FLAG_NO_LANGUAGE,
    FLAG_SUPPORTING,
    common_tech_env,
    difficulty,
    plan_common,
    targets,
)


def _pc(cid: str, sections: list[str], levels: list[str], n: int = 1) -> PostingCompetency:
    return PostingCompetency(
        competency_id=cid,
        label=cid,
        sections=sections,  # type: ignore[arg-type]
        primary=bool({"duties", "required"} & set(sections)),
        levels=levels,
        line_ids=[f"l{i}" for i in range(n)],
    )


def _ev(cid: str, scope: str = "primary", unit: str | None = None) -> CompetencyEvidence:
    u = NcsUnitRef(unit_cl_cd=unit or f"u-{cid}", name="단위", scope=scope, n_criteria=3)  # type: ignore[arg-type]
    return CompetencyEvidence(competency_id=cid, ncs_units=[u])


def _profile(
    comps: list[PostingCompetency], tools: list[PostingTool] | None = None
) -> PostingProfile:
    return PostingProfile(
        posting_id="p",
        ontology_version="v",
        sub="data.eng",
        industry_id="ind.x",
        competencies=comps,
        tools=tools or [],
    )


COMPS = [
    _pc("comp:a", ["required"], ["활용"], n=5),
    _pc("comp:b", ["duties"], ["수행"], n=1),
    _pc("comp:c", ["duties"], ["활용"], n=2),
    _pc("comp:d", ["duties"], ["이해"], n=9),
    _pc("comp:e", ["preferred"], ["수행"]),
    _pc("comp:noev", ["duties"], ["수행"], n=20),
]
EV = [_ev(c) for c in ["comp:a", "comp:b", "comp:c", "comp:d", "comp:e"]]


def test_order_level_then_section_then_lines_and_skip_no_evidence() -> None:
    plan = plan_common(_profile(COMPS), EV)
    # 수행 b > 활용·담당업무 c > 활용·자격요건 a. 근거 없는 noev·보조 e 는 제외
    assert [c.competency_id for c in plan.competencies] == ["comp:b", "comp:c", "comp:a"]
    assert plan.flags == [] and difficulty(plan) == "high"


def test_related_to_first_is_grouped_first() -> None:
    rel = Relatedness(pairs={"comp:b": {"comp:d"}, "comp:d": {"comp:b"}})
    plan = plan_common(_profile(COMPS), EV, rel)
    assert [c.competency_id for c in plan.competencies] == ["comp:b", "comp:d", "comp:c"]


def test_fill_with_supporting_and_flags() -> None:
    plan = plan_common(_profile([COMPS[1], COMPS[4]]), EV)
    assert [c.competency_id for c in plan.competencies] == ["comp:b", "comp:e"]
    assert FLAG_SUPPORTING in plan.flags
    none = plan_common(_profile([COMPS[5]]), EV)
    assert none.competencies == [] and none.flags == [FLAG_NO_EVIDENCE]


def test_targets_units_scope_order_and_fallback_flag() -> None:
    ev = [
        CompetencyEvidence(
            competency_id="comp:b",
            ncs_units=[
                NcsUnitRef(unit_cl_cd="u2", name="x", scope="sub_fallback", n_criteria=2),
                NcsUnitRef(unit_cl_cd="u0", name="z", scope="primary", n_criteria=0),
                NcsUnitRef(unit_cl_cd="u9", name="y", scope="primary", n_criteria=2),
            ],
        ),
        _ev("comp:c"),
    ]
    plan = plan_common(_profile(COMPS[1:3]), ev)
    t = targets(plan, ev)
    assert t[0].ncs_unit_ids == [
        "u9",
        "u2",
    ]  # 수행준거 없는 u0 제외, 범위 순 and t[0].level == "수행"
    assert FLAG_FALLBACK in plan.flags


def test_common_tech_env_uses_posting_languages_only() -> None:
    tools = [
        PostingTool(tool_id="tech:go", label="Go", kind="language", sections=["preferred"]),
        PostingTool(tool_id="tech:python", label="Python", kind="language", sections=["duties"]),
        PostingTool(tool_id="tech:spark", label="Spark", kind="framework", sections=["duties"]),
    ]
    env, flags = common_tech_env(_profile(COMPS, tools))
    assert env.languages == ["Python", "Go"] and env.frameworks == [] and flags == []
    _, flags = common_tech_env(_profile(COMPS))
    assert flags == [FLAG_NO_LANGUAGE]
