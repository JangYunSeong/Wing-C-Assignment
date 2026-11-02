"""④ 대조 (결정적) — 가상 공고 프로필·주장·선택 결과."""

from __future__ import annotations

from task_spec.nodes.compare import MAX_VERIFY, compare_claims
from task_spec.ontology.relatedness import Relatedness
from task_spec.schemas import (
    CandidateRef,
    Claim,
    ClaimCandidates,
    CrossDocLink,
    PostingCompetency,
    PostingProfile,
    RunState,
)


def _claim(cid: str, role: str | None, quote: str, result: str | None = None) -> Claim:
    return Claim(
        claim_id=cid,
        doc_id="d1",
        action="행동",
        quote=quote,
        result=result,
        role=role,  # type: ignore[arg-type]
        role_basis=f"field:{role}" if role else "no_field",
    )


def _pick(cid: str, comps: list[str], uncovered: bool = False) -> ClaimCandidates:
    refs = [CandidateRef(competency_id=c, label=f"라벨 {c}", score=0.1) for c in comps]
    return ClaimCandidates(claim_id=cid, candidates=refs, competency_ids=comps, uncovered=uncovered)


def _state(**kw: object) -> RunState:
    profile = PostingProfile(
        posting_id="p-fake",
        ontology_version="vtest",
        sub="data.eng",
        industry_id="ind.fake",
        competencies=[
            PostingCompetency(competency_id="comp:a", label="A", primary=True),
            PostingCompetency(competency_id="comp:b", label="B", primary=False),
            PostingCompetency(competency_id="comp:c", label="C", primary=True),
        ],
    )
    claims = [
        _claim("d1-c1", "design", "파이프라인 구조를 설계했다", result="30% 단축"),
        _claim("d1-c2", "participate", "배치 운영에 참여했다"),
        _claim("d1-c3", "lead", "캐시 도입을 주도해 응답을 40% 줄였다"),
        _claim("d1-c4", "lead", "랭커로 재정렬했다"),
        _claim("d1-c5", None, "파이프라인을 다뤘다"),
    ]
    picks = [
        _pick("d1-c1", ["comp:a"]),
        _pick("d1-c2", ["comp:b"]),
        _pick("d1-c3", ["comp:x"]),
        _pick("d1-c4", [], uncovered=True),
        _pick("d1-c5", ["comp:a"]),
    ]
    return RunState(
        run_id="r1",
        ontology_version="vtest",
        posting_id="p-fake",
        posting_profile=profile,
        claims=claims,
        candidates=picks,
        **kw,  # type: ignore[arg-type]
    )


def test_categories_with_evidence_claims() -> None:
    cmp = compare_claims(_state())
    assert [(m.competency_id, m.claim_ids, m.primary) for m in cmp.overlap] == [
        ("comp:a", ["d1-c1", "d1-c5"], True),
        ("comp:b", ["d1-c2"], False),
    ]
    assert [m.competency_id for m in cmp.posting_only] == ["comp:c"]
    assert [(m.competency_id, m.in_posting, m.label) for m in cmp.applicant_only] == [
        ("comp:x", False, "라벨 comp:x")
    ]
    assert cmp.uncovered == ["d1-c4"]


def test_verify_ranking_and_reasons() -> None:
    t = compare_claims(_state()).claims_to_verify
    # c1: 주3+설계2+수치1=6 / c3: 밖1+주도1.5+수치1=3.5 / c5: 주3+역할없음0=3 / c2: 보조2+참여0.5=2.5
    assert [(x.claim_id, x.score) for x in t] == [
        ("d1-c1", 6.0),
        ("d1-c3", 3.5),
        ("d1-c5", 3.0),
        ("d1-c2", 2.5),
    ]
    assert "d1-c4" not in [x.claim_id for x in t]  # 역량 없는 주장은 후보 아님
    assert "역할 확인 필요" in t[2].reasons
    assert len(t) <= MAX_VERIFY


def test_mismatch_link_raises_priority() -> None:
    link = CrossDocLink(resume_claim_id="d1-c2", cover_letter_claim_id="d2-c9", relation="mismatch")
    t = compare_claims(_state(links=[link])).claims_to_verify
    c2 = next(x for x in t if x.claim_id == "d1-c2")
    assert c2.score == 4.5 and "이력서·자기소개서 어긋남" in c2.reasons


def test_no_documents_only_posting() -> None:
    s = _state().model_copy(update={"claims": [], "candidates": []})
    cmp = compare_claims(s)
    assert [m.competency_id for m in cmp.posting_only] == ["comp:a", "comp:b", "comp:c"]
    assert cmp.overlap == [] and cmp.claims_to_verify == []


def _rel(**kw: object) -> Relatedness:
    return Relatedness(**kw)  # type: ignore[arg-type]


def test_outside_competency_relatedness_levels() -> None:
    # comp:x 가 공고 역량 comp:a 와 근접 관계 → 강(+2): c3 = 2 + 주도 1.5 + 수치 1 = 4.5
    rel = _rel(pairs={"comp:x": {"comp:a"}, "comp:a": {"comp:x"}})
    cmp = compare_claims(_state(), rel)
    c3 = next(x for x in cmp.claims_to_verify if x.claim_id == "d1-c3")
    assert c3.score == 4.5 and any("직무 연계 강" in r for r in c3.reasons)
    assert cmp.applicant_only[0].relatedness == "strong"


def test_share_and_scope_give_medium_weak_is_excluded() -> None:
    medium = compare_claims(_state(), _rel(share={"data.eng": {"comp:x": 0.05}}))
    c3 = next(x for x in medium.claims_to_verify if x.claim_id == "d1-c3")
    assert c3.score == 3.5  # 중 +1
    scoped = compare_claims(_state(), _rel(scopes={"comp:x": ["data.*"]}))
    assert scoped.applicant_only[0].relatedness == "medium"
    weak = compare_claims(_state(), _rel(share={"data.eng": {"comp:x": 0.01}}))
    assert "d1-c3" not in [x.claim_id for x in weak.claims_to_verify]  # 약 → 후보 제외
    m = weak.applicant_only[0]
    assert (m.competency_id, m.relatedness, m.claim_ids) == (
        "comp:x",
        "weak",
        ["d1-c3"],
    )  # 목록엔 남음
