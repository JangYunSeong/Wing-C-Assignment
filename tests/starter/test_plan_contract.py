"""공개 계약 생성 plan_contract (nodes/starter/plan_contract.py) — 가짜 LLM, 가상 데이터만."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from task_spec.llm import Completion, FakeBackend
from task_spec.nodes.starter.plan_contract import (
    FLAG_INVALID,
    FLAG_SPEC_GAP,
    plan_contract,
    plan_input,
)
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import TaskItem
from task_spec.starter.templates import Template
from tests.spec.test_feature import _client, _skeleton
from tests.starter.test_contract import BACKEND_SEED, _contract, _manifest, _plan

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = PromptRegistry(ROOT / "prompts")
TEMPLATE = Template(root=Path("/nonexistent/v1"), manifest=_manifest())


def _task(**over: Any) -> TaskItem:
    base = _skeleton().model_dump()
    base |= {
        "title": "가상 주문 상태 변경 이력 추가",
        "scenario": "가상의 물류 회사가 주문 상태 변경마다 이력을 남기려 한다.",
        "requirements": [
            {"req_id": "r1", "text": "상태 변경 요청마다 이력을 남긴다"},
            {"req_id": "r2", "text": "이력을 최신순으로 조회한다"},
        ],
        "acceptance_public": [
            {"ac_id": "p1", "text": "성공한 변경 1건이면 이력 1건"},
            {"ac_id": "p2", "text": "조회 결과는 최신순"},
        ],
        "deliverables": ["핵심 구현", "테스트", "설계 메모"],
        "traps": [
            {
                "trap_id": "t1",
                "description": "요청 하나가 연결을 둘 쥐지 않는다",
                "why_ai_fails": "기록을 별도 단위로 즉시 쓴다",
            }
        ],
        "acceptance_hidden": [
            {"ac_id": "h1", "trap_id": "t1", "text": "동시 요청에서 모두 정상 응답"}
        ],
        "artifacts": [
            {
                "artifact_id": "a1",
                "title": "운영 설정",
                "content": "pool: 8\nthreads: 32",
                "role": "key",
            }
        ],
        "clarifications": [
            {
                "clarification_id": "c1",
                "topic": "피크 요청량",
                "answer": "마감 직전 동시 요청은 최대 32건입니다.",
                "reveals": ["t1"],
            }
        ],
    }
    base |= over
    return TaskItem.model_validate(base)


def _out(contract: dict[str, Any] | None = None, plan: dict[str, Any] | None = None) -> Completion:
    body = {"contract": contract or _contract(), "hidden_plan": plan or _plan()}
    return Completion(json.dumps(body, ensure_ascii=False))


def _leaky() -> dict[str, Any]:
    c = _contract()
    c["entities"][1]["fields"].append(
        {"name": "connectionPoolSize", "type": "integer", "refs": ["r1"]}
    )
    return c


async def _run(fake: FakeBackend, task: TaskItem | None = None):  # type: ignore[no-untyped-def]
    return await plan_contract(
        task or _task(),
        TEMPLATE,
        llm=_client(fake),
        prompts=PROMPTS,
        ontology_version="onto-test",
        seed=BACKEND_SEED,
    )


async def test_first_answer_ok() -> None:
    fake = FakeBackend().push(_out())
    res = await _run(fake)
    assert res.ok and len(fake.calls) == 1 and res.flags == []
    assert res.provenance[0].prompt == "plan_contract@2"
    assert res.contract is not None and [e.endpoint_id for e in res.contract.endpoints] == [
        "e1",
        "e2",
    ]
    assert res.hidden_plan is not None and res.hidden_plan.harness["pool_size"] == 8


async def test_problems_are_reasked_once() -> None:
    fake = FakeBackend().push(_out(_leaky()), _out())
    res = await _run(fake)
    assert len(fake.calls) == 2 and res.ok
    assert any("공개 계약" in p and "connection pool" in p for p in res.first_problems)
    assert any(e.code == "contract_invalid" for e in res.errors)


async def test_persistent_problem_is_flagged_and_best_kept() -> None:
    fake = FakeBackend().push(_out(_leaky()), _out(_leaky()))
    res = await _run(fake)
    assert len(fake.calls) == 2 and not res.ok
    assert res.contract is not None and res.problems
    assert any(f.startswith(FLAG_INVALID) for f in res.flags)


async def test_spec_without_operating_values_fails_without_llm() -> None:
    task = _task(
        artifacts=[
            {"artifact_id": "a1", "title": "운영 설정", "content": "로그 수준 INFO", "role": "key"}
        ],
        clarifications=[
            {
                "clarification_id": "c1",
                "topic": "피크",
                "answer": "마감 때 몰린다.",
                "reveals": ["t1"],
            }
        ],
    )
    fake = FakeBackend()  # 응답 없음 — 부르면 실패한다
    res = await _run(fake, task)
    assert fake.calls == [] and res.contract is None
    assert res.flags and res.flags[0].startswith(FLAG_SPEC_GAP)
    assert {"필수 운영 수치 pool_size", "필수 운영 수치 concurrency"} <= set(res.spec_gaps)


async def test_hidden_plan_value_mismatch_is_reasked() -> None:
    wrong = copy.deepcopy(_plan())
    wrong["harness"]["pool_size"] = 10  # 자료는 8
    fake = FakeBackend().push(_out(plan=wrong), _out())
    res = await _run(fake)
    assert res.ok and any("pool_size=10" in p for p in res.first_problems)


def test_plan_input_renders_template_spec_and_materials() -> None:
    inp = plan_input(_task(), TEMPLATE, "이전 답의 문제:\n- x")
    assert "pool_size (int, 필수, 지원자에게 보임" in inp.harness_params
    assert "query_delay_ms (int, 선택, 0–200)" in inp.harness_params
    assert "- pool_acquire_timeout — " in inp.failure_reasons
    assert "[a1] 운영 설정\npool: 8" in inp.materials
    assert "[c1] 피크 요청량: 마감 직전" in inp.clarifications
    assert inp.acceptance_hidden == "h1 (t1) 동시 요청에서 모두 정상 응답"
    assert inp.traps.startswith("t1 ") and inp.revision_note.startswith("이전 답의 문제")
    assert "backend/test-stack@v1 — 인터페이스: http" in inp.template


def test_prompt_renders_dict_and_union_fields_compactly() -> None:
    t = PROMPTS.get("plan_contract")
    prompt = assemble(t, plan_input(_task(), TEMPLATE), ontology_version="v")
    system = prompt.messages[0].content
    assert "covers: {문자열: [문자열]}" in system
    assert "harness?: {문자열: 정수 | 수 | 문자열 | [정수]}" in system
    assert "$defs" not in system
    user = prompt.messages[-1].content
    assert "숨은 요구 (hidden_plan 에만" in user and "t1 요청 하나가 연결을 둘 쥐지 않는다" in user
