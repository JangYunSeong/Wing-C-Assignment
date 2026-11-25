"""숨김 수용 기준의 실패 지정 — 명세가 항목 내용으로 정하고(사람 승인), 계약 단계는 API·요청 칸에 옮기기만 한다. 가상 데이터만."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any, cast

from task_spec.nodes.starter.generate import fault_text, plan_text
from task_spec.nodes.starter.plan_contract import fill_hidden_defaults, fit_faults
from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.schemas.build import Fault
from task_spec.schemas.domain import HiddenAcceptance, HiddenFault
from task_spec.spec.feature import check_hidden_faults
from tests.starter.test_contract import BACKEND_SEED, _check, _contract, _has, _plan, _spec

APPROVED = HiddenFault(operation="주문 상태 변경", item="SHIPPED", times=1, status=503)
MAPPED = {"endpoint": "e1", "when": {"toStatus": "SHIPPED"}, "status": 503}


def _with(case: dict[str, Any]) -> dict[str, Any]:
    plan = _plan()
    plan["cases"] = [{**plan["cases"][0], **case}]
    return plan


def test_spec_check_needs_content_faults_for_item_materials() -> None:
    item = BACKEND_SEED.model_copy(update={"fault_by": "item"})
    bare = HiddenAcceptance(
        ac_id="h1", trap_id="t1", text="재연결 직후 처음 2건 503 이어도 한 번씩 저장"
    )
    out = cast(Any, SimpleNamespace(acceptance_hidden=[bare]))
    probs = check_hidden_faults(out, item)
    assert any("h1 에 실패 지정(faults)이 없음" in p for p in probs)
    assert any("순번으로 정함('처음 2건')" in p for p in probs)
    ok = bare.model_copy(
        update={
            "text": "SHIPPED 변경의 첫 전송이 실패해도 한 번씩 저장",
            "faults": [APPROVED],
            "items": ["SHIPPED", "DELIVERED"],
        }
    )
    assert check_hidden_faults(cast(Any, SimpleNamespace(acceptance_hidden=[ok])), item) == []
    assert check_hidden_faults(out, BACKEND_SEED) == []  # 재료가 쓰지 않으면 보지 않는다


def test_fault_must_name_a_contract_api_and_its_request_fields() -> None:
    res = _check(plan=_with({"faults": [{"endpoint": "e9", "status": 503}]}))
    assert _has(res.problems, "실패 지정 API e9 가 계약에 없음")
    res = _check(plan=_with({"faults": [{"endpoint": "e1", "when": {"nope": "x"}, "status": 503}]}))
    assert _has(res.problems, "요청 본문에 없는 칸으로 고름: nope", "toStatus")


def test_contract_must_carry_the_approved_fault_with_the_same_values() -> None:
    spec = dataclasses.replace(_spec(), hidden_faults={"h1": (APPROVED,)})
    res = _check(plan=_with({"scenario": "처음 2건 503"}), spec=spec)
    assert _has(
        res.problems, "hc1 가 숨김 수용 기준 h1 의 승인된 실패 지정을 옮기지 않음", "'SHIPPED'"
    )
    assert _has(res.problems, "순번으로 정함('처음 2건')")
    wrong = {**MAPPED, "status": 500}
    assert _has(_check(plan=_with({"faults": [wrong]}), spec=spec).problems, "옮기지 않음")
    ok = _with({"scenario": "SHIPPED 변경의 첫 전송만 503", "faults": [MAPPED]})
    assert not _has(_check(plan=ok, spec=spec).problems, "실패")
    # 승인된 지정이 없으면 순번 글도 지금처럼 둔다
    assert not _has(_check(plan=_with({"scenario": "처음 2건 503"})).problems, "순번")


def test_faults_are_dropped_unless_the_spec_approved_them() -> None:
    plan = HiddenPlan.model_validate(_with({"faults": [MAPPED]}))
    kept, notes = fit_faults(plan, dataclasses.replace(_spec(), hidden_faults={"h1": (APPROVED,)}))
    assert kept.cases[0].faults and notes == []
    dropped, notes = fit_faults(plan, _spec())
    assert dropped.cases[0].faults == [] and "실패 지정 뺌 (hc1)" in notes[0]


def test_hidden_test_input_says_which_request_fails_by_content() -> None:
    plan = HiddenPlan.model_validate(_with({"faults": [MAPPED]}))
    text = plan_text(plan, TaskContract.model_validate(_contract()))
    assert (
        '실패 지정: PATCH /orders/{id}/status 에서 본문 toStatus="SHIPPED" 인 요청의 처음 1번 → 503 응답'
        in text
    )
    assert "몇 번째 요청인지로 정하지 않는다" in text
    assert "모든 요청의 처음 2번 → 연결 실패" in fault_text(Fault(endpoint="e2", times=2))


def test_inputs_carry_the_fault_rule_and_the_approved_faults() -> None:
    """명세 재료 글에 '실패 지정' 줄(재료가 켰을 때만), 계약 입력의 숨김 수용 기준 줄에 승인된 실패 지정."""
    import json
    from pathlib import Path

    from task_spec.nodes.starter.plan_contract import hidden_ac_line
    from task_spec.schemas.incident import PublicIncident
    from task_spec.spec.feature import seed_block

    raw = json.loads(
        (
            Path(__file__).parents[2] / "incidents/public_v1/sw.frontend.offline-replay.json"
        ).read_text("utf-8")
    )
    inc = PublicIncident.model_validate(raw)
    assert "실패 지정 (숨김 수용 기준마다 필수)" in seed_block(inc)
    off = inc.model_copy(update={"feature": inc.feature.model_copy(update={"fault_by": None})})  # type: ignore[union-attr]
    assert "실패 지정" not in seed_block(off)
    h = HiddenAcceptance(ac_id="h1", trap_id="t1", text="가상 기준", faults=[APPROVED])
    assert hidden_ac_line(h).splitlines()[1] == (
        '  실패 지정 (승인됨 — 이 기준의 숨은 케이스 faults 로 옮긴다): 주문 상태 변경 — 항목 "SHIPPED" 처음 1번 → 503 응답'
    )


def test_status_below_400_means_network_failure() -> None:
    """LLM 이 연결 실패를 0 으로 적어 스키마 위반으로 재시도를 날렸다 — 400 미만·빈 값은 연결 실패(None)로 받는다."""
    for v in (0, "", 200, None):
        assert HiddenFault(operation="가상 저장", item="가상B", status=v).status is None  # type: ignore[arg-type]
        assert Fault(endpoint="e1", status=v).status is None  # type: ignore[arg-type]
    assert HiddenFault(operation="가상 저장", item="가상B", status=503).status == 503


# ── 숨은 contract.ts 의 실패 지정 도우미 (failHcN) — 테스트는 부르기만 한다 ──


def _helper(case: dict[str, Any]) -> str:
    from task_spec.starter.hidden_helpers import render_contract_case
    from task_spec.starter.templates import load_template

    plan = HiddenPlan.model_validate(_with(case))
    f = render_contract_case(
        TaskContract.model_validate(_contract()),
        plan,
        load_template("frontend/react-vite-ts").manifest,
    )
    assert f is not None
    return f.content


def test_helper_writes_one_once_per_fault_and_time_matched_by_body() -> None:
    text = _helper(
        {
            "faults": [
                {"endpoint": "e1", "when": {"toStatus": "SHIPPED"}, "times": 2, "status": 503},
                {"endpoint": "e1", "when": {"toStatus": '메모 "C"'}, "status": None},
            ]
        }
    )
    body = text[text.index("export function failHc1(): void {") :]
    once = [ln for ln in body.splitlines() if ln.strip().startswith("api.once(")]
    assert len(once) == 3  # times 2 + 1
    assert once[0].startswith('  api.once("PATCH", "/orders/:id/status", { status: 503, json: ')
    assert '(c) => String((c.body as Record<string, unknown>)["toStatus"]) === "SHIPPED"' in once[0]
    assert (
        "{ networkError: true }" in once[2] and '=== "메모 \\"C\\""' in once[2]
    )  # 따옴표·한글 그대로


def test_helper_is_absent_without_faults() -> None:
    assert "failHc1" not in _helper({})


def test_case_file_must_call_the_helper_and_not_fail_directly() -> None:
    from task_spec.schemas import GeneratedFile
    from task_spec.starter.static_check import _fault_calls

    plan = HiddenPlan.model_validate(_with({"faults": [MAPPED]}))
    good = GeneratedFile(path="src/__grading__/cases/hc1.test.tsx", content="// hc1\nfailHc1();\n")
    assert _fault_calls(good, {"hc1"}, plan, None) == []
    bad = GeneratedFile(
        path="src/__grading__/cases/hc1.test.tsx",
        content="// hc1\nconst call = await waitForX();\nawait call.networkError();\n",
    )
    probs = _fault_calls(bad, {"hc1"}, plan, None)
    assert any("failHc1() 을 부르지 않음" in p for p in probs)
    assert any("실패를 직접 건다('.networkError(')" in p for p in probs)
    assert _fault_calls(bad, {"hc2"}, plan, None) == []  # 다른 케이스 파일은 보지 않는다


def test_material_listing_params_keeps_only_those() -> None:
    """2026-11-11 N+1 백엔드 알파: 템플릿 조절값이 모두 보여 LLM 이 무관한 concurrency=1·bursts=1 을 넣고 근거가 없어 계약 실패.
    재료가 조절값을 나열했으면 입력·검사·숨은 계획 모두 그것만 — 나열하지 않았으면([]·None) 템플릿 그대로 (lost-update 는 동시 요청을 쓴다)."""
    import json
    from pathlib import Path

    from task_spec.nodes.starter.plan_contract import fit_material_params, for_material
    from task_spec.schemas.incident import PublicIncident
    from task_spec.starter.templates import load_template

    root = Path(__file__).parents[2] / "incidents/public_v1"
    java = load_template("backend/java-spring-boot3-jpa")
    nplus1 = PublicIncident.model_validate(
        json.loads((root / "sw.backend.n-plus-one.json").read_text("utf-8"))
    ).feature
    lost = PublicIncident.model_validate(
        json.loads((root / "sw.backend.lost-update.json").read_text("utf-8"))
    ).feature
    known = {p.name for p in java.manifest.harness_params}
    t = for_material(java, nplus1)
    # 나열한 운영 값 + 숨은 장치(지원자에게 안 보이고 기본값 있는 값 — bursts): 재료 나열은 운영 값만 고른다 (좁히기는 for_material 하나)
    assert {p.name for p in t.manifest.harness_params} == {
        "query_delay_ms",
        "request_timeout_s",
        "bursts",
    }
    lt = for_material(java, lost)
    assert "concurrency" in {p.name for p in lt.manifest.harness_params}
    # 갱신 손실 재료는 버스트 조절값 concurrency 를 자료에서 읽는다 (필수·보임·이 과제 설명) — 2026-11-13
    conc = lt.manifest.param("concurrency")
    assert conc is not None and conc.required and conc.visible and "— 이 과제:" in conc.description
    # 숨은 장치(bursts)는 재료 나열과 무관하게 남아 기본값으로 채워진다 (이름이 아니라 visible·default 로 가른다)
    burst_plan = HiddenPlan.model_validate(
        _with({"data": [{"entity_id": "m1", "rows": 200, "burst": True}]})
        | {"harness": {"concurrency": 17}}
    )
    filled, filled_notes = fill_hidden_defaults(burst_plan, lt.manifest)
    assert filled.harness.get("bursts") == 3 and filled_notes
    plan = HiddenPlan.model_validate(
        _with({"data": [{"entity_id": "m1", "rows": 200, "burst": True}]})
        | {
            "harness": {
                "query_delay_ms": 12,
                "request_timeout_s": 1,
                "concurrency": 1,
                "pool_size": 10,
                "pool": 3,  # 템플릿에 없는 키 — 지우지 않고 계약 검사가 다시 묻는다 (#179)
            }
        }
    )
    fixed, notes = fit_material_params(plan, t.manifest, known)
    assert fixed.harness == {"query_delay_ms": 12, "request_timeout_s": 1, "pool": 3}
    assert not any(d.burst for c in fixed.cases for d in c.data)
    assert "concurrency, pool_size" in notes[0] and "버스트 표시도 끔" in notes[0]
    # 갱신 손실 재료는 concurrency 만 나열 — 다른 운영 값은 빼지만 버스트 장치(bursts)는 남아 버스트 표시를 끄지 않는다.
    # 지연(query_delay_ms)은 v4 부터 숨은 장치(기본값 20)라 재료 나열과 무관하게 남는다 (template_mechanics)
    fixed, notes = fit_material_params(plan, lt.manifest, known)
    assert fixed.harness == {"query_delay_ms": 12, "concurrency": 1, "pool": 3}
    assert all(d.burst for c in fixed.cases for d in c.data)
    assert "버스트 표시도 끔" not in notes[0]


def test_fault_items_must_list_test_items_and_leave_one_unfailed() -> None:
    """2026-11-11 재배정 명세(오프라인): item 을 '대기 중인 점검 메모'(항목 전체)로 적어 '그 항목만 실패'가 성립하지 않았다."""
    item = BACKEND_SEED.model_copy(update={"fault_by": "item"})

    def probs(items: list[str], faulted: list[str]) -> list[str]:
        h = HiddenAcceptance(
            ac_id="h1",
            trap_id="t1",
            text="가상 기준",
            items=items,
            faults=[HiddenFault(operation="가상 저장", item=i, status=503) for i in faulted],
        )
        return check_hidden_faults(cast(Any, SimpleNamespace(acceptance_hidden=[h])), item)

    assert any("2개 이상의 서로 다른 이름이 아님" in p for p in probs([], ["대기 중인 점검 메모"]))
    assert any("item 이 items 에 없음: 메모Z" in p for p in probs(["메모A", "메모B"], ["메모Z"]))
    assert any("모두 실패 지정됨" in p for p in probs(["메모A", "메모B"], ["메모A", "메모B"]))
    assert probs(["메모A", "메모B", "메모C"], ["메모B"]) == []


def test_contract_input_lists_the_approved_items() -> None:
    from task_spec.nodes.starter.plan_contract import hidden_ac_line

    h = HiddenAcceptance(
        ac_id="h1", trap_id="t1", text="가상 기준", items=["메모A", "메모B"], faults=[APPROVED]
    )
    assert (
        "테스트가 만들 항목 (승인됨 — 이 기준의 숨은 케이스는 이 이름 그대로 만든다): 메모A, 메모B"
        in hidden_ac_line(h)
    )
