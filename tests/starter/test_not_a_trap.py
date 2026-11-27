"""#341 — 겹침·무력화 재료 판정은 막지 않는다 (2026-11-27 사용자): 숨은 케이스는 채점에 남기고 '함정 아님' 표시로 review,
승인은 '알고 받아들임' 으로만 (tests/graph/test_build_approval.py). 창 없음·과부하는 그대로 막는다.
받아들이지 않는 때 (적대적 검토 두 번 반영) — 다른 막는 표시 · 관문 수리 뒤 회차 · 다른 수리가 남은 회차 · 허용 구현이 무엇 하나라도
떨어지거나 결과가 없는 회차 · 무력화인데 흔한 구현이 숨은 케이스를 떨어진 회차 · 겹침인데 흔한 구현이 판정 밖 공개 테스트에서도 떨어진 회차.
가상 데이터만, LLM 없음 (가짜 LLM·순서대로 결과를 내는 검증기)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from task_spec.nodes.starter import build as build_mod
from task_spec.nodes.starter.build import BuildOutcome, build_and_repair
from task_spec.schemas import VerifyReport
from task_spec.schemas.package import NOT_A_TRAP
from task_spec.starter.repair import (
    CONTRACT_SUSPECT,
    MATERIAL_VERDICT,
    NEUTRALIZED,
    OVERLAP,
    REBUILD_HINT,
    RepairAction,
    RepairPlan,
    accepted_material,
    overlap_tests,
    overlap_verdict,
)
from task_spec.starter.static_check import Overlay
from tests.starter.test_contract import BACKEND_SEED
from tests.starter.test_generate import D1, _files, _inputs, _task
from tests.starter.test_repair import (
    D1_FAIL,
    D1_OK,
    NAIVE_OK,
    PROMPTS,
    ScriptedVerifier,
    _llm,
    _one_case,
    _run,
    _t,
)
from tests.starter.test_repair import HIDDEN as HIDDEN_FILES
from tests.starter.test_repair import PUBLIC as PUBLIC_ANSWER
from tests.starter.test_static_check import PKG
from tests.starter.test_trap_gate import D1_TRAP, NAIVE_PUB, PUBLIC_CLASS, TrapVerifier
from tests.starter.test_trap_gate import _llm as _trap_llm
from tests.starter.test_variant_gate import ADDED_PATH, VariantVerifier, _addition
from tests.starter.test_variant_gate import _llm as _variant_llm

NEUT = f"{MATERIAL_VERDICT}{NEUTRALIZED} — 가상 흔한 구현이 허용 설계의 장치로 hc1 통과"
OVER = overlap_verdict(["pt1"])
NO_WINDOW = f"{MATERIAL_VERDICT}가상 창 없음 (재료 확인) · {REBUILD_HINT}"
OVERLOAD = f"{MATERIAL_VERDICT}허용 구현도 버티지 못하는 부하 — 가상"

NAIVE_PASSES = _run("naive-all", "ref_naive", _t("hidden", "hc1"))
CLEAN = _one_case(NAIVE_PASSES, D1_OK)  # 허용 구현은 모두 통과, 흔한 구현은 숨은 케이스를 모두 통과
PT1_FAIL = _t("public", "pt1", "ASSERT", None, "기대 200 실제 500")
CAUGHT = _t("hidden", "hc1", "ASSERT", "pool_acquire_timeout", "32")


def _accept(
    plan: RepairPlan, report: VerifyReport = CLEAN, repairing: bool = False
) -> tuple[list[str], str]:
    return build_mod._accept_material(plan, report, repairing)


def _kept(plan: RepairPlan, report: VerifyReport = CLEAN, repairing: bool = False) -> str:
    """받아들이지 않았으면 판정은 막는 표시로 그대로 남고 이유가 있다 — 그 이유."""
    before = list(plan.unresolved)
    admitted, why = _accept(plan, report, repairing)
    assert admitted == [] and plan.unresolved == before
    return why


# ── 겹침 판정 문장 ────────────────────────────────────────────


def test_the_overlap_verdict_names_its_public_tests_and_reads_back() -> None:
    v = overlap_verdict(["pt1", "OrderHistoryPublicTest"])
    assert accepted_material(v) and overlap_tests(v) == ["pt1", "OrderHistoryPublicTest"]
    assert overlap_tests(build_mod._not_a_trap_flag(v)) == ["pt1", "OrderHistoryPublicTest"]
    assert overlap_tests(NEUT) == []


# ── 받아들일 판정 고르기 ─────────────────────────────────────


def test_only_overlap_or_neutralized_verdicts_are_admitted() -> None:
    plan = RepairPlan(unresolved=[NEUT, OVER, NEUT])
    assert _accept(plan) == ([NEUT, OVER], "")  # 같은 판정은 한 번
    assert plan.unresolved == [] and plan.passed  # 막지 않는다 — 이 회차는 통과


def test_the_flag_names_the_verdict_and_the_approval_condition() -> None:
    neut, over = build_mod._not_a_trap_flag(NEUT), build_mod._not_a_trap_flag(OVER)
    for f in (neut, over):
        assert f.startswith(NOT_A_TRAP) and MATERIAL_VERDICT not in f and "'알고 받아들임'" in f
    assert NEUTRALIZED in neut and "흔한 구현은 숨은 케이스를 모두 통과" in neut
    # 겹침은 흔한 구현이 공개 테스트에서 먼저 떨어져 숨은 케이스를 흔한 구현으로 확인하지 못했고, 함정 관문은 건너뛴다
    assert OVERLAP in over and "숨은 케이스는 흔한 구현으로 확인 못 함" in over and "건너뜀" in over


def test_any_other_blocker_keeps_the_build_stopping() -> None:
    """창 없음·과부하는 숨은 테스트를 믿을 수 없고, 계약 결함 의심은 판정이 아니다 — 하나라도 있으면 예전처럼 멈춘다 (이유는 다른 표시가 말한다)."""
    for other in (NO_WINDOW, OVERLOAD, f"{CONTRACT_SUSPECT} — 가상"):
        assert _kept(RepairPlan(unresolved=[NEUT, other])) == ""
    empty = RepairPlan()
    assert _accept(empty) == ([], "") and empty.passed


def test_not_admitted_after_a_gate_repair() -> None:
    """관문 수리 뒤 회차는 받아들이지 않는다 — 수리 전 통과 상태(흔한 구현을 잡던 숨은 테스트)로 되돌린다."""
    assert "관문 수리 뒤" in _kept(RepairPlan(unresolved=[NEUT]), repairing=True)


def test_not_admitted_while_other_repairs_remain() -> None:
    """판정과 다른 수리가 함께면 예전처럼 멈춘다 — 함께 수리하면 예산 소진·회차 상한으로 실패 기록이 달라진다 (적대적 검토 2)."""
    act = RepairAction("gen_naive", "naive-all", "⑤", "가상 사유", "가상 수정 요청")
    assert "다른 수리" in _kept(RepairPlan(actions=[act], unresolved=[NEUT]))


def test_not_admitted_when_an_accepted_implementation_fails_or_reports_nothing() -> None:
    """채점에 남길 테스트는 올바른 구현을 통과시켜야 한다 — 겹침 판정은 같은 회차에 합쳐진 다른 공개 테스트 수리를 함께 버린다.
    결과가 없는 실행(기동은 됐지만 테스트 결과 없음)도 통과가 아니다 (calibrate.accepted_clean)."""
    public_fail = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", "pt3", "ASSERT"))
    boot = _run("d1", "ref_accepted", _t("hidden", "hc1"), setup="boot_error")
    empty = VerifyReport(
        runs=[_run("starter", "starter"), NAIVE_PASSES, _run("d1", "ref_accepted")]
    )
    empty = empty.model_copy(
        update={"runs": [*empty.runs[:2], empty.runs[2].model_copy(update={"tests": []})]}
    )
    for report in (*(_one_case(NAIVE_PASSES, d1) for d1 in (D1_FAIL, public_fail, boot)), empty):
        assert "허용 구현" in _kept(RepairPlan(unresolved=[OVER]), report)


def test_a_neutralized_verdict_is_not_admitted_when_the_naive_fails_some_hidden_case() -> None:
    """무력화 판정은 ③ 이 다른 케이스의 수리(의도와 다른 사유 등)를 건너뛰고 낸다 — 흔한 구현이 숨은 케이스를 모두 통과할 때만."""
    naive = _run(
        "naive-all",
        "ref_naive",
        _t("hidden", "hc1"),
        _t("hidden", "hc2", "ASSERT", "record_missing"),
    )
    assert "숨은 케이스" in _kept(RepairPlan(unresolved=[NEUT]), _one_case(naive, D1_OK))
    # 겹침은 흔한 구현의 숨은 결과를 보지 않는다 (공개 테스트에서 먼저 떨어짐)
    assert _accept(RepairPlan(unresolved=[OVER]), _one_case(naive, D1_OK)) == ([OVER], "")


def test_an_overlap_is_admitted_only_when_the_naive_fails_just_the_named_public_tests() -> None:
    """겹침 판정은 되풀이된 공개 테스트만 적고 같은 수리에 합쳐진 다른 실패를 버린다 — 흔한 구현이 판정 밖 공개 테스트에서도
    떨어지거나 하네스 오류면 받아들이지 않는다 (적대적 검토 2-3)."""
    named = _run("naive-all", "ref_naive", CAUGHT, PT1_FAIL)
    assert _accept(RepairPlan(unresolved=[OVER]), _one_case(named, D1_OK)) == ([OVER], "")
    extra = _run("naive-all", "ref_naive", CAUGHT, PT1_FAIL, _t("public", "pt3", "ASSERT"))
    harness = _run("naive-all", "ref_naive", CAUGHT, _t("public", "pt1", "HARNESS_ERR"))
    for naive in (extra, harness):
        assert "판정 밖 공개 테스트" in _kept(
            RepairPlan(unresolved=[OVER]), _one_case(naive, D1_OK)
        )


def test_the_flags_follow_the_latest_state() -> None:
    out = BuildOutcome(status="failed", review_flags=["다른 표시", f"{NOT_A_TRAP}옛 회차 판정"])
    build_mod._set_not_a_trap(out, [f"{NOT_A_TRAP}새 회차 판정"])
    assert out.review_flags == ["다른 표시", f"{NOT_A_TRAP}새 회차 판정"]
    build_mod._set_not_a_trap(out, [])
    assert out.review_flags == ["다른 표시"]


def test_restoring_a_passed_state_restores_its_flags() -> None:
    """관문 수리 뒤 되돌리면 표시도 그때(통과한 회차) 것으로 — 수리 뒤 회차의 판정이 남지 않는다."""
    out = BuildOutcome(status="failed", review_flags=[f"{NOT_A_TRAP}수리 뒤 회차 판정"])
    snap = build_mod._Passed({}, (), [], not_trap=(f"{NOT_A_TRAP}통과 때 판정",))
    build_mod._restore_passed(out, snap, "가상 사유")
    assert [f for f in out.review_flags if f.startswith(NOT_A_TRAP)] == [
        f"{NOT_A_TRAP}통과 때 판정"
    ]


# ── 빌드 루프 ────────────────────────────────────────────────

MARKER = r"\bReentrantLock\b"
MARKED = _files(
    (
        f"{PKG}/order/OrderService.java",
        "package com.example.service.order;\n\nimport java.util.concurrent.locks.ReentrantLock;\n\n"
        "class OrderService {\n    private final ReentrantLock lock = new ReentrantLock();\n}\n",
    )
)
SEED = BACKEND_SEED.model_copy(update={"fix_markers": [MARKER]})
NOT_ADMITTED = "재료 판정(겹침·무력화)을 받아들이지 않음 — "


def _flags(out: BuildOutcome, head: str) -> list[str]:
    return [f for f in out.review_flags if f.startswith(head)]


async def _build(verifier: Any, max_rounds: int | None = None) -> tuple[BuildOutcome, Any]:
    client, backend = _llm(gen_naive=[MARKED])
    out = await build_and_repair(
        _inputs(seed=SEED),
        (D1,),
        llm=client,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=verifier,
        **({"max_rounds": max_rounds} if max_rounds is not None else {}),
    )
    return out, backend


async def _trap_build(verifier: TrapVerifier) -> tuple[BuildOutcome, Any]:
    """함정 관문(⑪)까지 도는 빌드 — 허용 설계 d1 에 함정이 적혀 있어 함정 변형을 만든다 (tests/starter/test_trap_gate)."""
    client, backend = _trap_llm(gen_naive=[MARKED], gen_hidden=[HIDDEN_FILES, HIDDEN_FILES])
    task = _task().model_copy(update={"accepted_designs": [D1_TRAP]})
    out = await build_and_repair(
        _inputs(task=task, seed=SEED),
        (D1_TRAP,),
        llm=client,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=verifier,
    )
    return out, backend


def _hidden_paths() -> set[str]:
    return {f["path"] for f in json.loads(HIDDEN_FILES.content)["files"]}


async def test_a_neutralized_trap_goes_to_review_with_its_cases_kept() -> None:
    """흔한 구현이 허용 설계의 장치로 숨은 케이스를 통과하고, 장치를 짚어 다시 만든 뒤에도 같다 — 예전에는 '재료 판정으로 멈춤'.
    이제는 review: 숨은 케이스는 그대로 채점하고, 승인자에게 '함정 아님' 표시 하나."""
    out, backend = await _build(ScriptedVerifier(CLEAN))
    assert out.status == "review", out.review_flags
    marks = _flags(out, NOT_A_TRAP)
    assert len(marks) == 1 and NEUTRALIZED in marks[0] and "ReentrantLock" in marks[0]
    assert not _flags(out, MATERIAL_VERDICT) and not _flags(out, NOT_ADMITTED)
    assert _hidden_paths() <= {
        f.path for f in out.overlays["hidden"].files
    }  # 숨은 케이스가 채점에 남는다
    assert out.dropped_cases == []
    assert backend.calls.count("gen_naive") == 2  # 처음 + 장치를 짚어 한 번 다시


async def test_a_verdict_with_a_failing_accepted_implementation_still_stops() -> None:
    """판정과 함께 허용 구현이 떨어진 회차는 받아들이지 않는다 — 예전처럼 그 회차에 재료 판정으로 멈춘다 (채점에 남길 테스트가 올바른
    구현을 떨어뜨릴 수 있다). 실패 기록은 막는 판정 문장 그대로(실패 종류 material)에 받아들이지 않은 이유 한 줄, '함정 아님' 표시는 없다."""
    verifier = ScriptedVerifier(
        CLEAN,  # 0: 흔한 구현이 장치로 통과 → 흔한 구현 다시
        _one_case(NAIVE_PASSES, D1_FAIL),  # 1: 다시 만든 뒤에도 장치 + 허용 구현 실패
        _one_case(NAIVE_OK, D1_OK),
    )
    out, _ = await _build(verifier)
    assert out.status == "failed" and len(verifier.seen) == 2
    assert any(NEUTRALIZED in f for f in _flags(out, MATERIAL_VERDICT)) and not _flags(
        out, NOT_A_TRAP
    )
    assert any("허용 구현" in f for f in _flags(out, NOT_ADMITTED))


async def test_a_flag_set_before_a_trap_gate_repair_is_withdrawn_when_the_naive_falls_after() -> (
    None
):
    """함정 관문이 숨은 테스트를 다시 만들기 전 회차에 '함정 아님' 을 받아들였는데, 다시 만든 숨은 테스트가 흔한 구현을 잡으면 표시를
    걷는다 — 끝난 상태의 표시만 승인자에게."""
    naive_pass = _run("naive-all", "ref_naive", _t("hidden", "hc1"), _t("public", PUBLIC_CLASS))
    d1 = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", PUBLIC_CLASS))
    verifier = TrapVerifier(
        "PASS",  # 1회차 뒤 함정 관문: 함정 변형이 두 번 모두 살아남음 → 숨은 테스트 다시
        "PASS",
        "ASSERT",  # 2회차 뒤: 함정 변형이 잡힘
        reports=[
            _one_case(naive_pass, d1),  # 0: 흔한 구현이 장치로 통과 → 흔한 구현 다시
            _one_case(
                naive_pass, d1
            ),  # 1: 다시 만든 뒤에도 → 무력화 판정을 받아들여 통과 → 함정 관문 수리
            _one_case(NAIVE_PUB, d1),  # 2: 다시 만든 숨은 테스트가 흔한 구현을 잡음
        ],
    )
    out, _ = await _trap_build(verifier)
    assert out.status == "review", out.review_flags
    assert ("⑪", "gen_hidden", "regenerated") in [(r.gate, r.target, r.result) for r in out.repairs]
    assert not _flags(out, NOT_A_TRAP) and not _flags(out, MATERIAL_VERDICT)


async def test_a_verdict_after_a_gate_repair_restores_the_passed_state() -> None:
    """관문 수리 뒤 회차에 무력화가 나면 받아들이지 않고 수리 전 통과 상태(흔한 구현을 잡던 숨은 테스트)로 되돌린다 — 예전과 같다.
    받아들이면 흔한 구현을 잡던 판을 버리고 '구별되지 않음' 을 승인자에게 받아들이라고 하게 된다 (적대적 검토 1)."""
    naive_pass = _run("naive-all", "ref_naive", _t("hidden", "hc1"), _t("public", PUBLIC_CLASS))
    d1 = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", PUBLIC_CLASS))
    verifier = TrapVerifier(
        "PASS",
        "PASS",
        "ASSERT",
        reports=[
            _one_case(naive_pass, d1),  # 0: 흔한 구현이 장치로 통과 → 흔한 구현 다시
            _one_case(
                NAIVE_PUB, d1
            ),  # 1: 흔한 구현이 잡힘 → 통과 → 함정 관문: 살아남음 → 숨은 테스트 다시
            _one_case(naive_pass, d1),  # 2: 새 숨은 테스트를 흔한 구현이 장치로 통과 → 무력화 판정
        ],
    )
    out, _ = await _trap_build(verifier)
    assert out.status == "review", out.review_flags
    assert any("복원함" in f for f in out.review_flags)
    assert not _flags(out, NOT_A_TRAP)


async def test_an_overlap_goes_to_review_with_the_public_test_named() -> None:
    """흔한 구현만 같은 공개 테스트에서 떨어지고, 흔한 구현·공개 테스트를 다시 만든 뒤에도 같다 — 겹침. 예전에는 '재료 판정으로
    멈춤', 이제는 review 와 '함정 아님' 표시 (그 요구는 공개 테스트가 이미 잰다)."""
    naive = _run("naive-all", "ref_naive", CAUGHT, PT1_FAIL)
    d1 = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", "pt1"))
    out, backend = await _build(ScriptedVerifier(_one_case(naive, d1)))
    assert out.status == "review", out.review_flags
    marks = _flags(out, NOT_A_TRAP)
    assert len(marks) == 1 and OVERLAP in marks[0] and overlap_tests(marks[0]) == ["pt1"]
    assert not _flags(out, MATERIAL_VERDICT)
    assert backend.calls.count("gen_public") == 2  # 처음 + 누설 의심으로 한 번 다시


async def test_an_overlap_skips_the_trap_gate() -> None:
    """겹침이면 함정 변형도 겹친 공개 테스트에서 떨어져 함정 관문이 '무효' 로만 판정한다 — 건너뛰고 표시에 적는다 (적대적 검토 2-1).
    예전 판단이면 함정 변형을 다시 만들고 '모두 무효라 시험하지 못함' 으로 끝났다."""
    naive = _run(
        "naive-all",
        "ref_naive",
        CAUGHT,
        _t("public", PUBLIC_CLASS, "ASSERT", None, "기대 200 실제 500"),
    )
    d1 = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", PUBLIC_CLASS))
    out, backend = await _trap_build(TrapVerifier("PASS", reports=[_one_case(naive, d1)]))
    assert out.status == "review", out.review_flags
    marks = _flags(out, NOT_A_TRAP)
    assert len(marks) == 1 and OVERLAP in marks[0] and "건너뜀" in marks[0]
    assert "gen_trap" not in backend.calls and not out.traps


class _OverlapVariantVerifier(VariantVerifier):
    """공개 테스트 변별 관문(⑩)이 더한 클래스를 확인하는 실행에서도 흔한 구현은 겹친 공개 테스트에서 떨어진다 — 검증 회차와 같다."""

    async def verify_variants(
        self, starter: Overlay, variants: Sequence[Overlay], public: Overlay
    ) -> VerifyReport:
        rep = await super().verify_variants(starter, variants, public)
        if not variants or variants[0].label.startswith("var-"):
            return rep
        runs = [
            r.model_copy(
                update={
                    "tests": [
                        t.model_copy(update={"result": "ASSERT"}) if t.name == PUBLIC_CLASS else t
                        for t in r.tests
                    ]
                }
            )
            if r.kind == "ref_naive"
            else r
            for r in rep.runs
        ]
        return VerifyReport(runs=runs)


async def test_an_overlap_build_keeps_the_public_cases_the_variant_gate_adds() -> None:
    """⑩ 은 더한 공개 테스트 클래스를 참조 구현(흔한·허용)이 모두 통과할 때만 남긴다 — 겹침을 받아들인 빌드의 흔한 구현은 겹친 공개
    테스트에서 원래 떨어지므로 그 실패는 더한 테스트 탓으로 보지 않는다 (예전 판단이면 더한 클래스를 모두 버렸다, 적대적 검토 2-1)."""
    naive = _run(
        "naive-all",
        "ref_naive",
        CAUGHT,
        _t("public", PUBLIC_CLASS, "ASSERT", None, "기대 200 실제 500"),
    )
    d1 = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", PUBLIC_CLASS))
    llm, backend = _variant_llm(
        gen_public=[PUBLIC_ANSWER, PUBLIC_ANSWER, _addition(touch_old=False)]
    )
    out = await build_and_repair(
        _inputs(),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=_OverlapVariantVerifier(_one_case(naive, d1), "PASS", "ASSERT"),
    )
    assert out.status == "review", out.review_flags
    assert len(_flags(out, NOT_A_TRAP)) == 1
    assert ADDED_PATH in {f.path for f in out.overlays["public"].files}
    assert not any("기존 테스트가 실패함" in f for f in out.review_flags)
    assert backend.calls.count("gen_public") == 3  # 처음 + 누설 의심 한 번 + ⑩ 더하기


async def test_an_overlap_that_swallowed_another_public_repair_still_fails() -> None:
    """겹침 판정은 같은 회차에 합쳐진 공개 테스트 수리를 통째로 버린다 — 허용 구현이 다른 공개 테스트에서 떨어진 채 review 로 가면
    안 된다. 받아들이지 않고 예전처럼 재료 판정으로 멈춘다 (적대적 검토 1-2)."""
    naive = _run("naive-all", "ref_naive", CAUGHT, PT1_FAIL, _t("public", "pt3"))
    d1 = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", "pt1"), _t("public", "pt3"))
    pt3_fail = _t("public", "pt3", "ASSERT", None, "기대 3건 실제 2건")
    naive2 = _run("naive-all", "ref_naive", CAUGHT, PT1_FAIL, pt3_fail)
    d1_broken = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", "pt1"), pt3_fail)
    verifier = ScriptedVerifier(
        _one_case(naive, d1), _one_case(naive, d1), _one_case(naive2, d1_broken)
    )
    out, _ = await _build(verifier)
    assert out.status == "failed"
    assert any(OVERLAP in f for f in _flags(out, MATERIAL_VERDICT)) and not _flags(out, NOT_A_TRAP)
    assert _flags(out, NOT_ADMITTED)
