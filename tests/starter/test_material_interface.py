"""재료 인터페이스 (#256 1단계) — 재료가 정한 명령·입출력 파일·엔티티를 코드가 계약에 채우고(fit_interface), 계약 검사가 지킨다.
가상 데이터만 (LLM 답 꼴은 2026-11-18 데이터 알파의 모양을 가상 이름으로 옮긴 것)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.builder import steps as steps_mod
from task_spec.nodes.starter.plan_contract import PlanResult, fit_output, for_material, plan_input
from task_spec.schemas import (
    MaterialInterface,
    PlanContractOutput,
    PublicTest,
    TaskContract,
)
from task_spec.schemas.build import Fault
from task_spec.spec.feature import find_terms
from task_spec.spec.incidents import incident_by_id, load_incidents, load_interfaces
from task_spec.starter.contract import _leaks, check_contract
from task_spec.starter.interface import (
    FLAG_NO_INTERFACE,
    fit_interface,
    interface_problems,
    without_fixed,
)
from task_spec.starter.templates import TEMPLATE_FOR_SUB, load_template
from tests.builder.test_build_provenance import _job, _real_steps
from tests.starter.test_contract import _spec
from tests.starter.test_data_template import DE, _batch_seed, _contract, _plan
from tests.starter.test_plan_contract import _task

INCIDENTS = Path("incidents/public_v1")
BATCH = "pub:data.eng:batch-rerun-late-partial"
REQ, ACS = ("r1", "r2"), ("p1", "p2")  # _spec() 의 공개 요구·수용 기준


def _iface() -> MaterialInterface:
    return load_interfaces(INCIDENTS)[BATCH]


def _manifest() -> Any:
    return for_material(DE, _batch_seed()).manifest  # plan_contract 와 같게 재료로 좁힌 매니페스트


def _f(name: str, typ: str, refs: tuple[str, ...] = ("r1",)) -> dict[str, Any]:
    return {"name": name, "type": typ, "refs": list(refs)}


def _llm_contract() -> TaskContract:
    """LLM 답 — 칸 이름·출력 위치를 스스로 정하고, 스케줄 명령과 env 입력 환경을 더했다 (2026-11-18 알파 꼴)."""
    return TaskContract.model_validate(
        {
            "interface_kind": "cli",
            "endpoints": [
                {
                    "endpoint_id": "e1",
                    "path": "pipeline/run.py",
                    "summary": "지정한 날짜의 매장별 매출 요약을 산출한다",
                    "request": [
                        _f("raw_dir", "string"),
                        _f("out_dir", "string", ("r2",)),
                        _f("run_date", "date"),
                    ],
                    "responses": [
                        {"status": 0, "description": "완료"},
                        {"status": 1, "description": "실패"},
                    ],
                    "refs": ["r1", "r2"],
                },
                {
                    "endpoint_id": "e2",
                    "path": "pipeline/setup_schedule.py",
                    "summary": "매일 실행되도록 등록한다",
                    "request": [_f("schedule_time", "string", ("r2",))],
                    "responses": [{"status": 0, "description": "등록됨"}],
                    "refs": ["r2"],
                },
            ],
            "entities": [
                {
                    "entity_id": "m1",
                    "name": "PaymentEvent",
                    "fields": [
                        _f("payment_time", "datetime"),
                        _f("store_id", "string", ("r2",)),
                        _f("transaction_id", "string"),
                    ],
                },
                {
                    "entity_id": "m2",
                    "name": "DailySalesSummary",
                    "fields": [
                        _f("payment_date", "date", ("r2",)),
                        _f("total_amount", "integer", ("r2",)),
                    ],
                },
            ],
            "fixtures": [
                {
                    "fixture_id": "f1",
                    "kind": "file",
                    "summary": "원시 결제 이벤트 jsonl — 한 줄에 PaymentEvent 하나",
                    "path": "{raw_dir}/{run_date}/*.jsonl",
                    "refs": ["r1"],
                },
                {
                    "fixture_id": "f2",
                    "kind": "file",
                    "summary": "매출 요약 parquet — 한 행에 DailySalesSummary 하나",
                    "path": "{out_dir}/{run_date}/*.parquet",
                    "refs": ["r2"],
                },
                {"fixture_id": "f3", "kind": "env", "summary": "배치 실행 설정", "refs": ["r2"]},
            ],
            "public_tests": [
                {
                    "test_id": "pt1",
                    "ac_id": "p1",
                    "checks": "날짜 순서대로 실행하면 요약이 정의대로",
                },
                {"test_id": "pt2", "ac_id": "p2", "checks": "결제가 없는 매장은 행이 없다"},
            ],
            "covers": {"r1": ["e1", "m1"], "r2": ["e2", "m2"]},
        }
    )


def _clean_llm_contract() -> TaskContract:
    """고정 밖 요소(스케줄 명령·env 입력 환경)를 뺀 LLM 답 — 이름·위치만 재료와 다르다."""
    base = _llm_contract()
    return base.model_copy(
        update={
            "endpoints": base.endpoints[:1],
            "fixtures": base.fixtures[:2],
            "covers": {"r1": ["e1", "m1"], "r2": ["m2"]},
        }
    )


# ── 재료 파일 ──────────────────────────────────────────────


def test_every_interface_belongs_to_a_data_material_with_a_feature() -> None:
    """1단계는 데이터 재료만 — Java·React 재료는 #102 에서 정한다."""
    interfaces = load_interfaces(INCIDENTS)
    assert BATCH in interfaces
    catalog = load_incidents(INCIDENTS)
    for incident_id in interfaces:
        inc = incident_by_id(catalog, incident_id)
        assert inc is not None and inc.feature is not None, incident_id
        # 공고 배정 전(templates 가 빈 평가 전용) 재료도 빌드는 직군 템플릿으로 만든다 (#256 5단계)
        assert TEMPLATE_FOR_SUB.get(inc.sub, "").startswith("data/"), incident_id
        assert all(t.startswith("data/") for t in inc.templates), incident_id


def test_interface_text_never_names_the_hidden_requirement() -> None:
    """재료 글은 공개 계약에 그대로 나간다 — 숨은 요구·원인·운영 조건 낱말이 없어야 한다 (누설 검사는 고정 부분을 보지 않는다)."""
    catalog = load_incidents(INCIDENTS)
    for incident_id, spec in load_interfaces(INCIDENTS).items():
        inc = incident_by_id(catalog, incident_id)
        assert inc is not None and inc.feature is not None
        f = inc.feature
        terms = [
            *f.trigger_terms,
            *f.leak_terms,
            *f.stage2_cause_terms,
            *inc.hidden_terms,
            *inc.avoid_terms,
        ]
        text = json.dumps(spec.model_dump(), ensure_ascii=False)
        assert find_terms(terms, text) == [], incident_id


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["files"][0].update(path="{raw_dir}/{date}/*.jsonl"), "명령 옵션이 아님"),
        (lambda d: d["files"][1].update(entity="Summary"), "entities 에 없음"),
        (
            lambda d: d["files"][1].update(content="csv 또는 parquet, 한 행에 DailySales 하나"),
            "형식",
        ),
        (
            lambda d: d["files"][0].update(summary="원시 결제 이벤트 jsonl", content="jsonl"),
            "행 엔티티 이름",
        ),
        (lambda d: d["entities"][1].update(entity_id="m1"), "겹침"),
    ],
)
def test_interface_rejects_what_the_contract_check_would_send_back(
    change: Callable[[dict[str, Any]], None], message: str
) -> None:
    """코드가 채운 계약은 LLM 이 고칠 수 없다 — 계약 검사가 되돌릴 것을 재료 단계에서 막는다."""
    d = _iface().model_dump()
    change(d)
    with pytest.raises(ValidationError, match=message):
        MaterialInterface.model_validate(d)


# ── 계약에 채우기 ──────────────────────────────────────────────


def test_fit_overwrites_what_the_material_fixed_and_keeps_extras_for_the_check() -> None:
    out = PlanContractOutput(contract=_llm_contract(), hidden_plan=_plan())
    fitted, notes = fit_interface(out, _iface(), REQ, ACS)
    c = fitted.contract
    e1 = c.endpoints[0]
    assert (e1.endpoint_id, e1.path, [f.name for f in e1.request]) == (
        "e1",
        "pipeline/run.py",
        ["date", "raw", "out"],
    )
    assert [m.name for m in c.entities] == ["PaymentEvent", "DailySales"]
    assert [f.name for f in c.entities[1].fields] == ["store_id", "orders", "sales_amount"]
    assert [fx.path for fx in c.fixtures[:2]] == [
        "{raw}/{date}/*.jsonl",
        "{out}/daily_sales/date=YYYY-MM-DD/*.parquet",
    ]
    assert notes == [
        "재료 인터페이스로 변경 — 명령 e1(옵션), 파일 f1(위치), 파일 f2(위치), 엔티티 m1(칸), 엔티티 m2(이름·칸)"
    ]
    # 고정 밖 요소는 지우지 않는다 — 그 요소에 기대던 공개 테스트를 LLM 이 다시 짜도록 검사가 재요청한다
    assert [e.endpoint_id for e in c.endpoints] == ["e1", "e2"]
    assert [fx.fixture_id for fx in c.fixtures] == ["f1", "f2", "f3"]
    problems, flags = interface_problems(c, _iface(), DE.manifest)
    assert flags == [] and len(problems) == 1
    assert (
        "명령 e2 pipeline/setup_schedule.py" in problems[0] and "입력 환경 f3(env)" in problems[0]
    )


def test_refs_come_from_the_llm_answer() -> None:
    """refs 는 명세마다 다르다 — 같은 이름 칸은 그 칸의 refs, 나머지 칸은 짝지은 요소의 refs, 명세에 없는 id 는 버린다."""
    out = PlanContractOutput(contract=_llm_contract(), hidden_plan=_plan())
    c = fit_interface(out, _iface(), REQ, ACS)[0].contract
    m1 = c.entities[0]
    refs = {f.name: f.refs for f in m1.fields}
    assert refs["store_id"] == ["r2"]  # 같은 이름 칸
    assert refs["paid_at"] == ["r1", "r2"]  # LLM 엔티티 칸 refs 의 합
    assert c.endpoints[0].refs == ["r1", "r2"] and c.fixtures[1].refs == ["r2"]
    only_r9 = _clean_llm_contract()
    e = only_r9.endpoints[0].model_copy(update={"refs": ["r9"], "request": []})
    lone = only_r9.model_copy(update={"endpoints": [e]})
    got = fit_interface(PlanContractOutput(contract=lone, hidden_plan=_plan()), _iface(), REQ, ACS)
    assert got[0].contract.endpoints[0].refs == list(REQ)  # 쓸 refs 가 없으면 공개 요구 전체


def test_ids_the_llm_chose_are_renamed_in_covers_and_hidden_cases() -> None:
    base = _clean_llm_contract()
    e = base.endpoints[0].model_copy(update={"endpoint_id": "e3"})
    m = base.entities[1].model_copy(update={"entity_id": "m5", "name": "DailySales"})
    llm = base.model_copy(
        update={
            "endpoints": [e],
            "entities": [base.entities[0], m],
            "covers": {"r1": ["e3"], "r2": ["m5", "e3"]},
        }
    )
    plan = _plan()
    plan = plan.model_copy(
        update={"cases": [plan.cases[0].model_copy(update={"faults": [Fault(endpoint="e3")]})]}
    )
    fitted, _ = fit_interface(
        PlanContractOutput(contract=llm, hidden_plan=plan), _iface(), REQ, ACS
    )
    assert fitted.contract.covers == {"r1": ["e1"], "r2": ["m2", "e1"]}
    assert fitted.hidden_plan.cases[0].faults[0].endpoint == "e1"
    assert interface_problems(fitted.contract, _iface(), DE.manifest) == ([], [])


def test_a_contract_filled_from_the_material_passes_the_file_harness_and_leak_checks() -> None:
    out, notes = fit_output(
        PlanContractOutput(contract=_clean_llm_contract(), hidden_plan=_plan()),
        _manifest(),
        _spec(),
        _batch_seed(),
        interface=_iface(),
    )
    assert notes and notes[0].startswith("재료 인터페이스로 변경")
    res = check_contract(
        out.contract, out.hidden_plan, _spec(), _manifest(), _batch_seed(), interface=_iface()
    )
    assert (res.problems, res.spec_gaps) == (
        [],
        [],
    )  # 이름 규칙·명령 경로·파일·하네스 범위·누설·연결 검사 전부
    assert interface_problems(out.contract, _iface(), _manifest()) == ([], [])


def test_without_an_interface_nothing_changes_and_file_templates_are_flagged() -> None:
    out = PlanContractOutput(contract=_llm_contract(), hidden_plan=_plan())
    same, notes = fit_interface(out, None, REQ, ACS)
    assert same is out and notes == []
    assert without_fixed(out.contract, None) is out.contract
    assert interface_problems(out.contract, None, DE.manifest) == ([], [FLAG_NO_INTERFACE])
    java = load_template("backend/java-spring-boot3-jpa").manifest
    assert interface_problems(out.contract, None, java) == ([], [])


def test_leak_check_skips_the_material_text_but_not_what_the_llm_wrote() -> None:
    """명세 개념어가 재료 글에 우연히 있어도 LLM 은 고칠 수 없다 — 누설 검사는 고정 부분을 빼고 본다."""
    spec = _spec(concepts=("파티션",))  # 재료 출력 파일 글에 '파티션'
    out, _ = fit_output(
        PlanContractOutput(contract=_clean_llm_contract(), hidden_plan=_plan()),
        _manifest(),
        spec,
        _batch_seed(),
        interface=_iface(),
    )
    assert _leaks(out.contract, spec, _batch_seed())  # 빼지 않으면 걸린다
    res = check_contract(
        out.contract, out.hidden_plan, spec, _manifest(), _batch_seed(), interface=_iface()
    )
    assert not [p for p in res.problems if "드러내는 말" in p]
    leaky = out.contract.model_copy(
        update={
            "public_tests": [PublicTest(test_id="pt1", ac_id="p1", checks="파티션마다 요약을 확인")]
        }
    )
    res = check_contract(
        leaky, out.hidden_plan, spec, _manifest(), _batch_seed(), interface=_iface()
    )
    assert [p for p in res.problems if "드러내는 말" in p]


# ── 계약 계획 입력·빌더 ──────────────────────────────────────────────


def test_plan_input_shows_the_fixed_contract_only_when_the_material_has_one() -> None:
    inp = plan_input(_task(), DE, seed=_batch_seed(), interface=_iface())
    for text in (inp.seed_rules, inp.row_rules):  # type: ignore[attr-defined]
        assert "재료가 정한 공개 계약" in text
        assert "`{out}/daily_sales/date=YYYY-MM-DD/*.parquet`" in text
        assert "엔티티 m2 DailySales — store_id(string — 매장)" in text
    plain = plan_input(_task(), DE, seed=_batch_seed())
    assert "재료가 정한 공개 계약" not in plain.seed_rules
    assert plain == plan_input(_task(), DE, seed=_batch_seed(), interface=None)


async def test_contract_step_hands_the_material_interface_to_plan_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps, job = _real_steps(tmp_path), await _job(tmp_path)  # 재료 batch-rerun 명세
    seen: dict[str, Any] = {}

    async def fake(*_: Any, **kw: Any) -> PlanResult:
        seen.update(kw)
        return PlanResult(contract=_contract(), hidden_plan=_plan())

    monkeypatch.setattr(steps_mod, "plan_contract", fake)
    c = await steps.contract(job)
    assert seen["interface"] == _iface() and c.interface == _iface()


def test_tests_that_still_use_the_llm_names_are_sent_back() -> None:
    """코드가 계약 이름을 재료 것으로 바꿔도 테스트 글은 그대로다 — LLM 이 지은 옛 이름이 남으면 재요청한다."""
    from task_spec.starter.interface import stale_name_problems

    base = _clean_llm_contract()
    stale = base.model_copy(
        update={
            "public_tests": [
                PublicTest(
                    test_id="pt1", ac_id="p1", checks="run_date 마다 total_amount 합이 정의대로"
                )
            ]
        }
    )
    (problem,) = stale_name_problems(
        PlanContractOutput(contract=stale, hidden_plan=_plan()), _iface()
    )
    assert "run_date, total_amount" in problem and "옵션 date" in problem
    fine = base.model_copy(
        update={
            "public_tests": [
                PublicTest(test_id="pt1", ac_id="p1", checks="date 마다 sales_amount 합")
            ]
        }
    )
    assert (
        stale_name_problems(PlanContractOutput(contract=fine, hidden_plan=_plan()), _iface()) == []
    )
    assert stale_name_problems(PlanContractOutput(contract=stale, hidden_plan=_plan()), None) == []


def test_stale_names_are_caught_even_with_a_korean_particle_attached() -> None:
    """\\w 는 한글에도 맞는다 — 이름 경계를 ASCII 로 봐야 `run_date로` 같은 글을 잡는다."""
    from task_spec.starter.interface import stale_name_problems

    base = _clean_llm_contract()
    glued = base.model_copy(
        update={
            "public_tests": [
                PublicTest(
                    test_id="pt1", ac_id="p1", checks="run_date로 실행하고 total_amount의 합을 본다"
                )
            ]
        }
    )
    (problem,) = stale_name_problems(
        PlanContractOutput(contract=glued, hidden_plan=_plan()), _iface()
    )
    assert "run_date, total_amount" in problem


def test_an_extra_that_reuses_a_material_id_gets_a_free_id_and_is_sent_back() -> None:
    """LLM 이 e1 을 스케줄 명령에, e2 를 재료 명령에 쓰면 — 겹친 채 두면 id 가 둘이 되고 검사가 고정 밖 요소를 지목하지 못한다."""
    base = _llm_contract()
    run, schedule = base.endpoints
    llm = base.model_copy(
        update={
            "endpoints": [
                schedule.model_copy(update={"endpoint_id": "e1"}),
                run.model_copy(update={"endpoint_id": "e2"}),
            ],
            "covers": {"r1": ["e2", "m1"], "r2": ["e1", "m2"]},
        }
    )
    fitted, _ = fit_interface(
        PlanContractOutput(contract=llm, hidden_plan=_plan()), _iface(), REQ, ACS
    )
    c = fitted.contract
    assert [(e.endpoint_id, e.path) for e in c.endpoints] == [
        ("e1", "pipeline/run.py"),
        ("e3", "pipeline/setup_schedule.py"),
    ]
    assert c.covers == {"r1": ["e1", "m1"], "r2": ["e3", "m2"]}  # 참조도 같이 바뀐다
    problems, _ = interface_problems(c, _iface(), DE.manifest)
    assert len(problems) == 1 and "명령 e3 pipeline/setup_schedule.py" in problems[0]


def test_two_candidates_are_not_guessed() -> None:
    """짝 후보가 여럿이면 짝짓지 않는다 — 재료 명령을 새로 넣고 LLM 명령 둘은 고정 밖 요소로 남는다."""
    base = _clean_llm_contract()
    a = base.endpoints[0].model_copy(update={"endpoint_id": "e5", "path": "pipeline/a.py"})
    b = base.endpoints[0].model_copy(update={"endpoint_id": "e6", "path": "pipeline/b.py"})
    llm = base.model_copy(update={"endpoints": [a, b], "covers": {"r1": ["e5"], "r2": ["e6"]}})
    fitted, notes = fit_interface(
        PlanContractOutput(contract=llm, hidden_plan=_plan()), _iface(), REQ, ACS
    )
    assert [e.endpoint_id for e in fitted.contract.endpoints] == ["e1", "e5", "e6"]
    assert fitted.contract.endpoints[0].refs == list(REQ)  # 짝이 없으면 공개 요구 전체
    assert "명령 e1(새로 넣음)" in notes[0]


async def test_plan_contract_sends_back_stale_test_names_then_takes_the_fixed_answer() -> None:
    """루프 끝까지 — 첫 답의 테스트 글이 옛 이름을 쓰면 재요청하고, 재료 이름으로 고친 답을 받아 재료 인터페이스 계약으로 끝난다."""
    import json as _json

    from task_spec.llm import Completion, FakeBackend
    from task_spec.nodes.starter.plan_contract import plan_contract
    from tests.spec.test_feature import _client
    from tests.starter.test_plan_contract import PROMPTS

    def answer(checks: str) -> Completion:
        c = _clean_llm_contract()
        c = c.model_copy(
            update={
                "public_tests": [
                    PublicTest(test_id="pt1", ac_id="p1", checks=checks),
                    PublicTest(test_id="pt2", ac_id="p2", checks="결제가 없는 매장은 행이 없다"),
                ]
            }
        )
        body = {
            "contract": c.model_dump(mode="json"),
            "hidden_plan": _plan().model_dump(mode="json"),
        }
        return Completion(_json.dumps(body, ensure_ascii=False))

    fake = FakeBackend().push(
        answer("run_date로 실행하면 total_amount 합이 정의대로"),
        answer("date 순서대로 실행하면 sales_amount 합이 정의대로"),
    )
    res = await plan_contract(
        _task(),
        DE,
        llm=_client(fake),
        prompts=PROMPTS,
        ontology_version="onto-test",
        seed=_batch_seed(),
        interface=_iface(),
    )
    assert len(fake.calls) == 2
    assert any("재료 인터페이스에 없는 이름" in p for p in res.first_problems)
    assert res.ok and res.contract is not None
    assert res.contract.fixtures[1].path == "{out}/daily_sales/date=YYYY-MM-DD/*.parquet"
