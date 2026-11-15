"""숨은 테스트 함정 관문 ⑪ (#171) — 허용 설계의 함정에 빠진 구현이 숨은 테스트에서 떨어지는가 (starter/traps.py · build._trap_gate)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from task_spec.llm import Completion, LLMClient
from task_spec.nodes.starter.build import build_and_repair
from task_spec.schemas import AcceptedDesign, GeneratedFile, GenTrapOutput, VerifyReport
from task_spec.starter.static_check import Overlay
from task_spec.starter.traps import (
    TrapSpec,
    TrapTarget,
    build_traps,
    design_trap,
    judge_traps,
    trap_note,
    trap_targets,
)
from tests.starter.test_generate import PKG, SERVICE, STARTER, _files, _inputs, _task
from tests.starter.test_repair import HIDDEN as HIDDEN_ANSWER
from tests.starter.test_repair import (
    NAIVE_OK,
    PROMPTS,
    RoutedBackend,
    _no_sleep,
    _one_case,
    _run,
    _t,
)
from tests.starter.test_repair import PUBLIC as PUBLIC_ANSWER
from tests.starter.test_repair import SVC as SVC_ANSWER

SVC_PATH = f"{PKG}/order/OrderService.java"
TRAPPED = (
    "package com.example.service.order;\n\nclass OrderService { boolean checkThenSave = true; }\n"
)
PUBLIC_CLASS = "OrderHistoryPublicTest"
D1_TRAP = AcceptedDesign(
    design_id="d1",
    summary="요청 번호 유일 제약으로 한 번만 처리",
    conditions="같은 트랜잭션에서 기록 — 함정: 조회한 뒤 없으면 저장 (동시에 온 두 요청이 둘 다 없다고 봄)",
    trap_ids=["t1"],
)


def _trapped(*items: tuple[str, list[tuple[str, str]]]) -> Completion:
    return Completion(
        json.dumps(
            {
                "variants": [
                    {"note": note, "files": [{"path": p, "content": c} for p, c in fs]}
                    for note, fs in items
                ]
            },
            ensure_ascii=False,
        )
    )


class TrapBackend(RoutedBackend):
    MARKERS = {
        **RoutedBackend.MARKERS,
        "gen_trap": "**그 설계에 적힌 함정에 빠진 구현**",
        "gen_variant": "**공개 요구 하나를 어긴 구현**",
    }


def _llm(**over: list[Completion]) -> tuple[LLMClient, TrapBackend]:
    routes = {
        "gen_public": [PUBLIC_ANSWER],
        "gen_starter": [_files(*STARTER)],
        "gen_hidden": [HIDDEN_ANSWER],
        "gen_naive": [SVC_ANSWER],
        "gen_accepted": [SVC_ANSWER],
        "gen_trap": [_trapped(("조회한 뒤 없으면 저장", [(SVC_PATH, TRAPPED)]))],
        "gen_variant": [Completion(json.dumps({"variants": []}))],
    } | over
    backend = TrapBackend(**routes)
    return LLMClient({"opencode": backend, "openai": backend}, sleep=_no_sleep), backend  # type: ignore[dict-item]


class TrapVerifier:
    """검증 회차는 늘 통과. 변형 실행 경로로 함정 변형(trap-)이 오면 hidden_results 를 차례로, 공개 변형(var-)은 잡힘."""

    def __init__(self, *hidden_results: str, reports: list[VerifyReport] | None = None) -> None:
        self.hidden_results = list(hidden_results)
        self.trap_calls: list[list[str]] = []
        self.reports = list(reports or [REPORT])

    async def verify(self, *a: Any, **k: Any) -> VerifyReport:
        return self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]

    async def verify_variants(
        self,
        starter: Overlay,
        variants: Sequence[Overlay],
        public: Overlay,
        hidden: Overlay | None = None,
    ) -> VerifyReport:
        if variants and variants[0].label.startswith("trap-"):
            assert hidden is not None  # 함정 변형은 숨은 테스트와 함께 돈다
            self.trap_calls.append([v.label for v in variants])
            got = (
                self.hidden_results.pop(0)
                if len(self.hidden_results) > 1
                else self.hidden_results[0]
            )
            return VerifyReport(
                runs=[
                    _run(
                        v.label,
                        "ref_variant",
                        _t("public", PUBLIC_CLASS),
                        _t("hidden", "hc1", got, "duplicate_record" if got == "ASSERT" else None),
                    )
                    for v in variants
                ]
            )
        return VerifyReport(
            runs=[
                _run(v.label, "ref_variant", _t("public", PUBLIC_CLASS, "ASSERT")) for v in variants
            ]
        )


D1_OK = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", PUBLIC_CLASS))
NAIVE_PUB = NAIVE_OK.model_copy(
    update={
        "tests": [*NAIVE_OK.tests, *_run("x", "ref_naive", _t("public", PUBLIC_CLASS)).tests[1:]]
    }
)
REPORT = _one_case(NAIVE_PUB, D1_OK)


def _trap_inputs() -> Any:
    return _inputs(task=_task().model_copy(update={"accepted_designs": [D1_TRAP]}))


async def _build(verifier: Any, llm: LLMClient) -> Any:
    return await build_and_repair(
        _trap_inputs(),
        (D1_TRAP,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=verifier,
    )


def test_design_trap_reads_the_pitfall_sentence() -> None:
    assert (
        design_trap(D1_TRAP.conditions)
        == "조회한 뒤 없으면 저장 (동시에 온 두 요청이 둘 다 없다고 봄)"
    )
    assert design_trap("조건: 처리 실패 때 그 줄을 어떻게 풀지 밝힘") is None
    assert (
        design_trap("함정: 읽어서 확인한 뒤 따로 UPDATE 조건: 같은 잠금")
        == "읽어서 확인한 뒤 따로 UPDATE"
    )


def test_trap_targets_point_at_the_cases_of_the_hidden_requirement() -> None:
    b = _trap_inputs()
    got = trap_targets([D1_TRAP], b.plan)
    assert [t.design_id for t in got] == ["d1"]
    assert set(got[0].cases) == {c.case_id for c in b.plan.cases if c.trap_id == "t1"}
    assert trap_targets([D1_TRAP.model_copy(update={"conditions": "조건만"})], b.plan) == []


def test_build_and_judge_traps() -> None:
    target = TrapTarget(
        "d1", "요약", D1_TRAP.conditions, "조회한 뒤 없으면 저장", ("t1",), ("hc1",)
    )
    base = Overlay(
        kind="ref_accepted", files=(GeneratedFile(path=SVC_PATH, content=SERVICE),), label="d1"
    )
    out = GenTrapOutput.model_validate(
        {
            "variants": [
                {"note": "조회 뒤 저장", "files": [{"path": SVC_PATH, "content": TRAPPED}]},
                {"note": "같음", "files": [{"path": SVC_PATH, "content": SERVICE}]},
            ]
        }
    )
    kept, dropped = build_traps(target, out, base, {SVC_PATH: SERVICE})
    assert [s.label for _, s in kept] == ["trap-d1"] and len(dropped) == 1
    specs = [
        TrapSpec("trap-d1", "d1", "조회한 뒤 없으면 저장", "조회 뒤 저장", ("hc1",), 1),
        TrapSpec("trap-d1-2", "d1", "조회한 뒤 없으면 저장", "다른 길", ("hc1",), 1),
        TrapSpec("trap-d1-3", "d1", "조회한 뒤 없으면 저장", "공개도 깨짐", ("hc1",), 1),
    ]
    report = VerifyReport(
        runs=[
            _run(
                "trap-d1",
                "ref_variant",
                _t("public", PUBLIC_CLASS),
                _t("hidden", "hc1", "ASSERT", "duplicate_record"),
            ),
            _run("trap-d1-2", "ref_variant", _t("public", PUBLIC_CLASS), _t("hidden", "hc1")),
            _run(
                "trap-d1-3",
                "ref_variant",
                _t("public", PUBLIC_CLASS, "ASSERT"),
                _t("hidden", "hc1", "ASSERT"),
            ),
        ]
    )
    got = {r.label: r.verdict for r in judge_traps(report, specs)}
    assert got == {"trap-d1": "caught", "trap-d1-2": "survived", "trap-d1-3": "invalid"}
    note = trap_note([r for r in judge_traps(report, specs) if r.verdict == "survived"], specs)
    assert "숨은 케이스 hc1" in note and "조회한 뒤 없으면 저장" in note and "class " not in note


async def test_trap_gate_passes_when_hidden_tests_catch_the_trap() -> None:
    llm, backend = _llm()
    verifier = TrapVerifier("ASSERT")
    out = await _build(verifier, llm)
    assert out.status == "review" and backend.calls.count("gen_hidden") == 1
    assert [(r.label, r.verdict) for r in out.traps] == [("trap-d1", "caught")]
    trap_prompt = next(u for n, u in backend.users if n == "gen_trap")
    assert "조회한 뒤 없으면 저장" in trap_prompt


async def test_trap_gate_rebuilds_the_hidden_case_for_a_surviving_trap() -> None:
    """살아남은 함정 → 그 숨은 요구의 케이스를 다시 → 모든 게이트를 다시 돌고 같은 함정 변형을 다시 돌려(다시 만들지 않음) 잡히면 통과."""
    llm, backend = _llm(gen_hidden=[HIDDEN_ANSWER, HIDDEN_ANSWER])
    verifier = TrapVerifier("PASS", "PASS", "ASSERT")
    out = await _build(verifier, llm)
    assert out.status == "review" and out.rounds == 1
    # 첫 회차: 돌리고 살아남아 한 번 더(경합형 — 두 번 다 살아남아야), 수리 뒤 회차: 같은 변형을 다시 (다시 만들지 않음)
    assert backend.calls.count("gen_trap") == 1 and verifier.trap_calls == [["trap-d1"]] * 3
    assert [(r.gate, r.target, r.result) for r in out.repairs] == [
        ("⑪", "gen_hidden", "regenerated")
    ]
    repair_prompt = [u for n, u in backend.users if n == "gen_hidden"][-1]
    assert "함정" in repair_prompt and "hc1" in repair_prompt
    assert [r.verdict for r in out.traps] == ["caught"]
    assert not any("가르지 못함" in f for f in out.review_flags)


async def test_trap_gate_flags_a_trap_that_still_survives() -> None:
    llm, backend = _llm(gen_hidden=[HIDDEN_ANSWER, HIDDEN_ANSWER])
    out = await _build(TrapVerifier("PASS"), llm)
    assert out.status == "review"
    assert [r.result for r in out.repairs] == ["regenerated", "skipped"]
    assert any("함정을 가르지 못함" in f and "다시 만든 뒤에도" in f for f in out.review_flags)


async def test_trap_gate_restores_the_passing_state_when_the_repair_fails() -> None:
    """숨은 테스트 수리가 실패하면 관문 전 통과 상태로 — 관문은 막지 않는다."""
    bad = _files(("src/main/java/com/example/service/Nope.java", "class Nope {}\n"))
    llm, _ = _llm(gen_hidden=[HIDDEN_ANSWER, bad, bad, bad])
    out = await _build(TrapVerifier("PASS"), llm)
    assert out.status == "review"
    assert any("수리 실패" in f and "되돌림" in f for f in out.review_flags)
    assert any("함정을 가르지 못함" in f for f in out.review_flags)


async def test_designs_without_a_trap_skip_the_gate() -> None:
    llm, backend = _llm()
    out = await build_and_repair(
        _inputs(),
        (_task().accepted_designs[0],),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="t",
        verifier=TrapVerifier("ASSERT"),
    )
    assert out.status == "review" and "gen_trap" not in backend.calls and not out.traps


async def test_a_race_trap_caught_on_the_second_run_needs_no_repair() -> None:
    """경합형 함정(조회 뒤 저장)은 한 번에 안 드러날 수 있다 — 한 번 더 돌려 잡히면 숨은 테스트를 고치지 않는다 (리뷰 2026-11-13)."""
    llm, backend = _llm()
    verifier = TrapVerifier("PASS", "ASSERT")
    out = await _build(verifier, llm)
    assert out.status == "review" and not out.repairs and backend.calls.count("gen_hidden") == 1
    assert [r.verdict for r in out.traps] == ["caught"]
    assert verifier.trap_calls == [["trap-d1"], ["trap-d1"]]


def test_only_assertions_on_the_designs_cases_count_as_caught() -> None:
    """하네스 오류·다른 숨은 요구의 케이스·뺄 케이스(weak)는 '잡힘'의 근거가 아니다 (리뷰 2026-11-13)."""
    spec = TrapSpec("trap-d1", "d1", "조회한 뒤 없으면 저장", "조회 뒤 저장", ("hc1",), 1)

    def verdict(*hidden: dict[str, Any], weak: tuple[str, ...] = ()) -> tuple[str, str]:
        report = VerifyReport(
            runs=[_run("trap-d1", "ref_variant", _t("public", PUBLIC_CLASS), *hidden)]
        )
        r = judge_traps(report, [spec], weak)[0]
        return r.verdict, r.detail

    assert verdict(_t("hidden", "hc1", "ASSERT", "duplicate_record"))[0] == "caught"
    assert verdict(_t("hidden", "hc1", "HARNESS_ERR"))[0] == "invalid"
    assert verdict(_t("hidden", "hc1"), _t("hidden", "hc2", "ASSERT"))[1].startswith(
        "다른 숨은 요구의 케이스만"
    )
    assert (
        verdict(_t("hidden", "hc1", "ASSERT"), _t("hidden", "hc2"), weak=("hc1",))[0] == "survived"
    )


async def test_a_design_whose_traps_are_all_invalid_is_flagged() -> None:
    llm, backend = _llm()
    verifier = TrapVerifier("HARNESS_ERR")
    out = await _build(verifier, llm)
    assert out.status == "review" and [r.verdict for r in out.traps] == ["invalid"]
    assert any("모두 무효라 시험하지 못함" in f for f in out.review_flags)
    # 무효 이유를 붙여 한 번만 다시 만든다 (그 구현이 바뀔 때까지)
    assert backend.calls.count("gen_trap") == 2 and len(verifier.trap_calls) == 2


async def test_an_invalid_trap_is_remade_with_the_reason() -> None:
    """2026-11-13 풀 고갈 알파: 함정 변형이 공개 테스트까지 깨 시험하지 못함 — 무효 이유를 붙여 다시 만든 변형으로 시험한다."""
    llm, backend = _llm()
    verifier = TrapVerifier("HARNESS_ERR", "ASSERT")
    out = await _build(verifier, llm)
    assert [r.verdict for r in out.traps] == ["caught"], out.traps
    assert not any("모두 무효" in f for f in out.review_flags)
    notes = [u for name, u in backend.users if name == "gen_trap"]
    assert len(notes) == 2 and "앞 답의 변형을 시험하지 못했다" in notes[1]
    assert "하네스 오류" in notes[1]  # 무효 이유를 그대로


async def test_a_dropped_trap_is_asked_again_with_the_reason() -> None:
    """2026-11-13 알파 4건 중 2건: 함정 변형이 33·40줄로 상한에 걸려 버려짐 — 상한은 두고 버린 이유를 붙여 한 번 더 묻는다."""
    stray = _trapped(("다른 파일에 함정", [(f"{PKG}/order/Nope.java", TRAPPED)]))
    llm, backend = _llm(
        gen_trap=[stray, _trapped(("조회한 뒤 없으면 저장", [(SVC_PATH, TRAPPED)]))]
    )
    out = await _build(TrapVerifier("ASSERT"), llm)
    assert [r.verdict for r in out.traps] == ["caught"], out.traps
    notes = [u for name, u in backend.users if name == "gen_trap"]
    assert (
        len(notes) == 2
        and "앞 답의 변형을 쓰지 못했다" in notes[1]
        and "구현에 없는 파일" in notes[1]
    )
    assert any("구현에 없는 파일" in x for x in out.trap_dropped)  # 첫 답을 버린 이유도 남긴다


async def test_a_trap_without_its_cases_is_flagged_not_repaired() -> None:
    """그 숨은 요구의 케이스를 못 찾으면 고치지 않는다 — 케이스 id 가 없으면 숨은 테스트 전체를 다시 만든다 (리뷰 2026-11-13)."""
    llm, backend = _llm()
    lonely = D1_TRAP.model_copy(update={"trap_ids": ["t9"]})
    b = _inputs(task=_task().model_copy(update={"accepted_designs": [lonely]}))
    out = await build_and_repair(
        b, (lonely,), llm=llm, prompts=PROMPTS, ontology_version="t", verifier=TrapVerifier("PASS")
    )
    assert out.status == "review" and backend.calls.count("gen_hidden") == 1
    assert any("케이스를 찾지 못해 고치지 않음" in f for f in out.review_flags)


async def test_a_round_broken_after_the_trap_repair_is_repaired_normally() -> None:
    """함정 수리 뒤 회차가 깨져도 회차가 남으면 기존 수리(④ — 그 허용 구현을 다시)를 하고, 고쳐지면 되돌리지 않는다 (리뷰 2026-11-13)."""
    llm, _ = _llm(gen_hidden=[HIDDEN_ANSWER, HIDDEN_ANSWER], gen_accepted=[SVC_ANSWER, SVC_ANSWER])
    d1_fails = _run(
        "d1",
        "ref_accepted",
        _t("hidden", "hc1", "ASSERT", "duplicate_record", "기록 2건"),
        _t("public", PUBLIC_CLASS),
    )
    broken = _one_case(NAIVE_PUB, d1_fails)
    verifier = TrapVerifier("PASS", "PASS", "ASSERT", reports=[REPORT, broken, REPORT])
    out = await _build(verifier, llm)
    assert out.status == "review" and out.rounds == 2
    assert [(r.gate, r.target) for r in out.repairs][:2] == [
        ("⑪", "gen_hidden"),
        ("④", "gen_accepted"),
    ]
    assert not any("되돌림" in f for f in out.review_flags)


def test_builds_make_every_accepted_design_by_default() -> None:
    from task_spec.builder.settings import BuilderSettings

    assert BuilderSettings.model_fields["build_designs"].default is None  # 허용 설계 전부 (#171)
