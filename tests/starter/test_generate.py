"""시작 코드 생성기 4종 (nodes/starter/generate.py) — 가짜 LLM, 가상 데이터만.

입력 경계(누가 무엇을 못 보나)와 생성 → 정적 검사 → 1회 재요청을 본다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from task_spec.llm import Completion, FakeBackend
from task_spec.llm.backend import Usage
from task_spec.nodes.starter.generate import (
    BuildInputs,
    accepted_input,
    existing_apis,
    fit_input,
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


SEED_SQL = "src/main/resources/data.sql"  # 시작 코드에는 코드가 렌더한 시드가 늘 들어간다 (starter/seed_sql.py)


def _no_seed(files) -> list:  # type: ignore[no-untyped-def]
    return [f for f in files if f.path != SEED_SQL]


def _starter_overlay() -> Overlay:
    from task_spec.schemas import GeneratedFile

    return Overlay(
        kind="starter", files=tuple(GeneratedFile(path=p, content=c) for p, c in STARTER)
    )


async def _gen(fn, fake: FakeBackend, *args: Any, **kw: Any):  # type: ignore[no-untyped-def]
    return await fn(*args, llm=_client(fake), prompts=PROMPTS, ontology_version="onto-test", **kw)


async def test_failed_calls_are_counted_in_provenance() -> None:
    fake = FakeBackend().push(*[Completion("{not json", usage=Usage(input=800, output=300))] * 4)
    res = await _gen(gen_starter, fake, _inputs())
    assert not res.ok and res.overlay is None and len(fake.calls) == 4
    assert len(res.provenance) == 2 and sum(p.tokens.out for p in res.provenance) == 4 * 300


async def test_gen_starter_ok_and_reask_on_locked_file() -> None:
    fake = FakeBackend().push(_files(*STARTER))
    res = await _gen(gen_starter, fake, _inputs())
    assert res.ok and res.provenance[0].prompt == "gen_starter@3" and res.overlay is not None
    assert [f.path for f in _no_seed(res.overlay.files)] == [p for p, _ in STARTER]

    # 수리판(v3~): 재요청엔 첫 답이 이전 출력으로 가고, 고칠 파일만 — 잠긴 파일은 빈 content 로 지운다
    fake = FakeBackend().push(
        _files(("build.gradle", "plugins {}"), *STARTER), _files(("build.gradle", ""))
    )
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
    assert naive.provenance[0].prompt == "gen_naive@2"
    fake = FakeBackend().push(_files((f"{PKG}/order/OrderService.java", SERVICE)))
    acc = await _gen(gen_accepted, fake, _inputs(), _starter_overlay(), D1)
    assert acc.ok and acc.overlay is not None and acc.overlay.label == "d1"
    # 참조 구현이 테스트를 쓰면 재요청
    fake = FakeBackend().push(
        _files(("src/test/java/com/example/service/order/OrderServiceTest.java", "class T {}")),
        _files(
            ("src/test/java/com/example/service/order/OrderServiceTest.java", ""),  # 지움
            (f"{PKG}/order/OrderService.java", SERVICE),
        ),
    )
    acc = await _gen(gen_accepted, fake, _inputs(), _starter_overlay(), D1)
    assert acc.ok and any("OrderServiceTest.java" in p for p in acc.first_problems)


def test_existing_apis_come_from_public_spec() -> None:
    b = _inputs()
    assert existing_apis(b.task, b.contract) == ["PATCH /orders/{id}/status — 주문 상태 변경"]


def test_existing_apis_also_come_from_the_contract_flag() -> None:
    """2026-11-07 알파 테스트: 명세가 "거래 상태 변경 API" 라고만 써(경로 없음) 목록이 비자 시작 코드가 범위를 짐작했다."""
    b = _inputs()
    plain = b.task.model_copy(update={"scenario": "기존 상태 변경 API 에 이력을 더한다."})
    assert existing_apis(plain, b.contract) == []  # 경로가 없으면 글로는 못 찾는다
    flagged = b.contract.model_copy(
        update={
            "endpoints": [
                e.model_copy(update={"existing": e.method == "PATCH"}) for e in b.contract.endpoints
            ]
        }
    )
    assert existing_apis(plain, flagged) == ["PATCH /orders/{id}/status — 주문 상태 변경"]


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
        "데이터: Order id 105~200 (96행, 처음 status=READY, 버스트 — Burst.rounds(동시 요청 수, 버스트 수, …) 로 차례로, 요청마다 다른 id) — 이 범위만 쓴다 (시나리오에 적힌 id 보다 우선)"
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
        t = PROMPTS.get(task)
        prompt = assemble(t, fit_input(inp, t.input_model), ontology_version="v")
        assert "files: [{" in prompt.messages[0].content, task


def test_gen_starter_v2_asks_for_independent_existing_tests() -> None:
    """v2 (2026-11-07): 기존 기능 테스트 두 개가 같은 시드 행을 바꿔 실행 순서에 따라 실패했다."""
    b = _inputs()
    t = PROMPTS.get("gen_starter", 2)
    prompt = assemble(
        t,
        fit_input(starter_input(b.task, b.contract, b.template), t.input_model),
        ontology_version="v",
    )
    assert "테스트 메서드마다 서로 다른 시드 행" in prompt.messages[0].content


# ── 수리: 자기 이전 출력 + 고칠 파일만 (gen_starter v3 · gen_hidden/naive/accepted v2) ──


class _Pinned:
    """테스트용 — 지정한 생성기만 새 버전으로 (CURRENT 는 그대로)."""

    def __init__(self, **pins: int) -> None:
        self.pins = pins

    def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
        return PROMPTS.get(task, version or self.pins.get(task))


class _Capture:
    """응답을 차례로 돌려주고 보낸 user 메시지를 남긴다."""

    def __init__(self, *answers: Completion) -> None:
        self.answers, self.users = list(answers), []  # type: ignore[var-annotated]

    async def complete(self, model: str, messages: list[Any], params: dict[str, Any]) -> Completion:
        self.users.append(messages[-1].content)
        return self.answers.pop(0)


def _llm(backend: _Capture):  # type: ignore[no-untyped-def]
    from task_spec.llm import LLMClient

    async def _no_sleep(_: float) -> None:
        return None

    return LLMClient({"opencode": backend, "openai": backend}, sleep=_no_sleep)  # type: ignore[dict-item]


def test_merge_files_overwrites_keeps_and_deletes() -> None:
    from task_spec.nodes.starter.generate import merge_files
    from task_spec.schemas import GeneratedFile as F

    prev = [
        F(path="a.java", content="A"),
        F(path="b.java", content="B"),
        F(path="c.java", content="C"),
    ]
    merged = merge_files(
        prev,
        [
            F(path="b.java", content="B2"),
            F(path="c.java", content=" "),
            F(path="d.java", content="D"),
        ],
    )
    assert [(f.path, f.content) for f in merged] == [
        ("a.java", "A"),
        ("b.java", "B2"),
        ("d.java", "D"),
    ]


async def test_repair_gets_previous_output_and_returns_only_changed_files() -> None:
    """2026-11-07 루프: 숨은 테스트가 import 하나를 빠뜨렸는데 수리가 처음부터 다시 써 다른 문제가 생겼다."""
    fixed = ORDER.replace("private String status;", "private String status; // 고침")
    cap = _Capture(_files((f"{PKG}/order/Order.java", fixed)))
    res = await gen_starter(
        _inputs(),
        llm=_llm(cap),
        prompts=_Pinned(gen_starter=3),  # type: ignore[arg-type]
        ontology_version="onto-test",
        note="검증 실패 — Order.java 를 고친다",
        previous=_starter_overlay(),
    )
    assert res.ok and res.provenance[0].prompt == "gen_starter@3" and res.overlay is not None
    assert {f.path: f.content for f in _no_seed(res.overlay.files)} == {
        f"{PKG}/order/Order.java": fixed,
        "src/main/resources/schema.sql": STARTER[1][1],  # 손대지 않은 파일은 그대로
    }
    assert (
        "이전 출력 (수리일 때만" in cap.users[0]
        and "=== src/main/resources/schema.sql ===" in cap.users[0]
    )


async def test_static_reask_fixes_own_first_answer() -> None:
    """생성기 안 재요청도 첫 답을 이전 출력으로 — 잠긴 파일을 빈 content 로 지우게."""
    cap = _Capture(
        _files(("build.gradle", "plugins {}"), *STARTER),
        _files(("build.gradle", "")),
    )
    res = await gen_starter(
        _inputs(),
        llm=_llm(cap),
        prompts=_Pinned(gen_starter=3),
        ontology_version="onto-test",  # type: ignore[arg-type]
    )
    assert res.ok and res.overlay is not None
    assert sorted(f.path for f in _no_seed(res.overlay.files)) == sorted(p for p, _ in STARTER)
    assert "(없음)" in cap.users[0].split("이전 출력 (수리일 때만")[1][:80]  # 처음엔 이전 출력 없음
    assert "=== build.gradle ===" in cap.users[1]  # 재요청엔 첫 답을 이전 출력으로


async def test_old_versions_ignore_previous_output() -> None:
    cap = _Capture(_files(*STARTER))
    res = await gen_starter(
        _inputs(),
        llm=_llm(cap),
        prompts=_Pinned(gen_starter=2),  # type: ignore[arg-type]
        ontology_version="onto-test",
        previous=_starter_overlay(),
    )
    assert res.ok and res.provenance[0].prompt == "gen_starter@2"
    assert "이전 출력" not in cap.users[0]


async def test_seed_sql_is_rendered_by_code_from_the_contract() -> None:
    """2026-11-07 알파 3×3: 시작 코드가 수백 행 시드를 주석으로 생략(3/7) → 생성기가 쓴 data.sql 은 계약 시드 렌더본으로 바뀐다.
    시드가 모두 0행이면 렌더본은 주석뿐이라 init_scripts 가 SELECT 1; 을 붙인다 (Spring 부팅)."""
    lazy = (
        SEED_SQL,
        "-- 시드 데이터\nINSERT INTO orders (id, status) VALUES (1, 'READY');\n-- 나머지는 같은 방식으로\n",
    )
    fake = FakeBackend().push(_files(*STARTER, lazy))
    res = await _gen(gen_starter, fake, _inputs())
    assert res.ok and res.overlay is not None and len(fake.calls) == 1  # 재요청 없음
    seed = {f.path: f.content for f in res.overlay.files}[SEED_SQL]
    assert (
        "INSERT INTO orders (id, status) VALUES" in seed
        and "(200, " in seed
        and "같은 방식" not in seed
    )
    assert any("data.sql 를 계약 시드로 코드가 씀" in a for a in res.adjustments)
    # 시드가 모두 0행 → 주석뿐 → SELECT 1;
    empty = _inputs()
    empty = BuildInputs(
        **{
            **empty.__dict__,
            "contract": empty.contract.model_copy(
                update={
                    "seed": [
                        s.model_copy(update={"rows": 0, "groups": []}) for s in empty.contract.seed
                    ]
                }
            ),
        }
    )
    fake = FakeBackend().push(_files(*STARTER))
    res = await _gen(gen_starter, fake, empty)
    seed = {f.path: f.content for f in res.overlay.files}[SEED_SQL]
    assert seed.endswith("SELECT 1;\n") and "INSERT" not in seed
    # 참조 구현이 쓴 data.sql 은 빼고 시작 코드의 시드를 쓴다
    fake = FakeBackend().push(_files((f"{PKG}/order/OrderService.java", SERVICE), lazy))
    ref = await _gen(gen_naive, fake, _inputs(), _starter_overlay())
    assert ref.ok and SEED_SQL not in {f.path for f in ref.overlay.files}
    assert any("data.sql 를 뺌" in a for a in ref.adjustments)


def test_naive_goal_gets_the_template_naive_conditions_only() -> None:
    """2026-11-07 알파 5회차: 흔한 구현이 REQUIRES_NEW 를 같은 클래스 안에서 불러 전파 옵션이 무시됨 — 스택 사실만 붙인다 (정답 힌트인 stack_conditions 는 아님)."""
    b = _inputs()
    goal = naive_input(b.task, b.contract, b.template, _starter_overlay(), BACKEND_SEED).goal
    assert goal.startswith(BACKEND_SEED.naive_impl)
    assert "다른 빈의 public 메서드" in goal
    assert not any(c in goal for c in b.template.manifest.stack_conditions)
