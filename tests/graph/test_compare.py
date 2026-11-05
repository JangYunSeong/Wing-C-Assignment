"""④ 대조 (결정적) — 가상 공고 프로필·주장·선택 결과."""

from __future__ import annotations

from task_spec.nodes.compare import MAX_VERIFY, compare_claims
from task_spec.ontology.relatedness import Relatedness
from task_spec.schemas import (
    CandidateRef,
    Claim,
    ClaimCandidates,
    CrossDocLink,
    OverlapCheck,
    OverlapCheckResult,
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


# ── 두 경로 합치기 (선택 경로 + 공고 역량 확인) [확정: 2026-11-04] ─────────────


def _checked(*items: tuple[str, str, list[str]], status: str = "ok") -> RunState:
    res = OverlapCheckResult(
        status=status,  # type: ignore[arg-type]
        items=[OverlapCheck(competency_id=c, label=lab, claim_ids=ids) for c, lab, ids in items],  # type: ignore[arg-type]
    )
    return _state(overlap_check=res)


def test_sources_both_verify_and_select_only_is_not_overlap() -> None:
    # 선택: a ← c1·c5, b ← c2 / 확인: a action(c1), b none, c action(c3)
    cmp = compare_claims(
        _checked(
            ("comp:a", "action", ["d1-c1"]),
            ("comp:b", "none", []),
            ("comp:c", "action", ["d1-c3"]),
        )
    )
    assert [(m.competency_id, m.overlap_source, m.check_label) for m in cmp.overlap] == [
        ("comp:a", "both", "action"),
        ("comp:c", "verify", "action"),
    ]
    # 선택만 일치 + 확인 none → 공고에만, select_only 표시 [2026-11-06]
    assert [(m.competency_id, m.select_only, m.overlap_source) for m in cmp.posting_only] == [
        ("comp:b", True, None)
    ]
    assert cmp.related == [] and cmp.check_status == "ok" and cmp.promoted == []
    assert not any(m.select_only for m in cmp.overlap)
    c = cmp.overlap[1]
    assert (c.check_claim_ids, c.claim_ids, c.role) == (["d1-c3"], [], "lead")  # 근거 주장 칸 역할


def test_select_only_goes_to_related_or_posting_only() -> None:
    # 선택 b ← c2. 확인 related → 관련 경험, mention → 공고에만. 둘 다 select_only
    for label, where in (
        ("related", "related"),
        ("mention", "posting_only"),
        ("none", "posting_only"),
    ):
        cmp = compare_claims(
            _checked(
                ("comp:a", "action", ["d1-c1"]),
                ("comp:b", label, ["d1-c2"]),
                ("comp:c", "none", []),
            )
        )
        got = {m.competency_id: m for m in getattr(cmp, where)}
        assert got["comp:b"].select_only and got["comp:b"].check_label == label
        assert "comp:b" not in [m.competency_id for m in cmp.overlap]
        assert (
            not got.get("comp:c") or not got["comp:c"].select_only
        )  # 선택이 안 고른 역량은 표시 없음
    # 확인을 안 한 역량(답 빠짐)은 예전처럼 선택 경로만으로 겹침 — 출처·표시 없음
    partial = compare_claims(_checked(("comp:a", "action", ["d1-c1"]), status="partial"))
    b = next(m for m in partial.overlap if m.competency_id == "comp:b")
    assert (b.overlap_source, b.select_only, b.check_label) == (None, False, None)


def test_promoted_select_only_is_overlap_and_downgrade_wins() -> None:
    s = _checked(
        ("comp:a", "action", ["d1-c1"]), ("comp:b", "none", []), ("comp:c", "related", ["d1-c3"])
    )
    up = compare_claims(s, promoted=["comp:b", "comp:c", "comp:a", "comp:nope"])
    b = next(m for m in up.overlap if m.competency_id == "comp:b")
    assert (b.overlap_source, b.promoted, b.select_only) == ("promoted", True, True)
    assert up.promoted == [
        "comp:b"
    ]  # 선택만 일치가 아닌 역량(c 는 선택이 안 고름, a 는 action)은 올리지 않음
    both = compare_claims(s, downgraded=["comp:b"], promoted=["comp:b"])
    assert both.promoted == [] and [m.competency_id for m in both.related] == ["comp:b", "comp:c"]


def test_related_and_mention_are_not_overlap() -> None:
    cmp = compare_claims(
        _checked(
            ("comp:a", "action", ["d1-c1"]),
            ("comp:b", "action", ["d1-c2"]),
            ("comp:c", "related", ["d1-c3"]),
        )
    )
    assert [m.competency_id for m in cmp.related] == ["comp:c"]
    assert cmp.related[0].overlap_source is None and cmp.related[0].role == "lead"
    mention = compare_claims(
        _checked(("comp:a", "action", ["d1-c1"]), ("comp:c", "mention", ["d1-c3"]))
    )
    assert [m.competency_id for m in mention.posting_only] == ["comp:c"]
    # comp:b 는 확인 답이 없음(partial) → 선택 경로만으로 겹침, 출처 없음
    b = next(m for m in mention.overlap if m.competency_id == "comp:b")
    assert (b.overlap_source, b.check_label, b.role) == (None, None, "participate")


def test_roles_prefer_check_evidence() -> None:
    # a 의 확인 근거 c5(역할 없음)·c3(주도) → 주도. 선택 근거 c1(설계)는 확인 근거가 있으면 쓰지 않는다
    cmp = compare_claims(_checked(("comp:a", "action", ["d1-c5", "d1-c3"])))
    a = cmp.overlap[0]
    assert (a.overlap_source, a.role) == ("both", "lead")


def test_check_links_feed_verify_targets() -> None:
    # c3 은 선택 경로에서 공고 밖 comp:x 만 — 확인 경로에서 공고 주 역량 comp:c 근거가 되면 공고 주 역량 점수
    base = compare_claims(_state())
    cmp = compare_claims(_checked(("comp:c", "action", ["d1-c3"]), ("comp:a", "none", [])))
    c3 = next(x for x in cmp.claims_to_verify if x.claim_id == "d1-c3")
    old = next(x for x in base.claims_to_verify if x.claim_id == "d1-c3")
    assert (old.score, c3.score) == (3.5, 5.5)  # 밖1 → 주3 (+ 주도 1.5 + 수치 1)
    assert c3.competency_ids == ["comp:x", "comp:c"]
    assert c3.reasons[0] == "공고 주 역량: C(확인)"
    # 확인 근거가 된 uncovered 주장은 uncovered 에서 뺀다
    unc = compare_claims(_checked(("comp:b", "action", ["d1-c4"])))
    assert unc.uncovered == [] and "d1-c4" in [x.claim_id for x in unc.claims_to_verify]


def test_downgrade_moves_to_related_and_drops_links() -> None:
    s = _checked(("comp:c", "action", ["d1-c3"]), ("comp:a", "action", ["d1-c1"]))
    cmp = compare_claims(s, downgraded=["comp:c", "comp:nope"])
    assert [m.competency_id for m in cmp.related] == ["comp:c"] and cmp.related[0].downgraded
    assert cmp.downgraded == ["comp:c"]  # 공고 밖 id 는 무시
    c3 = next(x for x in cmp.claims_to_verify if x.claim_id == "d1-c3")
    assert c3.competency_ids == ["comp:x"]


def test_failed_check_falls_back_to_select_path() -> None:
    cmp = compare_claims(_checked(status="failed"))
    base = compare_claims(_state())
    assert [m.competency_id for m in cmp.overlap] == [m.competency_id for m in base.overlap]
    assert all(m.overlap_source is None and m.check_label is None for m in cmp.overlap)
    assert cmp.check_status == "failed" and base.check_status is None
    assert cmp.claims_to_verify == base.claims_to_verify


def test_role_falls_back_to_select_evidence() -> None:
    # 확인 근거 c5 는 칸 밖(역할 없음) → 선택 경로 근거 c1(설계)·c5 의 역할로
    cmp = compare_claims(_checked(("comp:a", "action", ["d1-c5"])))
    a = next(m for m in cmp.overlap if m.competency_id == "comp:a")
    assert (a.check_claim_ids, a.role) == (["d1-c5"], "design")


def test_select_only_is_marked_in_verify_reasons() -> None:
    from task_spec.nodes.compare import SELECT_ONLY

    cmp = compare_claims(_checked(("comp:a", "action", ["d1-c1"]), ("comp:b", "none", [])))
    c2 = next(x for x in cmp.claims_to_verify if x.claim_id == "d1-c2")
    assert (
        f"{SELECT_ONLY}: B" in c2.reasons
    )  # 확인 경로 none + 선택만 → 공고에만, 검증 후보로 남고 사유 표시
    assert (
        c2.competency_ids == ["comp:b"] and c2.score == 2.5
    )  # 점수 규칙 그대로 (보조 2 + 참여 0.5)
    c1 = next(x for x in cmp.claims_to_verify if x.claim_id == "d1-c1")
    assert not any(r.startswith(SELECT_ONLY) for r in c1.reasons)
    assert SELECT_ONLY == "선택만 일치(확인 경로 근거 없음)"
    from pathlib import Path

    ui = Path(__file__).resolve().parents[2] / "src/task_spec/api/static/app.js"
    assert f'const SELECT_ONLY_KO = "{SELECT_ONLY}"' in ui.read_text(
        "utf-8"
    )  # 승인 화면 표시와 같은 문구


def test_promoted_reason_replaces_select_only_reason() -> None:
    from task_spec.nodes.compare import PROMOTED_KO, SELECT_ONLY

    s = _checked(("comp:a", "action", ["d1-c1"]), ("comp:b", "none", []))
    c2 = next(
        x for x in compare_claims(s, promoted=["comp:b"]).claims_to_verify if x.claim_id == "d1-c2"
    )
    assert f"{PROMOTED_KO}: B" in c2.reasons
    assert not any(r.startswith(SELECT_ONLY) for r in c2.reasons)
    from pathlib import Path

    ui = (Path(__file__).resolve().parents[2] / "src/task_spec/api/static/app.js").read_text(
        "utf-8"
    )
    assert f'promoted: "{PROMOTED_KO}"' in ui  # 승인 화면과 같은 말
    assert 'select: "선택만 일치(예전 실행) — 확인 필요"' in ui
