"""공개 계약 생성 plan_contract (nodes/starter/plan_contract.py) — 가짜 LLM, 가상 데이터만."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from task_spec.llm import Completion, FakeBackend
from task_spec.llm.backend import Usage
from task_spec.nodes.starter.plan_contract import (
    FLAG_INVALID,
    FLAG_SPEC_GAP,
    for_prompt,
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
    assert res.provenance[0].prompt == "plan_contract@11"
    assert res.contract is not None and [e.endpoint_id for e in res.contract.endpoints] == [
        "e1",
        "e2",
    ]
    assert res.hidden_plan is not None and res.hidden_plan.harness["pool_size"] == 8


async def test_failed_calls_are_counted_in_provenance() -> None:
    bad = Completion("{not json", usage=Usage(input=1000, output=500))
    fake = FakeBackend().push(*[bad] * 6)  # 시도 3번 (ATTEMPTS) × (첫 답 + 형식 재요청)
    res = await _run(fake)
    assert not res.ok and res.contract is None
    assert len(fake.calls) == 6
    assert len(res.provenance) == 3 and all(p.prompt == "plan_contract@11" for p in res.provenance)
    assert sum(p.tokens.out for p in res.provenance) == 6 * 500


async def test_problems_are_reasked_once() -> None:
    fake = FakeBackend().push(_out(_leaky()), _out())
    res = await _run(fake)
    assert len(fake.calls) == 2 and res.ok
    assert any("공개 계약" in p and "connection pool" in p for p in res.first_problems)
    assert any(e.code == "contract_invalid" for e in res.errors)


async def test_persistent_problem_is_flagged_and_best_kept() -> None:
    """2026-11-08: 재요청 2번까지 (ATTEMPTS=3) — 검사 항목이 늘어 한 번에 다 못 고치는 경우가 늘었다."""
    fake = FakeBackend().push(_out(_leaky()), _out(_leaky()), _out(_leaky()))
    res = await _run(fake)
    assert len(fake.calls) == 3 and not res.ok
    assert res.contract is not None and res.problems
    assert any(f.startswith(FLAG_INVALID) for f in res.flags)


async def test_spec_without_operating_values_is_a_gap_after_evidence() -> None:
    """2026-11-07: 패턴으로 못 찾아도 LLM 을 부른다 — LLM 이 근거를 '없음'이라 하면 재요청 없이 명세 결함."""
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
    plan = _plan()
    plan["evidence"] = {"pool_size": "없음", "concurrency": "없음"}
    fake = FakeBackend().push(_out(plan=plan))
    res = await _run(fake, task)
    assert len(fake.calls) == 1  # 명세 결함은 재요청으로 못 고친다
    assert res.flags and res.flags[0].startswith(FLAG_SPEC_GAP)
    assert any("pool_size" in g for g in res.spec_gaps) and any(
        "concurrency" in g for g in res.spec_gaps
    )


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
    prompt = assemble(
        t, for_prompt(plan_input(_task(), TEMPLATE), t.input_model), ontology_version="v"
    )
    system = prompt.messages[0].content
    assert "covers: {문자열: [문자열]}" in system
    assert "harness?: {문자열: 정수 | 수 | 문자열 | [정수]}" in system
    assert "$defs" not in system
    user = prompt.messages[-1].content
    assert "숨은 요구 (hidden_plan 에만" in user and "t1 요청 하나가 연결을 둘 쥐지 않는다" in user


def test_v3_prompt_states_seed_rules_from_code() -> None:
    """v3 (2026-11-07): 시드 행 수 규칙은 검사(seed_rows)와 같은 값을 코드가 넣는다. v2 는 그 자리가 없어도 조립된다."""
    from task_spec.starter.seed_rows import RESERVED_ROWS
    from task_spec.starter.templates import load_template

    java = load_template("backend/java-spring-boot3-jpa")
    inp = plan_input(_task(), java)
    assert (
        f"작은 id {RESERVED_ROWS}행" in inp.seed_rules and "concurrency × bursts" in inp.seed_rules
    )
    v3 = assemble(PROMPTS.get("plan_contract", 3), inp, ontology_version="v")
    system, user = v3.messages[0].content, v3.messages[-1].content
    assert "groups?: [{" in system and "burst?: 참/거짓" in system  # 출력 스키마에 새 필드
    assert "id 범위는 적지 않습니다" in system
    assert "시드 규칙 (seed.rows · hidden_plan.cases.data):\n- 숨은 케이스는 작은 id" in user
    # 테스트 템플릿엔 burst_rows 가 없어 곱 규칙 줄이 빠진다
    assert "concurrency × bursts" not in plan_input(_task(), TEMPLATE).seed_rules
    t2 = PROMPTS.get("plan_contract", 2)
    v2 = assemble(t2, for_prompt(inp, t2.input_model), ontology_version="v")
    assert "시드 규칙" not in v2.messages[-1].content


async def test_row_counts_are_fitted_by_code_without_reask() -> None:
    """시드가 모자란 답 — 재요청 대신 코드가 맞추고(seed_rows.fit_seed) adjustments 로 남긴다."""
    c = _contract()
    c["seed"][0] = {
        "entity_id": "m1",
        "rows": 50,
        "groups": [{"first": 1, "last": 50, "values": {"status": "READY"}}],
    }
    fake = FakeBackend().push(_out(c))
    res = await _run(fake)
    assert res.ok and len(fake.calls) == 1, res.problems
    assert res.contract is not None and res.contract.seed[0].rows == 106  # 96 + 지원자 몫 10
    assert any("Order 시드 50 → 106행" in a for a in res.adjustments)


def test_v7_prompt_covers_ui_cli_fixtures_and_evidence() -> None:
    """v7 (2026-11-07 직군별 종이 단계): 화면·종료 코드·입력 환경·근거 인용."""
    ui = Template(
        root=Path("/nonexistent/v1"), manifest=_manifest(interfaces=["ui"], seed_rows=False)
    )
    inp = plan_input(_task(), ui)
    assert "화면 test id" in inp.template and "seed 와 숨은 케이스 data 는 비운다" in inp.seed_rules
    t = PROMPTS.get("plan_contract", 7)
    prompt = assemble(t, for_prompt(inp, t.input_model), ontology_version="v")
    system = prompt.messages[0].content
    for needle in (
        "screens?: [{",
        "fixtures?: [{",
        "setup?: [",
        "evidence?: {",
        "종료 코드 0~255",
        "그 값이 적힌 자료·답 문장을 그대로",
    ):
        assert needle in system, needle


def test_seed_needs_go_into_the_prompt_seed_rules() -> None:
    from task_spec.nodes.starter.plan_contract import plan_input
    from task_spec.starter.templates import load_template
    from tests.starter.test_contract import BACKEND_SEED

    inp = plan_input(_task(), load_template("backend/java-spring-boot3-jpa"), seed=BACKEND_SEED)
    assert (
        "숨은 요구가 계약에 꼭 필요로 하는 것" in inp.seed_rules and "허용 전이" in inp.seed_rules
    )
    assert (
        "숨은 요구가 계약에"
        not in plan_input(_task(), load_template("backend/java-spring-boot3-jpa")).seed_rules
    )


async def test_second_reask_carries_only_the_latest_problems() -> None:
    """재요청마다 직전 답의 문제만 — 앞 회차 문제는 이미 고쳤을 수 있다."""
    from tests.starter.test_generate import _Capture
    from tests.starter.test_generate import _llm as capture_llm

    cap = _Capture(_out(_leaky()), _out(_leaky()), _out())
    res = await plan_contract(
        _task(),
        TEMPLATE,
        llm=capture_llm(cap),
        prompts=PROMPTS,
        ontology_version="onto-test",
        seed=BACKEND_SEED,
    )
    assert len(cap.users) == 3 and res.ok
    assert "이전 답의 문제" not in cap.users[0]
    assert cap.users[2].count("이전 답의 문제") == 1 and "connection pool" in cap.users[2]
