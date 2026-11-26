"""빌드 조절값 보정 (#301 PR 2, starter/calibrate.py · repair._gate3 · build._calibrate) — 가상 데이터·가짜 검증기만.

흔한 구현이 허용 설계 장치 없이 숨은 케이스를 통과하면 다시 만들기 전에 그 사유의 숨긴 조절값(tunes)만 바꿔 흔한 구현·허용 구현을
다시 돌리고, '허용 구현은 모두 통과·흔한 구현은 의도한 사유로 실패'하는 창을 찾는다. 결과는 대기열 모형으로 흉내 낸다 —
동시 요청이 풀 크기씩 묶음으로 차례를 기다리고, 요청 하나가 연결을 쥔 채 '문장 수 × 지연' 동안 머문다 (open-in-view 기본 켬).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from task_spec.nodes.starter import build as build_mod
from task_spec.nodes.starter.build import build_and_repair
from task_spec.schemas import VerifyReport
from task_spec.schemas.domain import Artifact, TaskItem
from task_spec.schemas.verify_report import TargetRun
from task_spec.starter.calibrate import (
    FLAG,
    Knobs,
    Trial,
    base_label,
    judge,
    knobs,
    pick,
    search,
    spec_note,
    spec_values,
)
from task_spec.starter.repair import MATERIAL_VERDICT, RepairPlan, assign
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template, load_template
from tests.starter.test_generate import D1, _files, _inputs, _task
from tests.starter.test_repair import MISSED, OVERLAYS, PLAN2, PROMPTS, _llm, _report, _run, _t
from tests.starter.test_static_check import CASES, HC1, PLAN
from tests.starter.test_static_check import JAVA as JAVA_V1

JAVA = "backend/java-spring-boot3-jpa"
V4 = load_template(JAVA, 4).manifest
POOL = "pool_acquire_timeout"
POOL_PLAN = PLAN2.model_copy(update={"harness": {"pool_size": 20, "concurrency": 100}})


def _queue(naive_statements: int = 4, accepted_statements: int = 1, waves: int = 5) -> Any:
    """대기열 모형 — 풀 20·동시 100 이면 20건씩 5묶음, 마지막 묶음의 대기 = (묶음 − 1) × 문장 수 × 지연.
    #301 실측(대기 한도 250·지연 30 → 흔한 구현 114/300 실패, 400·50 → 122/300, 허용 구현은 모두 통과)과 ±5% 로 맞는다."""

    async def run(values: dict[str, int]) -> Trial:
        limit, delay = values["connection_timeout_ms"], values.get("query_delay_ms", 20)
        naive = (waves - 1) * naive_statements * delay
        accepted = (waves - 1) * accepted_statements * delay
        return Trial(dict(values), caught=naive > limit, clean=accepted <= limit)

    return run


# ── 무엇을 바꿀지 ───────────────────────────────────────────────────


def test_knobs_are_the_hidden_tunes_the_material_did_not_list() -> None:
    k = knobs(POOL, V4, POOL_PLAN)
    assert k == Knobs(
        POOL,
        "connection_timeout_ms",
        (2000, 1000, 500, 250),
        "query_delay_ms",
        (20, 50),
        looser=(4000, 8000),
    )
    # 계획이 지연을 정했으면 그 값부터 (계약 단계가 숨긴 값은 빼지만 재료가 정한 값일 수 있다)
    delayed = POOL_PLAN.model_copy(update={"harness": {**POOL_PLAN.harness, "query_delay_ms": 30}})
    assert knobs(POOL, V4, delayed) == replace(k, loads=(30, 50))
    # 재료가 지연을 나열했으면(명세에 보이는 값) 한도만 잰다
    assert knobs(POOL, V4, POOL_PLAN, ["query_delay_ms"]) == Knobs(
        POOL, "connection_timeout_ms", (2000, 1000, 500, 250), looser=(4000, 8000)
    )


@pytest.mark.parametrize(
    ("reason", "listed", "version"),
    [
        (POOL, ["connection_timeout_ms"], 4),  # 재료가 대기 한도를 나열 — 명세에 보이는 운영 조건
        ("request_timeout", [], 4),  # 한도 사다리를 아직 정하지 않은 값(request_timeout_s)뿐
        (POOL, [], 2),  # tunes 가 없는 판
    ],
)
def test_no_knobs_when_nothing_may_be_tuned(reason: str, listed: list[str], version: int) -> None:
    m = load_template(JAVA, version).manifest
    assert knobs(reason, m, POOL_PLAN, listed) is None


# ── 창 찾기 ─────────────────────────────────────────────────────────


async def test_the_hikari_floor_squeezes_the_window_until_the_delay_goes_up() -> None:
    """#301 대기열 모형: 지연 20 이면 흔한 구현 최대 대기 약 320ms — 하한 250 에서만 떨어져 창 [250, 250] (좁음), 창 끝 500 을
    두 번 더 돌려도 그대로. 지연 50 이면 [250, 500] → 기하 중간값 350 을 두 번 확인. 조합 12회 (사다리 4 + 창 끝 2 + 사다리 4 + 확인 2)."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _queue(), ["hc1"])
    assert cal.status == "calibrated" and cal.window == (250, 500)
    assert dict(cal.values) == {"connection_timeout_ms": 350, "query_delay_ms": 50}
    assert [t.values["connection_timeout_ms"] for t in cal.trials] == [
        *(2000, 1000, 500, 250, 500, 500),
        *(2000, 1000, 500, 250, 350, 350),
    ]
    assert [t.values["query_delay_ms"] for t in cal.trials] == [20] * 6 + [50] * 6
    assert cal.flag() == (
        f"{FLAG}connection_timeout_ms 350·query_delay_ms 50 — 흔한 구현이 hc1 에서 pool_acquire_timeout 로 실패, "
        "허용 구현 모두 통과 (connection_timeout_ms 창 250~500, 시험 12회)"
    )


async def test_a_wide_window_at_the_planned_delay_stops_early() -> None:
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _queue(naive_statements=10), ["hc1"])  # 지연 20 에서 흔한 구현 대기 800ms
    # 사다리 4 + 확인 2
    assert cal.status == "calibrated" and cal.window == (250, 500) and len(cal.trials) == 6
    assert dict(cal.values) == {"connection_timeout_ms": 350, "query_delay_ms": 20}


async def test_no_window_when_the_accepted_design_fails_where_the_naive_does() -> None:
    """흔한 구현은 떨어지는데 그 한도에서 허용 구현도 떨어진다 — 재료가 조절값으로 재현되지 않음 (사람). 허용 구현이 떨어지기
    시작한 한도 아래는 보지 않는다."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _queue(accepted_statements=3), ["hc1"])
    assert cal.status == "no_window" and not cal.values
    # 지연 20: 끝까지 (흔한 구현은 250 에서만 실패) + 창 끝 500 두 번 더 · 지연 50: 허용 구현이 500 에서 떨어져 250 은 보지 않음,
    # 창 끝 1000 에서 떨어져도 허용 구현이 통과한 가장 작은 한도가 1000 이라 2배에 못 미쳐 다시 돌리지 않는다
    assert [t.values["connection_timeout_ms"] for t in cal.trials] == [
        *(2000, 1000, 500, 250, 500, 500),
        *(2000, 1000, 500),
    ]
    assert "2배 창 없음" in cal.verdict() and "connection_timeout_ms: 500, 250" in cal.verdict()
    worse = await search(
        k, _queue(accepted_statements=6), ["hc1"]
    )  # 지연 50 에서 1000 부터 허용 구현 실패
    assert [t.values["connection_timeout_ms"] for t in worse.trials][4:] == [2000, 1000]


async def test_a_naive_that_never_falls_goes_back_to_regeneration() -> None:
    """어느 조합에서도 흔한 구현이 안 떨어지면 흔한 구현이 함정을 안 가졌을 수 있다 — 재료 판정이 아니라 기존 재생성 경로로."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _queue(naive_statements=1), ["hc1"])
    # 떨어진 한도가 없으면 창 끝도 없다
    assert cal.status == "naive_untouched" and len(cal.trials) == 8


def _flaky(base: Any, script: dict[tuple[int, int], list[dict[str, bool]]]) -> Any:
    """대기열 모형 위에서 (대기 한도, 지연) 조합의 앞 몇 번 결과만 바꾼다 — 시험 한 번의 흔들림 (#308 운영 b7)."""
    left = {key: list(changes) for key, changes in script.items()}

    async def run(values: dict[str, int]) -> Trial:
        trial = await base(values)
        changes = left.get((values["connection_timeout_ms"], values.get("query_delay_ms", 20)))
        return replace(trial, **changes.pop(0)) if changes else trial

    return run


async def test_a_naive_that_passes_the_edge_once_is_tried_again() -> None:
    """#308 운영 b7: 지연 50·500ms 에서 흔한 구현이 한 번 통과해 창이 [250, 250] 으로 줄고 빌드가 실패했다 (같은 조건 재생은 5번 중
    3번 실패). 창 끝 한도를 다시 돌려 떨어지면 창은 [250, 500] — 고른 350 을 두 번 확인하고 보정한다."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _flaky(_queue(), {(500, 50): [{"caught": False}]}), ["hc1"])
    assert cal.status == "calibrated" and cal.window == (250, 500)
    assert dict(cal.values) == {"connection_timeout_ms": 350, "query_delay_ms": 50}
    tried = [t.values["connection_timeout_ms"] for t in cal.trials]
    assert tried[6:] == [2000, 1000, 500, 250, 500, 350, 350]


async def test_a_picked_value_the_naive_passes_is_not_calibrated() -> None:
    """고른 값의 확인 시험에서 흔한 구현이 통과하면 그 값으로 채점하지 않는다 — 다른 부하 단계가 없으면 재료 판정 (사람)."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _flaky(_queue(), {(350, 50): [{"caught": False}]}), ["hc1"])
    assert cal.status == "no_window" and not cal.values and cal.unconfirmed == (350,)
    tried = [t.values["connection_timeout_ms"] for t in cal.trials]
    assert tried[6:] == [2000, 1000, 500, 250, 350]
    assert "창에서 고른 350 는 확인 시험에서 흔한 구현 통과 또는 허용 구현 실패" in cal.verdict()


async def test_the_edge_is_not_widened_when_the_accepted_design_fails_there() -> None:
    """창 끝을 다시 돌렸더니 흔한 구현은 떨어졌지만 허용 구현도 떨어졌다 — 허용 구현이 흔들리는 한도는 창에 넣지 않는다."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    script = {(500, 50): [{"caught": False}, {"caught": True, "clean": False}]}
    cal = await search(k, _flaky(_queue(), script), ["hc1"])
    assert cal.status == "no_window" and not cal.values
    tried = [t.values["connection_timeout_ms"] for t in cal.trials]
    assert tried[6:] == [2000, 1000, 500, 250, 500]


@pytest.mark.parametrize(
    ("lo", "hi", "want"), [(250, 500, 350), (250, 1000, 500), (500, 2000, 1000), (250, 2000, 710)]
)
def test_pick_is_the_geometric_middle(lo: int, hi: int, want: int) -> None:
    assert pick(lo, hi) == want


async def test_looser_limits_are_tried_when_the_accepted_design_fails_at_the_top_rung() -> None:
    """#337 7절: 명세 부하가 무거우면 가장 느슨한 한도(2000)에서도 허용 구현이 떨어진다 (#334 알파 2·3회차, 동시/풀 82/20·124/20) —
    예전에는 사다리를 더 내려가지 않고 2회 만에 끝났다. 더 느슨한 한도(4000·8000)부터 다시 잰다."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    # 지연 20: 허용 구현 대기 2400(2000 에서 실패) · 흔한 구현 9600 → 4000·8000 에서 허용 구현 통과·흔한 구현 실패 → 창 [4000, 8000]
    cal = await search(k, _queue(naive_statements=120, accepted_statements=30), ["hc1"])
    assert cal.status == "calibrated" and cal.window == (4000, 8000)
    assert [t.values["connection_timeout_ms"] for t in cal.trials] == [2000, 4000, 8000, 5660, 5660]
    assert dict(cal.values) == {"connection_timeout_ms": 5660, "query_delay_ms": 20}


async def test_accepted_failing_at_every_limit_is_a_spec_load_verdict() -> None:
    """#337 7절: 더 느슨한 한도로도 허용 구현이 떨어지면 조절값으로는 가를 수 없다 — 명세 수치(동시 요청·풀 크기) 문제다 (시험 불가 꼴,
    '재료 판정: ' 머리로 무력화·겹침과 같은 갈래). 예전에는 '흔한 구현이 안 떨어짐'으로 재생성 예산을 다 썼다."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _queue(naive_statements=200, accepted_statements=120), ["hc1"])
    assert cal.status == "overloaded" and not cal.values
    assert [t.values["connection_timeout_ms"] for t in cal.trials] == [2000, 4000, 8000] * 2
    verdict = cal.verdict()
    assert verdict.startswith("명세 부하가 허용 구현도 떨어뜨림 — ")
    assert "8000" in verdict and verdict.endswith("명세 수치 확인 (사람)")


async def test_a_calibration_names_only_the_cases_the_naive_failed() -> None:
    """케이스마다 따로 (#337 7절) — 흔한 구현이 hc1 에서만 떨어지면 보정은 hc1 로 하고, 승인 화면에 hc2 는 보정 밖이라고 적는다
    (hc2 는 다음 회차 기존 경로). 예전에는 '요청한 케이스 모두'를 조건으로 봐 보정이 통째로 안 됐다."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    base = _queue()

    async def run(values: dict[str, int]) -> Trial:
        t = await base(values)
        return replace(t, caught_cases=("hc1",) if t.caught else ())

    cal = await search(k, run, ["hc1", "hc2"])
    assert cal.status == "calibrated" and cal.missed == ("hc2",)
    assert "흔한 구현이 hc1 에서 pool_acquire_timeout 로 실패" in cal.flag()
    assert cal.flag().endswith(" · hc2 는 흔한 구현이 떨어지지 않아 보정 밖")


# ── 한 조합의 판정 ─────────────────────────────────────────────────


def _trial_report(naive: dict[str, Any], *accepted: dict[str, Any]) -> VerifyReport:
    return VerifyReport(
        runs=[
            _run("cal3-naive-all", "ref_naive", naive),
            *(_run(f"cal3-d{i}", "ref_accepted", t) for i, t in enumerate(accepted, 1)),
        ]
    )


def test_judge_reads_relabelled_runs_and_the_material_reason_family() -> None:
    assert base_label("cal12-naive-all") == "naive-all" and base_label("d1") == "d1"
    caught = _t("hidden", "hc1", "ASSERT", POOL, "32 건 실패")
    trial = judge(
        _trial_report(caught, _t("hidden", "hc1")),
        {"connection_timeout_ms": 500},
        [("naive-all", "hc1")],
        {"hc1": POOL},
    )
    assert trial.caught and trial.clean and trial.values == {"connection_timeout_ms": 500}
    other = _t("hidden", "hc1", "ASSERT", "request_timeout")
    report = _trial_report(other, _t("hidden", "hc1"))
    assert not judge(report, {}, [("naive-all", "hc1")], {"hc1": POOL}).caught
    family = (
        POOL,
        "request_timeout",
    )  # 재료가 정한 같은 숨은 요구의 사유 묶음 (repair._gate3 와 같다)
    assert judge(report, {}, [("naive-all", "hc1")], {"hc1": POOL}, family).caught
    broken = _trial_report(caught, _t("hidden", "hc1"), _t("hidden", "hc1", "ASSERT", POOL))
    assert not judge(broken, {}, [("naive-all", "hc1")], {"hc1": POOL}).clean


def test_judge_counts_a_case_the_naive_failed_even_when_another_case_passed() -> None:
    """#337 7절: 흔한 구현은 hc1 에서 의도한 사유로 떨어졌는데 누구에게나 가벼운 hc2 를 통과하자 '흔한 구현이 안 떨어짐'으로 분류됐다
    (#334 알파 2·3회차) → T-F1·재료 판정 대신 재생성 예산을 다 썼다. 케이스마다 따로 — 한 케이스라도 떨어지면 잡은 것."""
    report = VerifyReport(
        runs=[
            _run(
                "cal1-naive-all",
                "ref_naive",
                _t("hidden", "hc1", "ASSERT", POOL),
                _t("hidden", "hc2"),
            ),
            _run("cal1-d1", "ref_accepted", _t("hidden", "hc1"), _t("hidden", "hc2")),
        ]
    )
    request = [("naive-all", "hc1"), ("naive-all", "hc2")]
    trial = judge(report, {}, request, {"hc1": POOL, "hc2": POOL})
    assert trial.caught and trial.caught_cases == ("hc1",)


# ── 수리 배정 ───────────────────────────────────────────────────────


def test_assign_asks_to_calibrate_before_regenerating_the_naive() -> None:
    """흔한 구현이 숨은 요구 t1 의 케이스를 모두 통과 (어느 케이스도 함정을 못 보임) — 보정할 수 있는 hc1 만 보정 요청."""
    passed = _report(
        _run("naive-all", "ref_naive", _t("hidden", "hc1"), _t("hidden", "hc2")),
        _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("hidden", "hc2")),
    )
    plan = assign(passed, PLAN2, OVERLAYS, MISSED, calibrate=("hc1",))
    assert plan.calibrate == [("naive-all", "hc1")] and not plan.actions and not plan.passed
    # 같은 숨은 요구를 다른 케이스(hc2)가 이미 잡으면 흔한 구현은 함정을 가진 것 — 보정하지 않는다 (숨은 테스트 쪽 문제)
    shown = _report(
        _run(
            "naive-all",
            "ref_naive",
            _t("hidden", "hc1"),
            _t("hidden", "hc2", "ASSERT", "rollback_left_success"),
        ),
        _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("hidden", "hc2")),
    )
    assert not assign(shown, PLAN2, OVERLAYS, MISSED, calibrate=("hc1",)).calibrate
    # 보정할 수 없거나 이미 했으면 예전처럼 흔한 구현부터 다시
    plan = assign(passed, PLAN2, OVERLAYS, MISSED)
    assert [a.generator for a in plan.actions] == ["gen_naive"] and not plan.calibrate


# ── 빌드 루프 ───────────────────────────────────────────────────────


def _tuned() -> Template:
    """루프 테스트의 생성 산출물이 v1 모양이라 v1 템플릿에 v4 의 보정 칸만 얹는다 — 대기 한도 숨김·하한 250·기본값 없음,
    지연 기본값 20, 풀 사유 tunes."""
    m = JAVA_V1.manifest
    params = [
        p.model_copy(update={"visible": False, "minimum": 250})
        if p.name == "connection_timeout_ms"
        else p.model_copy(update={"default": 20, "default_expr": None})
        if p.name == "query_delay_ms"
        else p
        for p in m.harness_params
    ]
    reasons = [
        r.model_copy(update={"tunes": ["connection_timeout_ms", "query_delay_ms"]})
        if r.code == POOL
        else r
        for r in m.failure_reasons
    ]
    return replace(
        JAVA_V1,
        manifest=m.model_copy(update={"harness_params": params, "failure_reasons": reasons}),
    )


def _params(hidden: Overlay) -> dict[str, int]:
    text = next(f.content for f in hidden.files if f.path.endswith("grader-params.properties"))
    pairs = (
        line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#")
    )
    return {k: int(v) for k, v in pairs}


class QueueVerifier:
    """검증 회차·보정 조합 모두 숨은 오버레이의 조절값 파일로 대기열 모형을 푼다 — 풀 8·동시 32 는 8건씩 4묶음."""

    def __init__(self, naive_statements: int = 4, accepted_statements: int = 1) -> None:
        self.naive, self.accepted = naive_statements, accepted_statements
        self.verified: list[dict[str, int]] = []  # 검증 회차마다 본 조절값
        self.trials: list[list[str]] = []  # 보정 조합마다 돌린 대상 라벨

    def _runs(self, refs: Sequence[Overlay], hidden: Overlay) -> list[TargetRun]:
        values = _params(hidden)
        limit, delay = values.get("connection_timeout_ms", 30000), values.get("query_delay_ms", 20)
        caught = 3 * self.naive * delay > limit
        clean = 3 * self.accepted * delay <= limit
        out = []
        for o in refs:
            if o.kind == "ref_naive":
                hc1 = (
                    _t("hidden", "hc1", "ASSERT", POOL, "32 건 실패")
                    if caught
                    else _t("hidden", "hc1")
                )
            else:
                hc1 = (
                    _t("hidden", "hc1")
                    if clean
                    else _t("hidden", "hc1", "ASSERT", POOL, "3 건 실패")
                )
            out.append(_run(o.label, o.kind, hc1))
        return out

    async def verify(
        self,
        starter: Overlay,
        hidden: Overlay,
        refs: Sequence[Overlay],
        public: Overlay | None = None,
        conform: Overlay | None = None,
    ) -> VerifyReport:
        self.verified.append(_params(hidden))
        return VerifyReport(runs=[_run("starter", "starter"), *self._runs(refs, hidden)])

    async def verify_variants(
        self,
        starter: Overlay,
        variants: Sequence[Overlay],
        public: Overlay,
        hidden: Overlay | None = None,
    ) -> VerifyReport:
        assert hidden is not None
        self.trials.append([v.label for v in variants])
        return VerifyReport(runs=self._runs(variants, hidden))


@pytest.fixture
def no_variant_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """함정·변별 관문도 verify_variants 를 쓰는데 여기서는 보정만 본다 — 두 관문은 아무것도 하지 않게."""

    async def nothing(*_: Any, **__: Any) -> RepairPlan:
        return RepairPlan()

    monkeypatch.setattr(build_mod, "_trap_gate", nothing)
    monkeypatch.setattr(build_mod, "_variant_gate", nothing)


# 보정 칸이 있는 판의 숨은 케이스는 하네스 문서대로 지연 값을 읽어 켠다 — 읽지 않으면 정적 검사가 숨은 테스트를 다시 요청한다
# (static_check.case_applied_tunes, #308 팩트체크)
HIDDEN_DELAY = _files(
    (
        f"{CASES}/OrderStatusBurstTest.java",
        HC1.replace(
            '        int n = GraderParams.intValue("concurrency", 32);',
            '        GraderJdbcHarness.queryDelayMs = GraderParams.longValue("query_delay_ms", 20);\n'
            '        int n = GraderParams.intValue("concurrency", 32);',
        ).replace(
            "import com.example.service.grading.Burst;",
            "import com.example.service.grading.Burst;\n"
            "import com.example.service.grading.GraderJdbcHarness;",
        ),
    )
)


async def _loop(
    verifier: QueueVerifier, max_rounds: int = 3, task: TaskItem | None = None
) -> tuple[Any, Any]:
    llm, backend = _llm(gen_hidden=[HIDDEN_DELAY])
    hidden_plan = PLAN.model_copy(update={"harness": {"pool_size": 8, "concurrency": 32}})
    over: dict[str, Any] = {"task": task} if task is not None else {}
    out = await build_and_repair(
        _inputs(template=_tuned(), plan=hidden_plan, **over),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=verifier,
        max_rounds=max_rounds,
    )
    return out, backend


@pytest.mark.usefixtures("no_variant_gates")
async def test_the_build_calibrates_instead_of_regenerating_and_keeps_the_values() -> None:
    """흔한 구현이 Hikari 기본 대기에서 hc1 을 통과 → 보정 (지연 20 은 창 없음, 50 에서 [250, 500] → 350, 두 번 확인) → 다음 회차 전체 검증에서
    흔한 구현이 떨어져 review. 다시 만든 생성기 없음 — 보정 값은 빌드 계획(b.plan)·저장본(out.hidden_plan)·조절값 파일에 같이 남는다."""
    verifier = QueueVerifier()
    out, backend = await _loop(verifier)
    assert out.status == "review" and out.calibration is not None
    assert out.calibration.status == "calibrated"
    assert dict(out.calibration.values) == {"connection_timeout_ms": 350, "query_delay_ms": 50}
    assert verifier.verified == [
        {"pool_size": 8, "concurrency": 32},
        {"pool_size": 8, "concurrency": 32, "connection_timeout_ms": 350, "query_delay_ms": 50},
    ]
    assert len(verifier.trials) == 10 and verifier.trials[0] == ["cal1-naive-all", "cal1-d1"]
    assert len(out.calibration_reports) == 10  # 사다리 4 × 지연 2 + 확인 2
    assert out.hidden_plan is not None and out.hidden_plan.harness == {
        "pool_size": 8,
        "concurrency": 32,
        "connection_timeout_ms": 350,
        "query_delay_ms": 50,
    }
    assert _params(out.overlays["hidden"])["connection_timeout_ms"] == 350
    assert backend.calls.count("gen_naive") == 1 and backend.calls.count("gen_hidden") == 1
    assert any(f.startswith(FLAG) for f in out.review_flags)


@pytest.mark.usefixtures("no_variant_gates")
async def test_no_window_stops_the_build_for_a_person() -> None:
    """흔한 구현이 떨어지는 한도에서 허용 구현도 떨어진다 — 처음에는 시험 격자를 주고 그 숨은 케이스를 한 번 다시 만든 뒤 다시 보정하고
    (설계 검사 #332 T-F1: 창 유무는 그 회차의 버스트 대상·명세 수치에도 달려 같은 재료가 다른 회차에서는 보정됐다), 그래도 창이 없으면
    재료 판정으로 멈춘다. 흔한 구현은 다시 만들지 않는다."""
    out, backend = await _loop(QueueVerifier(accepted_statements=3))
    assert out.status == "failed" and out.calibration is not None
    assert out.calibration.status == "no_window" and out.hidden_plan is None
    assert len(out.calibration_tries) == 1 and out.calibration_tries[0].status == "no_window"
    assert any(
        f.startswith(MATERIAL_VERDICT)
        and "조절값으로 재현 불가" in f
        and "한 번 다시 만든 뒤에도" in f
        and "명세 운영 값" in f
        for f in out.review_flags
    )
    assert backend.calls.count("gen_naive") == 1  # 다시 만들지 않았다
    assert backend.calls.count("gen_hidden") == 2  # 처음 + 창 없음 뒤 한 번
    retry = [u for n, u in backend.users if n == "gen_hidden"][-1]
    assert "둘을 가를 창이 없었다" in retry and "흔한 구현" in retry
    assert [(r.gate, r.target) for r in out.repairs][:1] == [("③", "gen_hidden")]


async def test_a_spec_load_the_accepted_design_cannot_bear_stops_for_a_person() -> None:
    """#337 7절: 더 느슨한 한도(8000)까지 올려도 허용 구현이 떨어지면 — 창 없음처럼 처음에는 숨은 케이스를 한 번 다시 만들어 다시
    보정하고 (그 케이스가 허용 설계도 못 버티게 짜였을 수 있다), 그래도 같으면 '재료 판정: 명세 부하가 허용 구현도 떨어뜨림' 으로
    멈춘다. 예전에는 '흔한 구현이 안 떨어짐'으로 흔한 구현·숨은 테스트를 다시 만들며 예산을 다 썼다 (#334 알파 2·3회차)."""
    out, backend = await _loop(QueueVerifier(accepted_statements=140))
    assert out.status == "failed" and out.calibration is not None
    assert out.calibration.status == "overloaded" and out.hidden_plan is None
    assert len(out.calibration_tries) == 1 and out.calibration_tries[0].status == "overloaded"
    assert any(
        f.startswith(f"{MATERIAL_VERDICT}명세 부하가 허용 구현도 떨어뜨림")
        and "한 번 다시 만든 뒤에도" in f
        for f in out.review_flags
    )
    assert backend.calls.count("gen_naive") == 1  # 흔한 구현은 다시 만들지 않는다
    assert backend.calls.count("gen_hidden") == 2  # 처음 + 한 번
    retry = [u for n, u in backend.users if n == "gen_hidden"][-1]
    assert "허용 구현도 떨어졌다" in retry


@pytest.mark.usefixtures("no_variant_gates")
async def test_a_naive_that_never_falls_is_regenerated_once_without_another_calibration() -> None:
    verifier = QueueVerifier(naive_statements=1)
    out, backend = await _loop(verifier, max_rounds=1)
    assert out.calibration is not None and out.calibration.status == "naive_untouched"
    assert backend.calls.count("gen_naive") == 2  # 보정 대신 기존 경로 — 흔한 구현을 먼저 다시
    assert len(verifier.trials) == 8 and out.hidden_plan is None  # 보정은 빌드마다 한 번


@pytest.mark.usefixtures("no_variant_gates")
async def test_no_calibration_without_a_round_left_to_verify_it() -> None:
    """보정한 값은 다음 회차의 전체 검증이 판정한다 (③) — 마지막 회차에는 보정하지 않는다."""
    verifier = QueueVerifier()
    out, _ = await _loop(verifier, max_rounds=0)
    assert out.calibration is None and not verifier.trials


# ── 오프라인 재생 (평가 1단계) ──────────────────────────────────────


async def test_replay_reads_a_saved_alpha_and_runs_the_same_calibration(tmp_path: Path) -> None:
    """알파 폴더(run_alpha 출력)의 오버레이·숨은 계획으로 빌드와 같은 절차(calibrate_refs)를 돈다 — v4 보정 칸, 공개 재료는 재료 폴더에서."""
    from eval.starter.replay_calibration import replay

    alpha = tmp_path / "alpha"
    task = _inputs().task.model_copy(
        update={"source_incident": "pub:sw.backend:pool-hold-and-wait-deadlock"}
    )
    plan = PLAN.model_copy(update={"harness": {"pool_size": 8, "concurrency": 32}})
    alpha.mkdir()
    (alpha / "task.json").write_text(task.model_dump_json(), "utf-8")
    (alpha / "contract.json").write_text(
        json.dumps({"hidden_plan": plan.model_dump(mode="json")}), "utf-8"
    )
    for key, path in (
        ("starter", "src/main/java/com/example/service/order/Order.java"),
        ("hidden", "src/test/java/com/example/service/grading/cases/Hc1Test.java"),
        ("public", "src/test/java/com/example/service/publictests/OrderPublicTest.java"),
        ("naive-all", "src/main/java/com/example/service/order/OrderService.java"),
        ("d1", "src/main/java/com/example/service/order/OrderService.java"),
    ):
        (alpha / "overlays" / key / path).parent.mkdir(parents=True)
        (alpha / "overlays" / key / path).write_text("// 가상", "utf-8")
    verifier = QueueVerifier()
    rows = await replay(
        alpha,
        tmp_path / "work",
        incidents_dir=Path(__file__).parents[2] / "incidents/public_v1",
        make_verifier=lambda t, w: verifier,
    )
    assert [(r["reason"], r["status"], r["values"]) for r in rows] == [
        (POOL, "calibrated", {"connection_timeout_ms": 350, "query_delay_ms": 50})
    ]
    assert rows[0]["window"] == [250, 500] and len(rows[0]["trials"]) == 10
    assert verifier.trials[0] == ["cal1-d1", "cal1-naive-all"]  # 폴더 이름순 — 라벨이 대상


# ── 보정 값 ↔ 명세 수치 (#308) ────────────────────────────────────────


HIKARI_LOG = "WARN HikariPool-1 - Connection is not available, request timed out after 3000ms."


def test_spec_values_read_settings_and_the_hikari_log() -> None:
    """표기 패턴은 설정 꼴만 읽어 2단계 로그의 Hikari 문구를 못 읽었다 — 대기 한도는 로그 문구(SPEC_LOGS)도 본다."""
    m = V4
    texts = [HIKARI_LOG, "spring.datasource.hikari.connection-timeout: 5000"]
    assert spec_values(texts, "connection_timeout_ms", m) == [3000, 5000]
    assert spec_values(["가상 장애 보고 — 수치 없음"], "connection_timeout_ms", m) == []
    assert spec_note("connection_timeout_ms", 350, [3000]) == (
        " · 명세 자료의 connection_timeout_ms 값(3000)과 다름 — 채점은 보정 값 350 사용"
    )
    assert spec_note("connection_timeout_ms", 3000, [3000]) == ""
    assert spec_note("connection_timeout_ms", 350, []) == ""


@pytest.mark.usefixtures("no_variant_gates")
async def test_calibration_tells_the_approver_when_the_spec_says_another_value() -> None:
    """#308 운영 알파 2·3: 보정이 고른 대기 한도 350ms 가 명세 2단계 로그의 'timed out after 3000ms' 와 달랐다 — 3000 은 창 밖이라
    맞출 수 없어 승인 화면 보정 표시에 함께 남긴다 (채점은 보정 값)."""
    task = _task()
    log = Artifact(artifact_id="a9", title="가상 운영 로그", content=HIKARI_LOG, role="support")
    task = task.model_copy(update={"artifacts": [*task.artifacts, log]})
    out, _ = await _loop(QueueVerifier(), task=task)
    assert out.status == "review"
    [flag] = [f for f in out.review_flags if f.startswith(FLAG)]
    assert flag.endswith(
        " · 명세 자료의 connection_timeout_ms 값(3000)과 다름 — 채점은 보정 값 350 사용"
    )
