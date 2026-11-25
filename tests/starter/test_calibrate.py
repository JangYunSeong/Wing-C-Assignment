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
)
from task_spec.starter.repair import MATERIAL_VERDICT, RepairPlan, assign
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template, load_template
from tests.starter.test_generate import D1, _inputs
from tests.starter.test_repair import MISSED, OVERLAYS, PLAN2, PROMPTS, _llm, _report, _run, _t
from tests.starter.test_static_check import JAVA as JAVA_V1
from tests.starter.test_static_check import PLAN

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
        POOL, "connection_timeout_ms", (2000, 1000, 500, 250), "query_delay_ms", (20, 50)
    )
    # 계획이 지연을 정했으면 그 값부터 (계약 단계가 숨긴 값은 빼지만 재료가 정한 값일 수 있다)
    delayed = POOL_PLAN.model_copy(update={"harness": {**POOL_PLAN.harness, "query_delay_ms": 30}})
    assert knobs(POOL, V4, delayed) == replace(k, loads=(30, 50))
    # 재료가 지연을 나열했으면(명세에 보이는 값) 한도만 잰다
    assert knobs(POOL, V4, POOL_PLAN, ["query_delay_ms"]) == Knobs(
        POOL, "connection_timeout_ms", (2000, 1000, 500, 250)
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
    """#301 대기열 모형: 지연 20 이면 흔한 구현 최대 대기 약 320ms — 하한 250 에서만 떨어져 창 [250, 250] (좁음). 지연 50 이면
    [250, 500] → 기하 중간값 350. 조합 8회 (대기 한도 4 × 지연 2)."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _queue(), ["hc1"])
    assert cal.status == "calibrated" and cal.window == (250, 500)
    assert dict(cal.values) == {"connection_timeout_ms": 350, "query_delay_ms": 50}
    assert [t.values["query_delay_ms"] for t in cal.trials] == [20] * 4 + [50] * 4
    assert cal.flag() == (
        f"{FLAG}connection_timeout_ms 350·query_delay_ms 50 — 흔한 구현이 hc1 에서 pool_acquire_timeout 로 실패, "
        "허용 구현 모두 통과 (connection_timeout_ms 창 250~500, 시험 8회)"
    )


async def test_a_wide_window_at_the_planned_delay_stops_early() -> None:
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _queue(naive_statements=10), ["hc1"])  # 지연 20 에서 흔한 구현 대기 800ms
    assert cal.status == "calibrated" and cal.window == (250, 500) and len(cal.trials) == 4
    assert dict(cal.values) == {"connection_timeout_ms": 350, "query_delay_ms": 20}


async def test_no_window_when_the_accepted_design_fails_where_the_naive_does() -> None:
    """흔한 구현은 떨어지는데 그 한도에서 허용 구현도 떨어진다 — 재료가 조절값으로 재현되지 않음 (사람). 허용 구현이 떨어지기
    시작한 한도 아래는 보지 않는다."""
    k = knobs(POOL, V4, POOL_PLAN)
    assert k is not None
    cal = await search(k, _queue(accepted_statements=3), ["hc1"])
    assert cal.status == "no_window" and not cal.values
    # 지연 20: 끝까지 (흔한 구현은 250 에서만 실패) · 지연 50: 허용 구현이 500 에서 떨어져 250 은 보지 않음
    assert [t.values["connection_timeout_ms"] for t in cal.trials] == [
        2000,
        1000,
        500,
        250,
        2000,
        1000,
        500,
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
    assert cal.status == "naive_untouched" and len(cal.trials) == 8


@pytest.mark.parametrize(
    ("lo", "hi", "want"), [(250, 500, 350), (250, 1000, 500), (500, 2000, 1000), (250, 2000, 710)]
)
def test_pick_is_the_geometric_middle(lo: int, hi: int, want: int) -> None:
    assert pick(lo, hi) == want


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


async def _loop(verifier: QueueVerifier, max_rounds: int = 3) -> tuple[Any, Any]:
    llm, backend = _llm()
    hidden_plan = PLAN.model_copy(update={"harness": {"pool_size": 8, "concurrency": 32}})
    out = await build_and_repair(
        _inputs(template=_tuned(), plan=hidden_plan),
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
    """흔한 구현이 Hikari 기본 대기에서 hc1 을 통과 → 보정 (지연 20 은 창 없음, 50 에서 [250, 500] → 350) → 다음 회차 전체 검증에서
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
    assert len(verifier.trials) == 8 and verifier.trials[0] == ["cal1-naive-all", "cal1-d1"]
    assert len(out.calibration_reports) == 8
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
    """흔한 구현이 떨어지는 한도에서 허용 구현도 떨어진다 — 재생성 예산을 쓰지 않고 재료 판정으로 멈춘다."""
    out, backend = await _loop(QueueVerifier(accepted_statements=3))
    assert out.status == "failed" and out.calibration is not None
    assert out.calibration.status == "no_window" and out.hidden_plan is None
    assert any(
        f.startswith(MATERIAL_VERDICT) and "조절값으로 재현 불가" in f for f in out.review_flags
    )
    assert backend.calls.count("gen_naive") == 1  # 다시 만들지 않았다


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
    assert rows[0]["window"] == [250, 500] and len(rows[0]["trials"]) == 8
    assert verifier.trials[0] == ["cal1-d1", "cal1-naive-all"]  # 폴더 이름순 — 라벨이 대상
