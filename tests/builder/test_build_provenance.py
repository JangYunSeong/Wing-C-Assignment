"""생성기 provenance (#233, 불변식 8) — 계약·빌드 단계 LLM 호출마다 프롬프트 판·sha·모델·설정·비용을 빌드 기록에 남긴다.

2026-11-18 점검: builder 가 provenance 를 비용 합계로만 쓰고 버려, 승인된 시작 코드·숨은 테스트가 어느 프롬프트 판·모델로 만들어졌는지
빌드 기록·묶음 어디에도 없었다. 실패한 빌드·수리 루프 재요청·관문 호출·단가를 모르는 모델의 호출까지 남는지 본다.
가짜 단계·메모리 저장소 — LLM·podman 없이, 가상 데이터만.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from task_spec.builder import steps as steps_mod
from task_spec.builder.runner import BuildResult, ContractResult, Job, StepFailed, run_job
from task_spec.builder.steps import RealSteps
from task_spec.nodes.starter.build import HIDDEN
from task_spec.nodes.starter.build import BuildOutcome as LoopOutcome
from task_spec.nodes.starter.plan_contract import PlanResult
from task_spec.schemas import GeneratedFile
from task_spec.schemas.build_record import BuildProvenance
from task_spec.schemas.provenance import Provenance, TokenUsage
from task_spec.starter.repair import STARTER
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import load_template
from tests.builder.test_runner import CID, FakeSteps, _cfg, _claimed
from tests.starter.test_data_template import _contract, _plan

SHA = "a" * 64
UNPRICED = "opencode:가상-모델"  # 단가표에 없는 모델 — 비용 None


def _prov(
    n: int, prompt: str, cost: float | None = 0.01, model: str = "openai:gpt-4.1"
) -> Provenance:
    """가상 호출 1건 — 본문 없이 출처만 (프롬프트 판·sha·모델·설정·few-shot id·토큰·비용)."""
    return Provenance(
        provenance_id=f"prov-{n:012x}",
        prompt=prompt,
        prompt_sha256=SHA,
        model=model,
        params={"temperature": 0, "reasoning_effort": "low"},
        fewshot_ids=["ex-1"],
        ontology_version="onto-test",
        tokens=TokenUsage(**{"in": 1000}, out=200, reasoning=50),
        latency_ms=1200,
        cost_usd=cost,
    )


# 계약 단계: plan_contract 첫 답 + 재요청. 빌드 단계: 생성기 + 수리 루프 재요청 + 관문(단가 모르는 모델)
CONTRACT_CALLS = (_prov(1, "plan_contract@31"), _prov(2, "plan_contract@31", 0.02))
BUILD_CALLS = (
    _prov(3, "gen_hidden@5"),
    _prov(4, "gen_starter@6"),
    _prov(5, "gen_starter@6", 0.005),
    _prov(6, "gen_trap@2", None, UNPRICED),
)


class ProvSteps(FakeSteps):
    """FakeSteps + 단계마다 LLM 호출 provenance (단계 비용 = 호출 비용 합)."""

    async def contract(self, job: Job) -> ContractResult:
        c = await super().contract(job)
        return dataclasses.replace(c, cost_usd=0.03, provenance=CONTRACT_CALLS)

    async def build(self, job: Job, c: ContractResult) -> BuildResult:
        b = await super().build(job, c)
        return dataclasses.replace(b, cost_usd=0.025, provenance=BUILD_CALLS)


def _calls(
    stage: str, attempt: int, ps: tuple[Provenance, ...]
) -> list[tuple[str, int, Provenance]]:
    return [(stage, attempt, p) for p in ps]


def _seen(calls: list[BuildProvenance]) -> list[tuple[str, int, Provenance]]:
    return [(x.stage, x.attempt, x.provenance) for x in calls]


async def test_a_build_keeps_every_contract_and_build_call(tmp_path: Path) -> None:
    store, b = await _claimed()
    done = await run_job(store, ProvSteps(tmp_path), _cfg(tmp_path), b)
    assert done.status == "review"
    assert _seen(done.provenance) == [
        *_calls("contract", 1, CONTRACT_CALLS),
        *_calls("build", 1, BUILD_CALLS),
    ]
    # 비용 누계와 같은 때(record_stage)에 쌓는다 — 호출 비용 합 = 빌드 비용 (단가를 모르는 호출은 0)
    spent = sum(x.provenance.cost_usd or 0.0 for x in done.provenance)
    assert spent == pytest.approx(done.cost_usd)
    again = await store.get(b.build_id)
    assert again is not None and again.provenance == done.provenance


@pytest.mark.parametrize(
    ("stage", "prompt"), [("contract", "plan_contract@31"), ("build", "gen_accepted@4")]
)
async def test_a_failed_build_keeps_the_calls_it_made(
    tmp_path: Path, stage: str, prompt: str
) -> None:
    """실패한 단계가 부른 호출도 (실패한 호출·재요청 포함) — 단가를 모르는 모델이라 비용이 0 이어도 (예전에는 비용이 있을 때만 쌓았다)."""
    store, b = await _claimed()
    spent = _prov(7, prompt, None, UNPRICED)
    err = StepFailed(f"{stage}: 가상 실패", provenance=[spent])
    done = await run_job(store, ProvSteps(tmp_path, fail_at=stage, error=err), _cfg(tmp_path), b)
    assert (done.status, done.error) == ("failed", f"{stage}: 가상 실패")
    before = [] if stage == "contract" else _calls("contract", 1, CONTRACT_CALLS)
    assert _seen(done.provenance) == [*before, (stage, 1, spent)]


async def test_a_retried_build_keeps_each_attempts_calls_apart(tmp_path: Path) -> None:
    """죽은 작업 복구 뒤 다시 가져가도(시도 2) 앞 시도의 호출 기록을 지우지 않는다 (비용 누계처럼) — 시도 번호로 가른다."""
    store, b = await _claimed()
    first = [
        BuildProvenance(stage="contract", attempt=b.attempt, provenance=p) for p in CONTRACT_CALLS
    ]
    await store.record_stage(
        b.build_id, "build", 0.03, provenance=first
    )  # 계약까지 하고 builder 가 사라짐
    (back,) = await store.recover_expired(datetime.now(UTC) + timedelta(hours=1))
    assert back.status == "pending" and back.provenance == first
    again = await store.claim("w2", 60)
    assert again is not None and again.attempt == 2
    done = await run_job(store, ProvSteps(tmp_path), _cfg(tmp_path), again)
    assert _seen(done.provenance) == [
        *_calls("contract", 1, CONTRACT_CALLS),
        *_calls("contract", 2, CONTRACT_CALLS),
        *_calls("build", 2, BUILD_CALLS),
    ]


# ── 실제 단계(RealSteps)가 plan_contract·build_and_repair 의 provenance 를 그대로 넘기는지 ──


def _real_steps(tmp_path: Path) -> RealSteps:
    # LLM·프롬프트는 바꿔 끼운 plan_contract·build_and_repair 로만 간다
    return RealSteps(None, None, Path("incidents/public_v1"), tmp_path / "images")  # type: ignore[arg-type]


async def _job(tmp_path: Path) -> Job:
    store, b = await _claimed()
    task = await store.task(CID)
    assert task is not None
    task = task.model_copy(update={"source_incident": "pub:data.eng:batch-rerun-late-partial"})
    return Job(build=b, task=task, label=store.labels[CID], work=tmp_path / "work")


async def test_contract_step_hands_on_every_plan_contract_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps, job = _real_steps(tmp_path), await _job(tmp_path)

    async def ok(*_: Any, **__: Any) -> PlanResult:
        return PlanResult(
            contract=_contract(), hidden_plan=_plan(), provenance=list(CONTRACT_CALLS)
        )

    monkeypatch.setattr(steps_mod, "plan_contract", ok)
    c = await steps.contract(job)
    assert c.provenance == CONTRACT_CALLS and c.cost_usd == pytest.approx(0.03)

    async def failed(*_: Any, **__: Any) -> PlanResult:
        return PlanResult(
            fail_reasons=["EmptyResponse(finish_reason=stop): 가상"],
            provenance=list(CONTRACT_CALLS),
        )

    monkeypatch.setattr(steps_mod, "plan_contract", failed)
    with pytest.raises(StepFailed) as e:
        await steps.contract(job)
    assert e.value.provenance == CONTRACT_CALLS and e.value.cost_usd == pytest.approx(0.03)


async def test_build_step_hands_on_every_generator_repair_and_gate_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_and_repair 결과의 provenance 전부 — 생성기·수리 루프 재요청·관문 호출 (성공·실패 둘 다)."""
    steps, job = _real_steps(tmp_path), await _job(tmp_path)
    c = ContractResult(
        template=load_template("data/python-duckdb", 1),
        contract=_contract(),
        plan=_plan(),
        cost_usd=0.0,
    )
    overlays = {
        STARTER: Overlay(
            kind="starter", files=(GeneratedFile(path="pipeline/run.py", content="x = 1"),)
        ),
        HIDDEN: Overlay(
            kind="hidden", files=(GeneratedFile(path="grading/cases/test_hc1.py", content="x"),)
        ),
    }
    status = "review"

    async def loop(*_: Any, **__: Any) -> LoopOutcome:
        return LoopOutcome(status=status, overlays=dict(overlays), provenance=list(BUILD_CALLS))  # type: ignore[arg-type]

    monkeypatch.setattr(steps_mod, "build_and_repair", loop)
    monkeypatch.setattr(
        steps_mod,
        "render_readme",
        lambda *_, **__: GeneratedFile(path="README.md", content="# 가상"),
    )
    b = await steps.build(job, c)
    assert b.provenance == BUILD_CALLS and b.cost_usd == pytest.approx(0.025)

    status = "failed"
    with pytest.raises(StepFailed) as e:
        await steps.build(job, c)
    assert e.value.provenance == BUILD_CALLS and e.value.cost_usd == pytest.approx(0.025)
