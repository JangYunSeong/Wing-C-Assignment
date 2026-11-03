"""확장 슬롯 (PR-A) — 대상 고르기·공통 과제 정보 노출·정답 노출 검사·동시 생성·저장소 id. 가상 데이터만."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from task_spec.llm import Completion, FakeBackend
from task_spec.llm.backend import TransientLLMError
from task_spec.prompting import PromptRegistry
from task_spec.schemas import Artifact, DraftSlotOutput, Observable, TaskItem
from task_spec.spec import slots as slots_mod
from task_spec.spec.slots import (
    FLAG_SLOT_FAILED,
    build_slots,
    common_secrets,
    common_visible,
    leak_check,
    plan_slots,
)
from task_spec.spec.store import CommonKey, InMemoryCommonStore
from tests.spec.test_artifacts import _v3
from tests.spec.test_common import COMPS, _ev, _profile
from tests.spec.test_draft import _client, _ctx

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


def test_plan_slots_puts_uncovered_competencies_first_and_caps() -> None:
    ev = [_ev(c.competency_id) for c in COMPS if c.competency_id != "comp:noev"]
    picked = plan_slots(_profile(COMPS), ev, common_ids={"comp:b", "comp:a"}, max_slots=4)
    ids = [c.competency_id for c, _ in picked]
    assert len(ids) == 4
    assert set(ids[:2]) <= {"comp:c", "comp:d", "comp:e"}  # 공통 과제가 안 다룬 역량 먼저
    assert "comp:noev" not in ids  # 수행준거 근거 없는 역량은 슬롯 대상 아님
    assert all(units for _, units in picked)


def _common() -> TaskItem:
    v3 = _v3()
    return TaskItem(
        task_id="run-r1-common-1",
        track="common",
        target_competencies=_ctx().targets,
        tech_env=_ctx().env,
        difficulty_target="high",
        time_limit_min=60,
        title=v3.title,
        scenario=v3.scenario,
        artifacts=v3.artifacts,
        max_requests=3,
        traps=v3.traps,
        observables=[Observable(kind="first_mention_of", value="잠금 경합")],
    )


def test_common_visible_has_titles_but_no_artifact_content_or_traps() -> None:
    text = common_visible(_common())
    assert "가상 구성 요소 1 지표" in text  # 자료 목록(이름)은 지원자가 본 것
    assert "가상 로그 1" not in text  # 자료 본문은 넣지 않는다
    assert "a3 만 보면 틀림" not in text  # 함정도 넣지 않는다


def test_leak_check_flags_common_answer_in_slot() -> None:
    secrets = common_secrets(_common(), _ctx())
    assert secrets == ["잠금 경합"]
    leaked = DraftSlotOutput.model_validate(
        _slot_json(
            scenario="같은 시스템에서 잠금 경합 때문에 느려진다. 아래 자료 중 최대 2개를 요청할 수 있다."
        )
    )
    assert leak_check(secrets)(leaked)  # type: ignore[operator]
    assert not leak_check(secrets)(DraftSlotOutput.model_validate(_slot_json()))  # type: ignore[operator]


def _slot_json(**over: Any) -> dict[str, Any]:
    roles = ["key", "distractor", "distractor", "distractor", "support", "support"]
    d = _v3().model_dump(mode="json")
    d.update(
        title="같은 시스템의 수집기 지연",
        scenario="같은 시스템에서 이번에는 수집기가 피크 때 지연된다. 아래 자료 중 최대 2개를 요청할 수 있다.",
        deliverables=["자료를 요청한 순서와 이유", "AI 와의 작업 중 채택·기각한 제안과 이유"],
        artifacts=[
            Artifact(
                artifact_id=f"a{i}",
                title=f"수집기 구성 요소 {i} 로그",
                content=f"슬롯 원시 로그 {i}",
                role=r,
            ).model_dump()  # type: ignore[arg-type]
            for i, r in enumerate(roles, 1)
        ],
        max_requests=2,
        traps=[
            {
                "trap_id": "t1",
                "description": "버퍼만 키움",
                "why_ai_fails": "AI 는 버퍼 확대를 권함",
            }
        ],
        observables=[
            {"kind": "requests_artifact", "value": "a1"},
            {"kind": "first_mention_of", "value": "단일 소비자 병목"},
            {"kind": "catches_trap", "value": "t1"},
        ],
    )
    d.update(over)
    return d


async def _fake_criteria(repo: Any, units: list[str], **kw: Any) -> tuple[list[Any], list[Any]]:
    return _ctx().criteria, []


async def test_build_slots_generates_in_parallel_and_drops_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slots_mod, "criteria_detail", _fake_criteria)
    ev = [_ev(c.competency_id) for c in COMPS if c.competency_id != "comp:noev"]
    ok = json.dumps(_slot_json(), ensure_ascii=False)
    # 슬롯 4개 중 하나는 LLM 이 계속 실패 → 그 슬롯만 빠지고 flag
    fake = FakeBackend().push(
        Completion(ok),
        Completion(ok),
        Completion(ok),
        *[TransientLLMError("InternalServerError")] * 8,
    )
    res = await build_slots(
        _profile(COMPS),
        ev,
        _common(),
        _ctx(),
        ontology=object(),  # type: ignore[arg-type]
        llm=_client(fake),
        prompts=PROMPTS,
    )
    assert len(res.slots) == 3 and len(res.flags) == 1 and res.flags[0].startswith(FLAG_SLOT_FAILED)
    s = res.slots[0]
    assert s.track == "extension_slot" and s.task_id.startswith("s") and s.time_limit_min == 30
    assert s.parent_task_id == "run-r1-common-1"
    assert len(s.artifacts) == 6 and s.max_requests == 2 and s.title
    assert len(s.target_competencies) == 1


async def test_store_points_slots_at_saved_common_id() -> None:
    store = InMemoryCommonStore()
    slot = TaskItem(
        task_id="s1",
        track="extension_slot",
        target_competencies=_ctx().targets,
        tech_env=_ctx().env,
        difficulty_target="medium",
        time_limit_min=30,
        parent_task_id="run-r1-common-1",
    )
    common = _common().model_copy(update={"slots": [slot]})
    saved = await store.save_draft(CommonKey("p", "v", "draft_task@3+draft_slot@1"), common, [])
    assert saved.task.task_id.startswith("ct-")
    assert saved.task.slots[0].parent_task_id == saved.task.task_id
