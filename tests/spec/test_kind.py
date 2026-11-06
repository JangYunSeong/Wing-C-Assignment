"""공통 과제 유형 기본값 규칙 (spec/kind.py) — 가상 공고 프로필."""

from __future__ import annotations

from task_spec.schemas import PostingCompetency, PostingProfile
from task_spec.spec.kind import choose_task_kind, kind_lists


def _pc(cid: str, primary: bool = True) -> PostingCompetency:
    return PostingCompetency(
        competency_id=cid,
        label=cid,
        sections=["duties"] if primary else ["preferred"],
        primary=primary,
        levels=["활용"],
    )


def _profile(sub: str | None, comps: list[PostingCompetency]) -> PostingProfile:
    return PostingProfile(posting_id="p", ontology_version="v", sub=sub, competencies=comps)


def test_backend_lists_are_disjoint_competency_ids() -> None:
    lists = kind_lists("sw.backend")
    assert lists is not None
    assert not lists.build & lists.diagnose
    assert all(c.startswith("comp:") for c in lists.build | lists.diagnose)


def test_backend_build_heavy_posting_is_feature() -> None:
    p = _profile(
        "sw.backend", [_pc("comp:sw.rest_api"), _pc("comp:sw.spring"), _pc("comp:infra.incident")]
    )
    choice = choose_task_kind(p)
    assert choice.kind == "feature"
    assert "만드는 역량 2개" in choice.basis and "진단 역량 1개" in choice.basis


def test_backend_diagnose_heavy_posting_is_incident() -> None:
    p = _profile(
        "sw.backend", [_pc("comp:sw.java"), _pc("comp:infra.incident"), _pc("comp:sw.perf")]
    )
    assert choose_task_kind(p).kind == "incident"


def test_tie_goes_to_feature() -> None:
    p = _profile("sw.backend", [_pc("comp:sw.rest_api"), _pc("comp:infra.monitoring")])
    assert choose_task_kind(p).kind == "feature"


def test_only_primary_competencies_count() -> None:
    # 우대(보조)에만 있는 만드는 역량은 세지 않는다 → 진단 1 > 만드는 0
    p = _profile(
        "sw.backend",
        [_pc("comp:infra.incident"), _pc("comp:sw.rest_api", primary=False)],
    )
    assert choose_task_kind(p).kind == "incident"


def test_other_subs_stay_incident() -> None:
    p = _profile("data.eng", [_pc("comp:sw.rest_api"), _pc("comp:sw.spring")])
    choice = choose_task_kind(p)
    assert choice.kind == "incident" and "유형 규칙 없음" in choice.basis
    assert choose_task_kind(_profile(None, [_pc("comp:sw.rest_api")])).kind == "incident"
