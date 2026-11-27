"""빌드 기록 trace (설계 검사 #332 T-F16·B-F1) — 코드 맞춤·정적 재생성·보정·관문 결과를 DB 기록에, 재료 판정 실패는 종류로 구분.
가상 데이터만, LLM 없음 (build_and_repair 대역)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import task_spec.builder.steps as steps_mod
from task_spec.builder.runner import ContractResult, Job, StepFailed
from task_spec.nodes.starter.build import HIDDEN
from task_spec.schemas import GeneratedFile, HiddenPlan, TaskContract
from task_spec.schemas.build import RepairRecord
from task_spec.starter.repair import MATERIAL_VERDICT, STARTER
from task_spec.starter.static_check import Overlay
from task_spec.starter.store import TaskLabel
from task_spec.starter.templates import load_template
from tests.builder.test_runner import _claimed
from tests.starter.test_contract import BACKEND_SEED, _contract, _plan
from tests.starter.test_plan_contract import _task


class _Calibration:
    def row(self) -> dict[str, Any]:
        return {
            "status": "no_window",
            "reason": "pool_acquire_timeout",
            "values": {},
            "window": None,
            "trials": [{"values": {"t": 1}, "caught": True, "clean": False}],
        }


def _out(status: str, flags: list[str]) -> SimpleNamespace:
    starter = Overlay(
        kind="starter", files=(GeneratedFile(path="src/main/resources/a.yml", content="a: 1\n"),)
    )
    hidden = Overlay(kind="hidden", files=(GeneratedFile(path="가상", content="가상"),))
    return SimpleNamespace(
        status=status,
        overlays={STARTER: starter, HIDDEN: hidden} if status == "review" else {},
        hidden_plan=None,
        review_flags=flags,
        repairs=[],
        cost_usd=0.0,
        rounds=2,
        provenance=[],
        adjustments=["gen_starter: 가상 코드 맞춤"],
        static_retries=["gen_starter: 가상 정적 문제"],
        calibration=_Calibration(),
        variants=[],
        traps=[],
        hand=["⑫ 가상 손 구현: 통과"],
        reports=["가상 검증 보고서"],  # 검증까지 간 빌드
        failed_steps=[],
    )


async def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, out: SimpleNamespace) -> Any:
    async def built(*_a: object, **_k: object) -> SimpleNamespace:
        return out

    monkeypatch.setattr(steps_mod, "build_and_repair", built)
    monkeypatch.setattr(steps_mod, "verifier_for", lambda *_a, **_k: None)
    monkeypatch.setattr(steps_mod, "keep_overlays", lambda *_a, **_k: None)
    _, record = await _claimed()
    job = Job(
        build=record,
        task=_task(),
        label=TaskLabel(title="가상 과제", posting_id="가상_공고#001"),
        work=tmp_path,
    )
    steps = steps_mod.RealSteps(None, None, tmp_path / "없음", tmp_path / "images")  # type: ignore[arg-type]
    cr = ContractResult(
        template=load_template("backend/java-spring-boot3-jpa", 2),
        contract=TaskContract.model_validate(_contract()),
        plan=HiddenPlan.model_validate(_plan()),
        cost_usd=0.0,
        seed=BACKEND_SEED,
        adjustments=("가상 계약 맞춤 — 시드 행 수",),
    )
    return await steps.build(job, cr)


async def test_a_build_keeps_what_the_code_fixed_and_what_the_gates_saw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-F16: 계약·생성기 코드 맞춤, 정적 재생성, 보정 결과, 관문 결과가 운영 빌드 기록에 남는다 — 예전에는 알파 보고서에만 있었다."""
    b = await _run(tmp_path, monkeypatch, _out("review", ["가상 표시"]))
    trace = b.trace
    assert trace is not None and trace.failure_kind is None
    assert trace.contract_adjustments == ["가상 계약 맞춤 — 시드 행 수"]
    assert trace.build_adjustments == ["gen_starter: 가상 코드 맞춤"]
    assert trace.static_retries == ["gen_starter: 가상 정적 문제"]
    assert (
        trace.calibration
        and "no_window" in trace.calibration[0]
        and "시험 1회" in trace.calibration[0]
    )
    assert trace.gates == ["⑫ 가상 손 구현: 통과"]


async def test_a_material_verdict_failure_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-F1: 재료 판정으로 멈춘 빌드는 사유 머리와 기록 종류로 구분된다 — 예전에는 '수리 루프 실패'로만 보여, 담당자가 같은 명세로
    다시 요청해 같은 비용을 썼다. 재요청은 막지 않는다 (보정 창은 회차마다 달라질 수 있다, T-F1)."""
    out = _out("failed", [f"{MATERIAL_VERDICT}가상 판정 — 창 없음"])
    with pytest.raises(StepFailed) as err:
        await _run(tmp_path, monkeypatch, out)
    assert str(err.value).startswith("build: 재료 판정으로 멈춤 (회차 2)")
    detail = err.value.detail
    assert detail is not None and detail.trace is not None
    assert detail.trace.failure_kind == "material"
    other = _out("failed", ["가상 다른 실패"])
    with pytest.raises(StepFailed) as err2:
        await _run(tmp_path, monkeypatch, other)
    assert str(err2.value).startswith("build: 수리 루프 실패")
    assert err2.value.detail is not None and err2.value.detail.trace is not None
    assert err2.value.detail.trace.failure_kind == "generation"


async def test_a_build_that_stopped_before_verification_names_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#337 6절: 생성 단계에서 검증 전에 멈추면 사유가 '수리 기록 없음'이었다 (운영 3건). 채점 코드 컴파일 수리 뒤 생성이 실패하면 마지막
    컴파일 수리를 사유처럼 보였다. 판단은 수리 기록이 아니라 검증 보고서 유무로 — 멈춘 단계 이름만 (숨은 테스트 문구 없음)."""
    out = _out("failed", ["gen_hidden 생성 실패: 가상 사유"])
    out.reports = []
    out.failed_steps = ["gen_public", "gen_hidden", "gen_hidden"]
    out.repairs = [
        RepairRecord(
            gate="compile", target="gen_hidden", reason="가상 컴파일", result="regenerated"
        )
    ]
    with pytest.raises(StepFailed) as err:
        await _run(tmp_path, monkeypatch, out)
    assert str(err.value) == "build: 검증 전 생성 실패 (gen_public, gen_hidden) — 검토 표시 참고"
    detail = err.value.detail
    assert detail is not None and detail.trace is not None
    assert detail.trace.failure_kind == "generation"
