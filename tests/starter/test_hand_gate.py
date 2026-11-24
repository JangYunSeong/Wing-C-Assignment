"""손 구현 관문 ⑫ (#256 3단계) — 재료 손 구현 묶음(incidents/public_v1/references)·판정(starter/hand_refs.py)·빌드 연결(build._hand_gate)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.llm import Completion, LLMClient
from task_spec.nodes.starter import build as build_mod
from task_spec.nodes.starter.build import BuildOutcome, build_and_repair
from task_spec.schemas import GeneratedFile, ReferenceManifest, VerifyReport
from task_spec.schemas.build import HiddenPlan
from task_spec.spec.feature import find_terms
from task_spec.spec.incidents import feature_ref, incident_by_id, load_incidents, load_interfaces
from task_spec.spec.interface_terms import interface_brief
from task_spec.starter.hand_refs import (
    LABEL,
    HandGate,
    HandReferences,
    finding_flags,
    hidden_note,
    judge_hands,
    load_references,
    public_note,
)
from task_spec.starter.interface import interface_ref, interface_text
from task_spec.starter.repair import WEAK_FLAG, RepairPlan
from task_spec.starter.static_check import Overlay
from task_spec.starter.variants import VariantGate
from tests.starter.test_generate import D1, STARTER, _files, _inputs, _task
from tests.starter.test_repair import HIDDEN as HIDDEN_ANSWER
from tests.starter.test_repair import (
    PROMPTS,
    PUBLIC_TEST,
    RoutedBackend,
    _no_sleep,
    _one_case,
    _run,
    _t,
)
from tests.starter.test_repair import PUBLIC as PUBLIC_ANSWER
from tests.starter.test_repair import SVC as SVC_ANSWER
from tests.starter.test_trap_gate import NAIVE_PUB, PUBLIC_CLASS, REPORT

INCIDENTS = Path("incidents/public_v1")
BATCH = "pub:data.eng:batch-rerun-late-partial"
REASONS = ["late_data_missed", "duplicate_counted", "partial_output_visible"]
RULE = "공개 테스트 입력에는 같은 번호를 두 번 넣지 않는다"


def _refs(*specs: tuple[str, str, list[str]], rules: tuple[str, ...] = (RULE,)) -> HandReferences:
    refs: list[dict[str, Any]] = []
    design = 0
    for name, kind, misses in specs:
        design += kind == "accepted"
        refs.append(
            {
                "name": name,
                "kind": kind,
                "misses": misses,
                "design": design if kind == "accepted" else None,
                "note": f"{name} 설명",
            }
        )
    m = ReferenceManifest.model_validate(
        {"incident_id": BATCH, "interface_sha256": "0" * 64, "refs": refs}
    )
    files = {
        r.name: (GeneratedFile(path="pipeline/run.py", content=f"# {r.name}\n"),) for r in m.refs
    }
    return HandReferences(m, files, "test", public_rules=rules)


SET = _refs(
    ("acc1", "accepted", []),
    ("acc2", "accepted", []),
    ("naive", "naive", REASONS),
    ("no-restate", "one_miss", ["late_data_missed"]),
    ("no-dedupe", "one_miss", ["duplicate_counted"]),
    ("nonatomic", "one_miss", ["partial_output_visible"]),
)


def _plan(*cases: tuple[str, str]) -> HiddenPlan:
    return HiddenPlan.model_validate(
        {
            "cases": [
                {
                    "case_id": cid,
                    "ac_id": "h" + cid[2:],
                    "trap_id": "t1",
                    "scenario": "장면",
                    "expect": "기대",
                    "intended_reason": reason,
                }
                for cid, reason in cases
            ]
        }
    )


PLAN = _plan(
    ("hc1", "duplicate_counted"), ("hc2", "late_data_missed"), ("hc3", "partial_output_visible")
)


def _ideal() -> dict[str, dict[str, str]]:
    pub = {"pt1": "PASS", "pt2": "PASS"}
    ok = {"hc1": "PASS", "hc2": "PASS", "hc3": "PASS"}
    return {
        "acc1": {**pub, **ok},
        "acc2": {**pub, **ok},
        "naive": {
            **pub,
            "hc1": "duplicate_counted",
            "hc2": "late_data_missed",
            "hc3": "partial_output_visible",
        },
        "no-restate": {**pub, **ok, "hc2": "late_data_missed"},
        "no-dedupe": {**pub, **ok, "hc1": "duplicate_counted"},
        "nonatomic": {**pub, **ok, "hc3": "partial_output_visible"},
    }


def _report(table: dict[str, dict[str, str]], **setup: str) -> VerifyReport:
    """칸 값 — PASS · ASSERT · HARNESS_ERR · 그 밖은 그 사유로 ASSERT."""
    runs = []
    for name, cells in table.items():
        tests = []
        for test, v in cells.items():
            result = v if v in ("PASS", "ASSERT", "HARNESS_ERR") else "ASSERT"
            reason = None if v in ("PASS", "ASSERT", "HARNESS_ERR") else v
            group = "public" if test.startswith("pt") else "hidden"
            tests.append(
                _t(group, test, result, reason, "" if result == "PASS" else f"{test} 메시지")
            )
        runs.append(_run(LABEL + name, "ref_naive", *tests, setup=setup.get(name, "ok")))
    return VerifyReport(runs=runs)


# ── 판정 ────────────────────────────────────────────────────────────────


def test_ideal_matrix_has_nothing_to_fix_and_keeps_every_case() -> None:
    """11-06 손 예시 관문 그대로 — 허용 둘은 모두 통과, 하나만 놓친 셋은 자기 사유 케이스에서만, 흔한 구현은 셋 다."""
    j = judge_hands(_report(_ideal()), SET, PLAN)
    assert j.findings == [] and j.flags == []
    assert j.keep == {"hc1", "hc2", "hc3"}
    assert len(j.lines) == 6 and j.lines[2].startswith(
        "naive (흔한 구현) 공개 2/2 · 숨은 hc1:duplicate_counted"
    )


def test_a_test_every_accepted_fails_is_a_defect_and_one_failing_accepted_is_a_flag() -> None:
    t = _ideal()
    t["acc1"]["hc2"] = t["acc2"]["hc2"] = "ASSERT"
    j = judge_hands(_report(t), SET, PLAN)
    assert [(f.rule, f.generator, f.test, f.cases) for f in j.findings] == [
        ("H4", "gen_hidden", "hc2", ("hc2",))
    ]
    # 결함 케이스에서 떨어진 것은 근거가 아니다 — 구하기에서 빠지고 no-restate 는 판정할 케이스가 없다
    assert "hc2" not in j.keep and j.flags == []

    t = _ideal()
    t["acc1"]["pt1"] = "ASSERT"
    t["acc2"]["hc3"] = "ASSERT"
    j = judge_hands(_report(t), SET, PLAN)
    assert j.findings == []
    assert any("acc1 만 공개 테스트 pt1 에서 실패" in f for f in j.flags)
    assert any("acc2 만 숨은 케이스 hc3 에서 실패" in f and "조건을 넘는" in f for f in j.flags)


def test_a_public_test_a_naive_shape_fails_is_a_leak_and_the_note_hides_the_shape() -> None:
    t = _ideal()
    t["naive"]["pt2"] = "HARNESS_ERR"
    t["no-restate"]["pt2"] = "ASSERT"
    j = judge_hands(_report(t), SET, PLAN)
    assert [(f.rule, f.generator, f.test, f.refs) for f in j.findings] == [
        ("H5", "gen_public", "pt2", ("naive", "no-restate"))
    ]
    note = public_note(j.findings, SET.public_rules)
    assert "공개 테스트 pt2" in note and "pt2 메시지" in note and RULE in note
    assert "다른 공개 테스트 파일은 그대로" in note  # 변별 관문이 더한 클래스를 지우지 않게
    assert (
        "naive" not in note and "설명" not in note
    )  # 손 구현 이름·설명(숨은 요구)은 공개 테스트 수리 요청에 넣지 않는다
    assert "누설 의심" in finding_flags(j.findings, "다시 생성 1회 뒤에도")[0]

    t["acc1"]["pt2"] = t["acc2"]["pt2"] = "ASSERT"  # 허용 손 구현도 모두 떨어지면 결함 하나로만
    j = judge_hands(_report(t), SET, PLAN)
    assert [(f.rule, f.test) for f in j.findings] == [("H4", "pt2")]


def test_a_one_miss_that_passes_its_reason_case_sends_the_hidden_test_back() -> None:
    t = _ideal()
    t["no-dedupe"]["hc1"] = "PASS"
    j = judge_hands(_report(t), SET, PLAN)
    assert [(f.rule, f.generator, f.cases, f.refs) for f in j.findings] == [
        ("H3", "gen_hidden", ("hc1",), ("no-dedupe",))
    ]
    assert "사유 duplicate_counted 만 놓친 구현(no-dedupe 설명)" in j.findings[0].detail
    assert "hc1" in j.keep  # 흔한 구현이 그 사유 케이스에서 떨어진다
    assert "다른 케이스는 그대로" in hidden_note(j.findings)

    t = _ideal()
    t["naive"] = {k: "PASS" for k in t["naive"]}
    j = judge_hands(_report(t), SET, PLAN)
    assert [(f.rule, f.test, f.refs) for f in j.findings] == [("H3", "", ("naive",))]


def test_a_one_miss_failing_another_reason_case_is_only_flagged() -> None:
    t = _ideal()
    t["no-restate"]["hc1"] = "ASSERT"
    j = judge_hands(_report(t), SET, PLAN)
    assert j.findings == []
    assert any("no-restate 이 다른 사유 케이스 hc1 에서도 실패" in f for f in j.flags)


def test_setup_failures_and_missing_results_are_never_repair_evidence() -> None:
    """수집 오류·환경 문제로 LLM 재생성을 부르지 않게 — 결과 없음은 표시만 (리뷰 2026-11-24)."""
    t = _ideal()
    t["nonatomic"]["hc3"] = "PASS"  # 실행 준비가 안 됐으니 판정하지 않는다
    del t["no-dedupe"]["hc1"]  # 결과 없음 — 그 사유 케이스를 못 잡은 것으로 보지 않는다
    del t["naive"]["pt2"]  # 결과 없음 — 누설로 보지 않는다
    del t["acc1"]["pt1"]
    t["acc2"]["pt1"] = "ASSERT"  # 허용 하나는 결과 없음·하나는 실패 — 결함으로 단정하지 않는다
    j = judge_hands(_report(t, nonatomic="compile_error"), SET, PLAN)
    assert j.findings == []
    assert any("nonatomic 실행 준비 실패(compile_error)" in f for f in j.flags)
    assert any(
        "결과 없는 테스트" in f and "no-dedupe:hc1" in f and "naive:pt2" in f for f in j.flags
    )
    assert "hc1" in j.keep  # 흔한 구현이 의도한 사유 태그로 떨어뜨린다


def test_accepted_failing_every_test_stops_the_judgement() -> None:
    """허용 손 구현이 모든 테스트에서 떨어지면 테스트 결함이 아니라 손 구현이 이 계약·환경에서 돌지 않는 것 (11-18 계약 재생)."""
    t = _ideal()
    for name in ("acc1", "acc2"):
        t[name] = {k: "ASSERT" for k in t[name]}
    j = judge_hands(_report(t), SET, PLAN)
    assert j.findings == [] and j.keep == frozenset()
    assert any("허용 손 구현이 모든 테스트에서 실패" in f for f in j.flags)


def test_only_the_reason_owner_or_the_intended_tag_rescues_a_case() -> None:
    t = _ideal()
    t["naive"]["hc1"] = "late_data_missed"  # 흔한 구현이 다른 사유 태그로 떨어짐 — 근거 아님
    t["no-dedupe"]["hc1"] = "PASS"
    j = judge_hands(_report(t), SET, PLAN)
    assert "hc1" not in j.keep and {"hc2", "hc3"} <= j.keep
    t["no-dedupe"]["hc1"] = "ASSERT"  # 그 사유만 놓친 구현은 태그 없이 떨어져도 근거
    assert "hc1" in judge_hands(_report(t), SET, PLAN).keep


def test_a_reason_without_a_hidden_case_is_flagged() -> None:
    plan = _plan(("hc1", "duplicate_counted"), ("hc2", "late_data_missed"))
    t = _ideal()
    for cells in t.values():
        del cells["hc3"]
    j = judge_hands(_report(t), SET, plan)
    assert any("사유 partial_output_visible 케이스가 없음" in f for f in j.flags)
    assert j.findings == []


@pytest.mark.parametrize(
    ("refs", "message"),
    [
        (
            [
                {
                    "name": "ab",
                    "kind": "accepted",
                    "misses": ["x"],
                    "design": 1,
                    "note": "설명입니다",
                }
            ],
            "놓친 사유 없이",
        ),
        ([{"name": "ab", "kind": "accepted", "note": "설명입니다"}], "허용 설계 번호"),
        ([{"name": "ab", "kind": "naive", "design": 1, "note": "설명입니다"}], "허용 손 구현만"),
        (
            [{"name": "ab", "kind": "one_miss", "misses": ["x", "y"], "note": "설명입니다"}],
            "하나 적는다",
        ),
        ([{"name": "ab", "kind": "naive", "note": "설명입니다"}], "놓친 사유를 적는다"),
        (
            [{"name": "ab", "kind": "naive", "misses": ["x"], "note": "설명입니다"}],
            "허용 손 구현이 하나도 없음",
        ),
        (
            [
                {"name": "ab", "kind": "accepted", "design": 1, "note": "설명입니다"},
                {"name": "ab", "kind": "accepted", "design": 2, "note": "설명입니다"},
            ],
            "겹침",
        ),
    ],
)
def test_manifest_rejects_what_the_gate_could_not_judge(
    refs: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ReferenceManifest.model_validate(
            {"incident_id": BATCH, "interface_sha256": "0" * 64, "refs": refs}
        )


# ── 재료 손 구현 묶음 (git) ──────────────────────────────────────────────


def test_committed_references_match_the_material_and_the_interface() -> None:
    """인터페이스를 고치면 손 구현을 다시 확인하고 references.yaml 지문을 고친다 — 이 테스트가 그 약속을 지킨다."""
    refs = load_references(INCIDENTS)
    assert set(refs) == {BATCH}
    r = refs[BATCH]
    iface = load_interfaces(INCIDENTS)[BATCH]
    assert r.manifest.interface_sha256 == interface_ref(iface)
    checked = r.checked_against(iface)
    assert checked.stale == "" and checked.public_rules == tuple(iface.public_rules)
    inc = incident_by_id(load_incidents(INCIDENTS), BATCH)
    assert inc is not None and inc.feature is not None
    reasons = set(inc.feature.reasons)
    for x in r.refs:
        paths = {f.path for f in r.files[x.name]}
        assert {
            "pipeline/__init__.py",
            "pipeline/raw.py",
            "pipeline/reader.py",
            "pipeline/run.py",
        } <= paths
        assert set(x.misses) <= reasons, x.name
        if x.kind == "naive":
            assert set(x.misses) == reasons, x.name
    assert {x.misses[0] for x in r.refs if x.kind == "one_miss"} == reasons
    designs = sorted(x.design or 0 for x in r.refs if x.kind == "accepted")
    assert designs == list(range(1, len(inc.feature.accepted_designs) + 1))
    # 손 구현마다 자기 run.py — 공용 폴더의 것이 아니다
    runs = {
        x.name: next(f.content for f in r.files[x.name] if f.path == "pipeline/run.py")
        for x in r.refs
    }
    assert len(set(runs.values())) == len(runs)
    overlays = checked.overlays()
    assert [o.label for o in overlays] == [LABEL + x.name for x in r.refs]
    assert {o.kind for o in overlays} == {"ref_accepted", "ref_naive"}


def test_reference_notes_and_public_rules_never_name_the_hidden_requirement() -> None:
    """note 는 숨은 테스트 수리 요청·승인 화면에, 공개 테스트 규칙은 계약 입력에 나간다."""
    inc = incident_by_id(load_incidents(INCIDENTS), BATCH)
    assert inc is not None and inc.feature is not None
    f = inc.feature
    terms = [
        *f.trigger_terms,
        *f.leak_terms,
        *f.stage2_cause_terms,
        *inc.hidden_terms,
        *inc.avoid_terms,
    ]
    notes = [x.note for x in load_references(INCIDENTS)[BATCH].refs]
    rules = load_interfaces(INCIDENTS)[BATCH].public_rules
    assert find_terms(terms, json.dumps([*notes, *rules], ensure_ascii=False)) == []


def test_a_changed_interface_turns_the_gate_off() -> None:
    r = load_references(INCIDENTS)[BATCH]
    iface = load_interfaces(INCIDENTS)[BATCH]
    moved = iface.model_copy(
        update={"public_rules": [*iface.public_rules, "공개 테스트 규칙 하나를 더 둔다"]}
    )
    assert "references.yaml" in r.checked_against(moved).stale
    assert r.checked_against(None).stale


def test_public_rules_go_to_the_contract_input_only() -> None:
    iface = load_interfaces(INCIDENTS)[BATCH]
    assert iface.public_rules
    text = interface_text(iface)
    assert all(rule in text for rule in iface.public_rules)
    assert iface.public_rules[0] not in "\n".join(interface_brief(iface))  # 명세 입력에는 없다
    inc = incident_by_id(load_incidents(INCIDENTS), BATCH)
    assert inc is not None
    # 명세 저장 키는 명세가 보는 칸만 — 공개 테스트 규칙이 바뀌어도 승인된 명세를 그대로 쓴다
    assert feature_ref(inc, iface) == feature_ref(
        inc, iface.model_copy(update={"public_rules": []})
    )
    assert feature_ref(inc, iface) != feature_ref(
        inc,
        iface.model_copy(update={"incident_id": iface.incident_id, "files": iface.files[:1] * 2}),
    )


# ── 빌드 연결 ───────────────────────────────────────────────────────────


def test_settle_weak_keeps_the_cases_hand_refs_catch(monkeypatch: pytest.MonkeyPatch) -> None:
    dropped: list[list[str]] = []
    monkeypatch.setattr(build_mod, "_drop_cases", lambda out, b, ids: dropped.append(list(ids)))
    out = BuildOutcome(
        status="failed",
        review_flags=[
            f"숨은 케이스 hc1 는 숨은 테스트를 다시 생성한 뒤에도 흔한 구현(naive-all)이 통과 — hc1 는 {WEAK_FLAG} (무엇을 확인했나: x)",
            f"숨은 케이스 hc2 는 숨은 테스트를 다시 생성한 뒤에도 흔한 구현(naive-all)이 통과 — hc2 는 {WEAK_FLAG} (무엇을 확인했나: y)",
            "다른 표시",
        ],
    )
    build_mod._settle_weak(out, None, ["hc1", "hc2"], HandGate(keep=frozenset({"hc1", "hc3"})))  # type: ignore[arg-type]
    assert dropped == [["hc2"]] and out.hand_kept == ["hc1"]
    assert not any(f.startswith("숨은 케이스 hc1 는 ") for f in out.review_flags)
    assert any(f.startswith("숨은 케이스 hc2 는 ") for f in out.review_flags)
    assert any("숨은 케이스 hc1 채점 유지" in f for f in out.review_flags)


class HandBackend(RoutedBackend):
    MARKERS = {**RoutedBackend.MARKERS, "gen_variant": "**공개 요구 하나를 어긴 구현**"}


def _llm(**over: list[Completion]) -> tuple[LLMClient, HandBackend]:
    empty = Completion(json.dumps({"variants": []}))
    routes = {
        "gen_public": [PUBLIC_ANSWER, PUBLIC_ANSWER],
        "gen_starter": [_files(*STARTER)],
        "gen_hidden": [HIDDEN_ANSWER],
        "gen_naive": [SVC_ANSWER],
        "gen_accepted": [SVC_ANSWER],
        "gen_variant": [empty, empty, empty],
    } | over
    backend = HandBackend(**routes)
    return LLMClient(
        {"opencode": backend, "openai": backend, "gemini": backend}, sleep=_no_sleep
    ), backend  # type: ignore[dict-item]


class HandVerifier:
    """검증 회차 보고서를 차례로 (기본은 늘 통과). 변형 실행으로 손 구현(hand-)이 오면 hand 보고서를 차례로 (마지막 것을 되풀이)."""

    def __init__(self, *hand: VerifyReport, reports: Sequence[VerifyReport] = (REPORT,)) -> None:
        self.hand = list(hand)
        self.reports = list(reports)
        self.hand_calls: list[list[str]] = []

    async def verify(self, *a: Any, **k: Any) -> VerifyReport:
        return self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]

    async def verify_variants(
        self,
        starter: Overlay,
        variants: Sequence[Overlay],
        public: Overlay,
        hidden: Overlay | None = None,
    ) -> VerifyReport:
        if variants and variants[0].label.startswith(LABEL):
            assert hidden is not None  # 손 구현은 숨은 테스트와 함께 돈다
            self.hand_calls.append([v.label for v in variants])
            return self.hand.pop(0) if len(self.hand) > 1 else self.hand[0]
        return VerifyReport(
            runs=[
                _run(v.label, "ref_variant", _t("public", PUBLIC_CLASS, "ASSERT")) for v in variants
            ]
        )


def _hand_build_inputs(refs: HandReferences | None) -> Any:
    return _inputs(task=_task().model_copy(update={"accepted_designs": [D1]}), references=refs)


def _hand_report(naive_public: str) -> VerifyReport:
    case = _hand_build_inputs(None).plan.cases[0]
    return VerifyReport(
        runs=[
            _run(
                LABEL + "acc",
                "ref_accepted",
                _t("public", PUBLIC_CLASS),
                _t("hidden", case.case_id),
            ),
            _run(
                LABEL + "naive",
                "ref_naive",
                _t(
                    "public",
                    PUBLIC_CLASS,
                    naive_public,
                    None,
                    "" if naive_public == "PASS" else "공개 단언 실패",
                ),
                _t("hidden", case.case_id, "ASSERT", case.intended_reason, "숨은 단언 실패"),
            ),
        ]
    )


def _java_refs() -> HandReferences:
    reason = _hand_build_inputs(None).plan.cases[0].intended_reason
    return _refs(("acc", "accepted", []), ("naive", "naive", [reason]))


async def _build(
    verifier: HandVerifier, llm: LLMClient, refs: HandReferences | None
) -> BuildOutcome:
    return await build_and_repair(
        _hand_build_inputs(refs),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=verifier,
    )


async def test_hand_gate_sends_a_leaking_public_test_back_once_and_passes() -> None:
    llm, backend = _llm()
    verifier = HandVerifier(_hand_report("ASSERT"), _hand_report("PASS"))
    out = await _build(verifier, llm, _java_refs())
    assert out.status == "review"
    assert verifier.hand_calls == [[LABEL + "acc", LABEL + "naive"]] * 2
    assert backend.calls.count("gen_public") == 2
    regen = [u for n, u in backend.users if n == "gen_public"][1]
    assert "공개 요구만 지킨 구현이 공개 테스트" in regen and RULE in regen
    assert "naive 설명" not in regen and LABEL not in regen
    assert ("⑫", "gen_public", "regenerated") in [(r.gate, r.target, r.result) for r in out.repairs]
    assert out.regen_counts.get("gen_public#hand") == 1
    assert not any("누설 의심" in f for f in out.review_flags)
    assert len(out.hand) == 2 and out.hand_reports


async def test_hand_gate_flags_what_one_repair_did_not_fix() -> None:
    llm, backend = _llm()
    verifier = HandVerifier(_hand_report("ASSERT"))
    out = await _build(verifier, llm, _java_refs())
    assert out.status == "review" and backend.calls.count("gen_public") == 2
    assert len(verifier.hand_calls) == 2
    leak = [f for f in out.review_flags if "누설 의심" in f]
    assert len(leak) == 1 and "다시 생성 1회 뒤에도" in leak[0] and "naive" in leak[0]
    assert ("⑫", "gen_public", "skipped") in [(r.gate, r.target, r.result) for r in out.repairs]


async def test_hand_gate_is_off_with_a_stale_interface_and_absent_without_references() -> None:
    llm, _ = _llm()
    verifier = HandVerifier(_hand_report("ASSERT"))
    stale = replace(_java_refs(), stale="손 구현을 확인한 인터페이스 판과 지금 인터페이스가 다름")
    out = await _build(verifier, llm, stale)
    assert out.status == "review" and verifier.hand_calls == []
    assert [f for f in out.review_flags if f.startswith("손 구현 관문(⑫) 생략")] == [
        "손 구현 관문(⑫) 생략 — 손 구현을 확인한 인터페이스 판과 지금 인터페이스가 다름"
    ]

    llm, _ = _llm()
    verifier = HandVerifier(_hand_report("ASSERT"))
    out = await _build(verifier, llm, None)
    assert out.status == "review" and verifier.hand_calls == [] and out.hand == []
    assert not any("손 구현 관문" in f for f in out.review_flags)


PUBLIC_PATH = "src/test/java/com/example/service/publictests/OrderHistoryPublicTest.java"
# 검증 회차 실패 — 허용 구현이 숨은 케이스에서 떨어져 수리가 이어지다 회차 상한에서 끝난다
FAILING = _one_case(
    NAIVE_PUB,
    _run(
        "d1",
        "ref_accepted",
        _t("hidden", "hc1", "ASSERT", "pool_acquire_timeout", "3 건 시간 초과"),
        _t("public", PUBLIC_CLASS),
    ),
)


async def test_a_hand_repair_that_breaks_later_rounds_is_rolled_back() -> None:
    """손 관문 수리(공개 테스트) 뒤 다른 게이트가 못 고치고 끝나면 관문 전 통과 상태로 — 손 관문은 수리 없이 한 번 더 돌아 표시만."""
    llm, backend = _llm(
        gen_public=[
            _files((PUBLIC_PATH, PUBLIC_TEST)),
            _files((PUBLIC_PATH, PUBLIC_TEST + "// 다시 만든 판\n")),
        ]
    )
    verifier = HandVerifier(_hand_report("ASSERT"), reports=[REPORT, FAILING])
    out = await _build(verifier, llm, _java_refs())
    assert out.status == "review" and backend.calls.count("gen_public") == 2
    public = {f.path: f.content for f in out.overlays["public"].files}
    assert "다시 만든 판" not in public[PUBLIC_PATH]  # 관문 전 공개 테스트로 되돌림
    assert any(
        f.startswith("손 구현 관문(⑫) 수리 실패 —") and "복원함" in f for f in out.review_flags
    )
    assert len(verifier.hand_calls) == 2  # 첫 판정 + 되돌린 상태에서 수리 없이
    leak = [f for f in out.review_flags if "누설 의심" in f]
    assert len(leak) == 1 and "되돌린 상태" in leak[0]
    hand_records = [(r.target, r.result) for r in out.repairs if r.gate == "⑫"]
    assert hand_records == [("gen_public", "regenerated")]  # 되돌린 뒤 중복 기록 없음


async def test_a_hand_repair_whose_generator_fails_restores_and_flags() -> None:
    llm, _ = _llm(gen_public=[_files((PUBLIC_PATH, PUBLIC_TEST)), Completion("not json")])
    verifier = HandVerifier(_hand_report("ASSERT"))
    out = await _build(verifier, llm, _java_refs())
    assert out.status == "review" and len(verifier.hand_calls) == 1
    assert ("⑫", "gen_public", "failed") in [(r.gate, r.target, r.result) for r in out.repairs]
    assert (
        "손 구현 관문(⑫) 수리 실패 — 테스트를 다시 생성하지 못함. 관문 전(모든 게이트 통과) 상태로 복원함"
        in out.review_flags
    )
    assert any("누설 의심" in f and "다시 생성 실패" in f for f in out.review_flags)
    public = {f.path: f.content for f in out.overlays["public"].files}
    assert public[PUBLIC_PATH] == PUBLIC_TEST


async def test_a_trap_restore_still_drops_weak_cases_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_restore_passed 는 더 이상 케이스를 빼지 않는다 — 함정 관문(⑪) 경로도 _finish_restored 끝의 _settle_weak 가 한 번만 뺀다."""
    dropped: list[list[str]] = []
    monkeypatch.setattr(build_mod, "_drop_cases", lambda out, b, ids: dropped.append(list(ids)))

    async def no_variants(*a: Any, **k: Any) -> RepairPlan:
        return RepairPlan()

    monkeypatch.setattr(build_mod, "_variant_gate", no_variants)
    before = {"starter": Overlay(kind="starter", files=(), label="")}
    out = BuildOutcome(
        status="failed", overlays={"starter": Overlay(kind="starter", files=(), label="new")}
    )
    passed = build_mod._Passed(before, ("hc2",), [])
    got = await build_mod._finish_restored(
        out, _inputs(), {}, HandVerifier(), VariantGate(), HandGate(), passed, "이유", None
    )
    assert got.status == "review" and got.overlays == before and dropped == [["hc2"]]
    assert any(f.startswith("숨은 테스트 함정 관문(⑪) 수리 실패 — 이유") for f in got.review_flags)
