"""시작 코드 생성기 4종 (nodes/starter/generate.py) — 가짜 LLM, 가상 데이터만.

입력 경계(누가 무엇을 못 보나)와 생성 → 정적 검사 → 1회 재요청을 본다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from task_spec.llm import Completion, FakeBackend
from task_spec.nodes.starter.generate import (
    BuildInputs,
    accepted_input,
    existing_apis,
    gen_accepted,
    gen_hidden,
    gen_naive,
    gen_starter,
    hidden_input,
    naive_input,
    starter_input,
)
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import AcceptedDesign, TaskItem
from task_spec.starter.static_check import Overlay
from tests.spec.test_feature import _client
from tests.starter.test_contract import BACKEND_SEED
from tests.starter.test_render import _approved, _contract_with_list
from tests.starter.test_static_check import CASES, HC1, JAVA, ORDER, PKG, PLAN

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = PromptRegistry(ROOT / "prompts")
D1 = AcceptedDesign(
    design_id="d1",
    summary="상태 변경이 끝난 뒤 이력을 기록",
    conditions="같은 요청 안에서 두 번째 연결을 잡지 않는다",
    trap_ids=["t1"],
)
D2 = AcceptedDesign(
    design_id="d2", summary="별도 작업 큐로 기록", conditions="큐 유실 대비", trap_ids=["t1"]
)
SERVICE = "package com.example.service.order;\n\nclass OrderService {}\n"


def _task() -> TaskItem:
    t = _approved()
    return t.model_copy(
        update={
            "scenario": "기존 상태 변경 API(`PATCH /orders/{id}/status`)에 이력을 더한다.\n\n"
            + t.scenario,
            "accepted_designs": [D1, D2],
        }
    )


def _inputs(**over: Any) -> BuildInputs:
    base: dict[str, Any] = {
        "task": _task(),
        "contract": _contract_with_list(),
        "plan": PLAN,
        "template": JAVA,
        "seed": BACKEND_SEED,
        "avoid_terms": ("우아한형제들",),
    }
    base |= over
    return BuildInputs(**base)


def _files(*pairs: tuple[str, str]) -> Completion:
    return Completion(
        json.dumps({"files": [{"path": p, "content": c} for p, c in pairs]}, ensure_ascii=False)
    )


STARTER = (
    (f"{PKG}/order/Order.java", ORDER),
    (
        "src/main/resources/schema.sql",
        "CREATE TABLE orders (id BIGINT PRIMARY KEY, status VARCHAR(20));\n",
    ),
)


def _starter_overlay() -> Overlay:
    from task_spec.schemas import GeneratedFile

    return Overlay(
        kind="starter", files=tuple(GeneratedFile(path=p, content=c) for p, c in STARTER)
    )


async def _gen(fn, fake: FakeBackend, *args: Any, **kw: Any):  # type: ignore[no-untyped-def]
    return await fn(*args, llm=_client(fake), prompts=PROMPTS, ontology_version="onto-test", **kw)


async def test_gen_starter_ok_and_reask_on_locked_file() -> None:
    fake = FakeBackend().push(_files(*STARTER))
    res = await _gen(gen_starter, fake, _inputs())
    assert res.ok and res.provenance[0].prompt == "gen_starter@2" and res.overlay is not None
    assert [f.path for f in res.overlay.files] == [p for p, _ in STARTER]

    fake = FakeBackend().push(_files(("build.gradle", "plugins {}"), *STARTER), _files(*STARTER))
    res = await _gen(gen_starter, fake, _inputs())
    assert len(fake.calls) == 2 and res.ok
    assert any("build.gradle" in p for p in res.first_problems)


async def test_gen_starter_leak_in_code_is_reasked() -> None:
    leak = ORDER.replace("private String status;", "private String status; // 피크 대비")
    fake = FakeBackend().push(_files((f"{PKG}/order/Order.java", leak)), _files(*STARTER))
    res = await _gen(gen_starter, fake, _inputs())
    assert res.ok and any("피크" in p for p in res.first_problems)


async def test_gen_hidden_gets_params_from_code_and_bad_import_is_reasked() -> None:
    bad = HC1.replace(
        "import com.example.service.grading.Burst;",
        "import com.example.service.order.OrderService;\nimport com.example.service.grading.Burst;",
    )
    handwritten = ("src/test/resources/grader-params.properties", "pool_size=99\n")
    fake = FakeBackend().push(
        _files((f"{CASES}/OrderStatusBurstTest.java", bad)),
        _files((f"{CASES}/OrderStatusBurstTest.java", HC1), handwritten),
    )
    res = await _gen(gen_hidden, fake, _inputs())
    assert res.ok and len(fake.calls) == 2
    assert any("지원자 구현 내부" in p for p in res.first_problems)
    assert res.overlay is not None
    params = [f for f in res.overlay.files if f.path.endswith("grader-params.properties")]
    assert (
        len(params) == 1 and "pool_size=8" in params[0].content
    )  # 손으로 쓴 99 는 버리고 HiddenPlan 값


async def test_gen_refs() -> None:
    fake = FakeBackend().push(_files((f"{PKG}/order/OrderService.java", SERVICE)))
    naive = await _gen(gen_naive, fake, _inputs(), _starter_overlay())
    assert naive.ok and naive.overlay is not None and naive.overlay.label == "naive-all"
    assert naive.provenance[0].prompt == "gen_naive@1"
    fake = FakeBackend().push(_files((f"{PKG}/order/OrderService.java", SERVICE)))
    acc = await _gen(gen_accepted, fake, _inputs(), _starter_overlay(), D1)
    assert acc.ok and acc.overlay is not None and acc.overlay.label == "d1"
    # 참조 구현이 테스트를 쓰면 재요청
    fake = FakeBackend().push(
        _files(("src/test/java/com/example/service/order/OrderServiceTest.java", "class T {}")),
        _files((f"{PKG}/order/OrderService.java", SERVICE)),
    )
    acc = await _gen(gen_accepted, fake, _inputs(), _starter_overlay(), D1)
    assert acc.ok and any("OrderServiceTest.java" in p for p in acc.first_problems)


def test_existing_apis_come_from_public_spec() -> None:
    b = _inputs()
    assert existing_apis(b.task, b.contract) == ["PATCH /orders/{id}/status — 주문 상태 변경"]


def test_starter_input_has_only_public_information() -> None:
    b = _inputs()
    text = json.dumps(
        starter_input(b.task, b.contract, b.template).model_dump(), ensure_ascii=False
    )
    assert "PATCH /orders/{id}/status — 주문 상태 변경" in text  # 이미 있는 API
    files = starter_input(b.task, b.contract, b.template).template_files
    assert "=== src/main/java/com/example/service/ServiceApplication.java (잠김) ===" in files
    assert "=== gradle/wrapper/gradle-wrapper.jar" not in files and "=== gradlew" not in files
    for hidden in (
        b.task.traps[0].description,
        b.task.acceptance_hidden[0].text,
        b.task.clarifications[0].answer,
        D1.summary,
        "pool_size",
        "아래 자료 중 최대",
    ):
        assert hidden not in text, hidden


def test_hidden_input_has_plan_and_harness_but_no_code() -> None:
    b = _inputs()
    text = json.dumps(
        hidden_input(b.task, b.contract, b.plan, b.template).model_dump(), ensure_ascii=False
    )
    assert (
        "pool_size = 8" in text and "hc1 (h1, 숨은 요구 t1, 실패 사유 pool_acquire_timeout)" in text
    )
    assert "Burst.fire" in text  # 하네스 API 문서
    # 시드 id 범위는 코드가 나눠 준다 (seed_rows) — 시작 코드를 보지 않고도 쓸 행을 안다
    assert (
        "데이터: Order id 105~200 (96행, 처음 status=READY, 버스트 — 요청마다 다른 id) — 이 범위만 쓴다 (시나리오에 적힌 id 보다 우선)"
        in text
    )
    assert "조절값 파일(src/test/resources/grader-params.properties)은 쓰지 않는다" in text
    assert "class Order" not in text and D1.summary not in text


def test_ref_inputs_see_starter_and_their_own_goal_only() -> None:
    b = _inputs()
    assert BACKEND_SEED is not None
    naive = json.dumps(
        naive_input(b.task, b.contract, b.template, _starter_overlay(), BACKEND_SEED).model_dump(),
        ensure_ascii=False,
    )
    assert "class Order" in naive and BACKEND_SEED.naive_impl in naive
    for hidden in (
        BACKEND_SEED.why_naive_breaks,
        D1.summary,
        b.task.acceptance_hidden[0].text,
        "pool_size",
    ):
        assert hidden not in naive, hidden
    acc = json.dumps(
        accepted_input(b.task, b.contract, b.template, _starter_overlay(), D1).model_dump(),
        ensure_ascii=False,
    )
    assert D1.summary in acc and "REQUIRES_NEW" in acc  # 템플릿 스택 조건
    assert D2.summary not in acc and BACKEND_SEED.naive_impl not in acc and "Burst" not in acc


def test_generator_prompts_assemble_with_compact_schema() -> None:
    b = _inputs()
    inputs = {
        "gen_starter": starter_input(b.task, b.contract, b.template),
        "gen_hidden": hidden_input(b.task, b.contract, b.plan, b.template),
        "gen_naive": naive_input(b.task, b.contract, b.template, _starter_overlay(), BACKEND_SEED),  # type: ignore[arg-type]
        "gen_accepted": accepted_input(b.task, b.contract, b.template, _starter_overlay(), D1),
    }
    for task, inp in inputs.items():
        prompt = assemble(PROMPTS.get(task), inp, ontology_version="v")
        assert "files: [{" in prompt.messages[0].content, task


def test_gen_starter_v2_asks_for_independent_existing_tests() -> None:
    """v2 (2026-11-07): 기존 기능 테스트 두 개가 같은 시드 행을 바꿔 실행 순서에 따라 실패했다."""
    b = _inputs()
    prompt = assemble(
        PROMPTS.get("gen_starter", 2),
        starter_input(b.task, b.contract, b.template),
        ontology_version="v",
    )
    assert "테스트 메서드마다 서로 다른 시드 행" in prompt.messages[0].content
