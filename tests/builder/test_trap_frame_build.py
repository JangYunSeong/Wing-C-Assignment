"""함정 틀이 빌드 계약 단계에 닿는 길 (#318 D1) — 공통 과제 행의 틀 적용 재료를 원 재료 대신 쓰고, 금지 저장 엔티티는 계약 입력·검사 4 로.

공용 재료(공개 사례 파일·회사 사례 DB)는 바꾸지 않는다 — 공고마다 자기 틀. 가상 데이터만, LLM 없음.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from task_spec.builder import steps as steps_mod
from task_spec.builder.runner import Job, StepFailed, run_job
from task_spec.nodes.starter.plan_contract import PlanResult, plan_input
from task_spec.schemas import FramedMaterial, TaskItem, TrapFrame
from task_spec.schemas.trap_frame import ForbiddenStorage
from task_spec.spec.store import CommonKey, InMemoryCommonStore
from task_spec.starter.templates import load_template
from tests.builder.test_build_provenance import _job, _real_steps
from tests.builder.test_runner import CID, FakeSteps, _cfg, _claimed
from tests.starter.test_contract import _check, _has
from tests.starter.test_data_template import _contract, _plan

SESSION = ForbiddenStorage(
    entity="Order", why="흔한 구현이 주문 상태를 매 요청 테이블에서 다시 읽는다 (가상)"
)


def _frame(**over: Any) -> TrapFrame:
    base: dict[str, Any] = {
        "kind": "design",
        "hidden_requirement": "가상 숨은 요구 — 틀이 정한 것",
        "forbidden_storage": [SESSION.model_dump()],
        "reason": "record_mismatch",
    }
    return TrapFrame.model_validate(base | over)


async def _framed_job(tmp_path: Path) -> tuple[Job, FramedMaterial]:
    job = await _job(tmp_path)
    inc = await _real_steps(tmp_path).incident_for(job.task)
    assert inc is not None and inc.feature is not None
    framed = FramedMaterial(
        seed=inc.feature.model_copy(update={"hidden_requirement": "가상 숨은 요구 — 틀이 정한 것"}),
        frame=_frame(),
    )
    return Job(job.build, job.task, job.label, job.work, framed=framed), framed


async def test_the_contract_step_uses_the_framed_material_and_forbidden_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = _real_steps(tmp_path)
    job, framed = await _framed_job(tmp_path)
    seen: dict[str, Any] = {}

    async def capture(*_: Any, **kw: Any) -> PlanResult:
        seen.update(kw)
        return PlanResult(contract=_contract(), hidden_plan=_plan())

    monkeypatch.setattr(steps_mod, "plan_contract", capture)
    c = await steps.contract(job)
    assert (
        seen["seed"] == framed.seed and c.seed == framed.seed
    )  # 생성기 입력(BuildInputs.seed)도 틀 적용 재료
    assert list(seen["forbidden_storage"]) == [SESSION]
    # 같은 목록을 빌드 입력(정적 검사·README 검사)에도 — 계약 단계만 받으면 시작 코드가 금지 저장을 둔다 (설계 검사 #332 T-F2)
    assert c.forbidden_storage == (SESSION,)
    captured: list[Any] = []

    async def stop(b: Any, *_: Any, **__: Any) -> Any:
        captured.append(b)
        raise StepFailed("build: 가상 멈춤")

    monkeypatch.setattr(steps_mod, "build_and_repair", stop)
    with pytest.raises(StepFailed):
        await steps.build(job, c)
    assert captured and captured[0].forbidden_storage == (SESSION,)
    # 틀이 없는 공고는 원 재료 그대로 — 공용 재료는 바꾸지 않는다
    plain = await _job(tmp_path)
    await steps.contract(plain)
    original = await steps.incident_for(plain.task)
    assert original is not None and seen["seed"] == original.feature
    assert list(seen["forbidden_storage"]) == []
    assert (await steps.contract(plain)).forbidden_storage == ()
    # 재료의 원인 기술 용어는 누설 검사에만 — 계약 검사(check_terms)와 빌드 입력으로 (설계 검사 #332 T-F7)
    assert tuple(seen["check_terms"]) == tuple(original.hidden_terms)
    assert (await steps.contract(plain)).check_terms == tuple(original.hidden_terms)


async def test_a_build_reads_the_framed_material_of_its_common_task(tmp_path: Path) -> None:
    store, b = await _claimed()
    job, framed = await _framed_job(tmp_path)
    store.framed_materials[CID] = framed
    seen: list[FramedMaterial | None] = []

    class Capture(FakeSteps):
        async def contract(self, job: Job):  # type: ignore[no-untyped-def]
            seen.append(job.framed)
            raise StepFailed("contract: 가상 멈춤")

    await run_job(store, Capture(tmp_path), _cfg(tmp_path), b)
    assert seen == [framed]


async def test_the_common_task_keeps_the_framed_material_with_its_draft(tmp_path: Path) -> None:
    store = InMemoryCommonStore()
    key = CommonKey("가상_공고#001", "onto-test", "interview:int-가상")
    task = TaskItem.model_validate(
        {
            "task_id": "ct-가상",
            "track": "common",
            "target_competencies": [{"competency_id": "comp:a", "label": "가상 역량"}],
            "tech_env": {"basis": "공고 언어"},
            "difficulty_target": "medium",
            "time_limit_min": 60,
        }
    )
    _, framed = await _framed_job(tmp_path)
    saved = await store.save_draft(key, task, [], framed=framed)
    plain = await store.save_draft(key, task, [])
    assert await store.framed(saved.id) == framed and await store.framed(plain.id) is None
    await store.approve_interview(saved.id, "가상 승인자", datetime.now(UTC))
    assert await store.framed(saved.id) == framed  # 승인해도 같은 행


def test_forbidden_storage_is_told_up_front_and_rejected_as_an_entity() -> None:
    """검사 4 (#318): 틀이 금지한 저장 방식이 계약 엔티티면 재요청 — 새·기존 모두, 이름은 단·복수 같게. 계약 입력에도 미리 알린다."""
    java = load_template("backend/java-spring-boot3-jpa")
    order = ForbiddenStorage(entity="orders", why="가상 해법 — 상태를 DB 밖에 둔다")
    got = _check(manifest=java.manifest, forbidden_storage=[order]).problems
    assert _has(got, "엔티티 Order(m1) 는 공개 계약에 두지 않는다", "함정 틀")
    assert not _has(_check(manifest=java.manifest).problems, "공개 계약에 두지 않는다")
    from tests.starter.test_plan_contract import _task

    text = plan_input(_task(), java, forbidden_storage=[order]).seed_rules
    assert "공개 계약에 엔티티(테이블)로 두지 않는 것" in text and "orders" in text
    assert "공개 계약에 엔티티(테이블)로 두지 않는 것" not in plan_input(_task(), java).seed_rules
    # 틀의 why(LLM 글 — 해법이 들어 있을 수 있음)는 계약 입력·재요청 사유 어디에도 싣지 않는다 (설계 검사 B-F10)
    assert "DB 밖" not in text and not any("DB 밖" in p for p in got)
