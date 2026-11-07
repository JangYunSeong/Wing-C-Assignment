"""공개 계약·HiddenPlan 결정적 검사 (starter/contract.py, spec-to-code §3.3) — 가상 데이터만, LLM 없음."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from task_spec.schemas import FeatureSeed, HiddenPlan, TaskContract, TemplateManifest
from task_spec.spec.incidents import load_incidents
from task_spec.starter.contract import (
    SpecView,
    check_contract,
    contract_text,
    extract_values,
    hidden_terms,
    spec_missing_params,
)

ROOT = Path(__file__).resolve().parents[2]
BACKEND_SEED = load_incidents(ROOT / "incidents" / "public_v1")["sw.backend"].feature
assert BACKEND_SEED is not None


def _manifest(**over: Any) -> TemplateManifest:
    data: dict[str, Any] = {
        "template_id": "backend/test-stack",
        "version": 1,
        "interfaces": ["http"],
        "harness_params": [
            {
                "name": "pool_size",
                "kind": "int",
                "description": "운영 DB 연결 최대값",
                "minimum": 1,
                "maximum": 200,
                "visible": True,
                "spec_patterns": [
                    r"pool:\s*(\d+)",
                    r"maximumPoolSize\s*[=:]\s*(\d+)",
                    r"최대\s*연결\s*수는?\s*(\d+)",
                ],
                "config_keys": ["spring.datasource.hikari.maximum-pool-size"],
            },
            {
                "name": "concurrency",
                "kind": "int",
                "description": "피크 동시 요청 수",
                "visible": True,
                "spec_patterns": [r"threads:\s*(\d+)", r"동시\s*요청은\s*최대\s*([\d,]+)\s*건"],
            },
            {
                "name": "query_delay_ms",
                "kind": "int",
                "description": "SQL 왕복마다 더하는 지연",
                "minimum": 0,
                "maximum": 200,
                "required": False,
            },
        ],
        "failure_reasons": [
            {
                "code": "pool_acquire_timeout",
                "description": "커넥션 획득 대기 시간 초과",
                "log_patterns": ["Connection is not available", r"\bP2024\b"],
            },
            {
                "code": "rollback_left_success",
                "description": "롤백된 요청에 성공 기록이 남음",
                "log_patterns": ["SUCCESS after rollback"],
            },
        ],
    }
    data.update(over)
    return TemplateManifest.model_validate(data)


def _spec(**over: Any) -> SpecView:
    data: dict[str, Any] = {
        "req_ids": ("r1", "r2"),
        "public_ac_ids": ("p1", "p2"),
        "hidden_acs": {"h1": "t1"},
        "trap_ids": ("t1",),
        "visible_texts": ("pool: 8\nthreads: 32", "마감 직전 동시 요청은 최대 32건입니다."),
        "concepts": ("점유 범위",),
    }
    data.update(over)
    return SpecView(**data)


def _contract() -> dict[str, Any]:
    def f(name: str, typ: str, refs: list[str]) -> dict[str, Any]:
        return {"name": name, "type": typ, "refs": refs}

    return {
        "interface_kind": "http",
        "endpoints": [
            {
                "endpoint_id": "e1",
                "method": "PATCH",
                "path": "/orders/{id}/status",
                "summary": "주문 상태 변경",
                "request": [f("toStatus", "enum(READY|SHIPPED|DELIVERED)", ["r1"])],
                "responses": [
                    {
                        "status": 200,
                        "description": "변경됨",
                        "body": [f("id", "integer", ["r1"]), f("status", "string", ["p1"])],
                    },
                    {"status": 409, "description": "허용되지 않는 상태 변경"},
                ],
                "refs": ["r1", "p1"],
            },
            {
                "endpoint_id": "e2",
                "method": "GET",
                "path": "/orders/{id}/history",
                "summary": "주문별 변경 이력 최신순",
                "responses": [
                    {
                        "status": 200,
                        "description": "이력 목록",
                        "body": [f("items", "array", ["r2"])],
                    }
                ],
                "refs": ["r2", "p2"],
            },
        ],
        "entities": [
            {
                "entity_id": "m1",
                "name": "Order",
                "fields": [f("id", "integer", ["r1"]), f("status", "string", ["r1"])],
            },
            {
                "entity_id": "m2",
                "name": "OrderHistory",
                "fields": [
                    f("orderId", "integer", ["r1"]),
                    f("requestedBy", "string", ["r1"]),
                    f("fromStatus", "string", ["r1"]),
                    f("toStatus", "string", ["r1"]),
                    f("result", "enum(SUCCESS|FAILURE)", ["r1"]),
                    f("createdAt", "datetime", ["r2"]),
                ],
                "relations": ["OrderHistory.orderId → Order.id"],
            },
        ],
        "seed": [
            {
                "entity_id": "m1",
                "rows": 200,
                "groups": [{"first": 1, "last": 200, "values": {"status": "READY"}}],
            },
            {"entity_id": "m2", "rows": 0},
        ],
        "public_tests": [
            {"test_id": "pt1", "ac_id": "p1", "checks": "변경 성공 시 이력 1건"},
            {"test_id": "pt2", "ac_id": "p2", "checks": "조회가 최신순"},
        ],
        "covers": {"r1": ["e1", "m1", "m2"], "r2": ["e2", "m2"]},
    }


def _plan(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "harness": {"pool_size": 8, "concurrency": 32, "query_delay_ms": 20},
        "cases": [
            {
                "case_id": "hc1",
                "ac_id": "h1",
                "trap_id": "t1",
                "scenario": "같은 시각에 32건 상태 변경",
                "expect": "모두 기대 코드, 요청마다 기록 1건",
                "intended_reason": "pool_acquire_timeout",
                "data": [
                    {"entity_id": "m1", "rows": 96, "initial": {"status": "READY"}, "burst": True}
                ],
            }
        ],
    }
    data.update(over)
    return data


def _check(contract: dict[str, Any] | None = None, plan: dict[str, Any] | None = None, **kw: Any):  # type: ignore[no-untyped-def]
    return check_contract(
        TaskContract.model_validate(contract or _contract()),
        HiddenPlan.model_validate(plan or _plan()),
        kw.pop("spec", _spec()),
        kw.pop("manifest", _manifest()),
        kw.pop("seed", BACKEND_SEED),
    )


def _has(problems: list[str], *needles: str) -> bool:
    return any(all(n in p for n in needles) for p in problems)


def test_valid_contract_passes() -> None:
    res = _check()
    assert res.ok, (res.problems, res.spec_gaps)


def test_every_requirement_must_be_covered() -> None:
    c = _contract()
    c["covers"] = {"r1": ["e1", "m1", "m2"]}
    res = _check(c)
    assert _has(res.problems, "공개 요구 r2", "covers 에 없음")
    assert _has(res.problems, "연결되지 않은 계약 요소", "e2")  # 거꾸로도 걸림


def test_contract_elements_must_trace_back_to_spec() -> None:
    c = _contract()
    c["endpoints"].append(
        {
            "endpoint_id": "e3",
            "method": "POST",
            "path": "/orders/{id}/replay",
            "summary": "다시 보내기",
            "responses": [{"status": 202, "description": "접수"}],
            "refs": ["r1"],
        }
    )
    c["entities"][1]["fields"].append({"name": "attempt", "type": "integer", "refs": ["r9"]})
    res = _check(c)
    assert _has(res.problems, "연결되지 않은 계약 요소", "e3")
    assert _has(res.problems, "필드 attempt", "명세에 없는 id: r9")
    c = _contract()
    c["covers"]["r3"] = ["e1"]
    c["covers"]["r1"].append("e9")
    res = _check(c)
    assert _has(res.problems, "covers 의 키 r3")
    assert _has(res.problems, "covers[r1]", "e9")


def test_public_and_hidden_acceptance_need_tests() -> None:
    c = _contract()
    c["public_tests"] = [{"test_id": "pt1", "ac_id": "p1", "checks": "변경 성공"}]
    p = _plan()
    p["cases"][0] |= {"ac_id": "h2"}
    res = _check(c, p)
    assert _has(res.problems, "공개 수용 기준 p2", "공개 테스트가 없음")
    assert _has(res.problems, "숨은 케이스 hc1", "h2", "숨김 수용 기준이 아님")
    assert _has(res.problems, "숨김 수용 기준 h1", "숨은 케이스가 없음")
    p = _plan()
    p["cases"][0] |= {"trap_id": "t2"}
    res = _check(plan=p)
    assert _has(res.problems, "trap_id t2", "h1 의 숨은 요구")


def test_naming_and_interface_rules() -> None:
    c = _contract()
    c["endpoints"][0]["path"] = "/Orders/{id}"
    c["endpoints"][1]["method"] = None
    c["entities"][1]["name"] = "order_history"
    c["entities"][0]["fields"][1]["name"] = "Status"
    res = _check(c)
    assert _has(res.problems, "e1", "경로 규칙")
    assert _has(res.problems, "e2", "HTTP 메서드가 없음")
    assert _has(res.problems, "order_history", "이름 규칙")
    assert _has(res.problems, "필드 이름", "Status")
    res = _check(manifest=_manifest(interfaces=["cli"]))
    assert _has(res.problems, "인터페이스 http", "지원하지 않음")


def test_hidden_terms_are_banned_from_contract_including_identifiers() -> None:
    c = _contract()
    c["entities"][1]["fields"].append(
        {"name": "connectionPoolSize", "type": "integer", "refs": ["r1"]}
    )
    c["endpoints"][1]["summary"] = "피크 시간 이력 조회"  # 재료 trigger_terms
    c["public_tests"][0]["checks"] = "점유 범위 확인"  # 먼저 말할 개념
    res = _check(c)
    assert _has(res.problems, "공개 계약", "connection pool", "피크", "점유 범위")
    assert "connection pool size" in contract_text(TaskContract.model_validate(c))


def test_public_business_need_words_are_allowed() -> None:
    assert BACKEND_SEED is not None
    seed = BACKEND_SEED.model_copy(update={"trigger_terms": ["감사 기록", "피크"]})
    terms = hidden_terms(seed, _spec())
    assert "감사 기록" not in terms and "피크" in terms  # 감사 기록은 재료 업무 요구에 공개된 말


def test_harness_values_must_match_visible_spec_values() -> None:
    p = _plan()
    p["harness"]["pool_size"] = 10
    res = _check(plan=p)
    assert _has(res.problems, "pool_size=10", "명세 자료의 값(8)")
    assert not res.spec_gaps  # 값은 명세에 있다 — 계약 쪽 잘못이라 재요청으로 고침


def test_missing_spec_value_is_a_gap_not_a_reask() -> None:
    spec = _spec(visible_texts=("pool: 8",))  # 동시 요청 수가 자료·답 어디에도 없음
    res = _check(spec=spec)
    assert not res.problems
    assert _has(res.spec_gaps, "concurrency", "명세 수정 필요")
    assert spec_missing_params(spec.visible_texts, _manifest()) == ["concurrency"]


def test_extract_values_uses_patterns_not_any_number() -> None:
    m = _manifest()
    pool = m.param("pool_size")
    conc = m.param("concurrency")
    assert pool is not None and conc is not None
    texts = ["버전 8 배포, 서버 8대", "maximumPoolSize = 12"]  # '8' 은 풀 크기가 아니다
    assert extract_values(pool, texts) == ["12"]
    assert extract_values(conc, ["마감 직전 동시 요청은 최대 1,200 건"]) == ["1,200"]
    p = _plan()
    p["harness"]["concurrency"] = 1200
    spec = _spec(visible_texts=("pool: 8", "마감 직전 동시 요청은 최대 1,200건"))
    assert _check(plan=p, spec=spec).ok  # 천 단위 쉼표는 같은 수


def test_harness_keys_types_ranges_and_reasons() -> None:
    p = _plan(
        harness={"pool": 8, "concurrency": "32", "query_delay_ms": 999},
        cases=[{**_plan()["cases"][0], "intended_reason": "timeout"}],
    )
    res = _check(plan=p)
    assert _has(res.problems, "템플릿에 없는 조절값", "pool")
    assert _has(res.problems, "필수 조절값 pool_size")
    assert _has(res.problems, "concurrency", "형식이 int")
    assert _has(res.problems, "query_delay_ms=999", "상한")
    assert _has(res.problems, "의도한 실패 사유", "timeout", "pool_acquire_timeout")


def test_duplicate_ids_are_problems() -> None:
    c = _contract()
    c["endpoints"][1]["endpoint_id"] = "e1"
    c["covers"] = {"r1": ["e1", "m1", "m2"], "r2": ["m2"]}
    res = _check(c)
    assert _has(res.problems, "엔드포인트 id 이 겹침", "e1")


def test_spec_view_from_approved_feature_task() -> None:
    from task_spec.spec.draft import DraftResult
    from task_spec.spec.feature import apply_feature
    from tests.spec.test_feature import _out, _skeleton

    task = apply_feature(_skeleton(), DraftResult(output=_out(), provenance_id="p"))
    view = SpecView.from_task(task)
    assert view.req_ids == ("r1", "r2") and view.public_ac_ids == ("p1", "p2")
    assert view.hidden_acs == {"h1": "t1"} and view.trap_ids == ("t1",)
    assert "pool: 8\nthreads: 32" in view.visible_texts  # 1단계 자료 본문
    assert "마감 직전 수십 건이 동시에 들어온다" in view.visible_texts  # 추가 요구 구체 답
    assert "점유 범위" in view.concepts
    # 이 명세로 만든 계약은 운영 수치가 자료와 맞는다
    res = _check(spec=view)
    assert not res.spec_gaps and not _has(res.problems, "명세 자료의 값")


def test_seedless_check_still_bans_concepts() -> None:
    c = _contract()
    c["public_tests"][0]["checks"] = "점유 범위 확인"
    res = _check(c, seed=None)
    assert _has(res.problems, "점유 범위")


@pytest.mark.parametrize("seed", [BACKEND_SEED])
def test_backend_seed_terms_are_collected(seed: FeatureSeed) -> None:
    terms = hidden_terms(seed, _spec())
    assert "커넥션 풀" in terms and "동시 요청" in terms and "점유 범위" in terms


# ── 표 열·헤더·선택 조절값 (3단계 스모크 준비, 2026-11-07) ──


def test_table_columns_are_read() -> None:
    from task_spec.schemas import HarnessParam

    p = HarnessParam.model_validate(
        {
            "name": "concurrency",
            "kind": "int",
            "description": "d",
            "visible": True,
            "spec_columns": [r"in_?flight(?:_requests)?"],
        }
    )
    spaced = "time   rps  inflight_requests  p95_ms\n17:20  14   6   41\n18:00  103  32  95\n"
    md = "| 시각 | inflight_requests |\n|---|---|\n| 17:20 | 6 |\n| 18:00 | 1,032 |\n"
    assert extract_values(p, [spaced]) == ["6", "32"]
    assert extract_values(p, [md]) == ["6", "1,032"]
    assert extract_values(p, ["inflight_requests 는 32 쯤"]) == []  # 표가 아니면 열로 읽지 않는다


def test_headers_have_their_own_naming_rule() -> None:
    c = _contract()
    c["endpoints"][0]["headers"] = [{"name": "X-Actor", "type": "string", "refs": ["r1"]}]
    assert _check(c).ok
    c["endpoints"][0]["headers"] = [{"name": "actor", "type": "string", "refs": ["r1"]}]
    res = _check(c)
    assert _has(res.problems, "헤더 이름", "actor")


def test_invented_optional_visible_value_is_a_problem_not_a_gap() -> None:
    m = _manifest()
    params = [p.model_dump() for p in m.harness_params] + [
        {
            "name": "server_threads",
            "kind": "int",
            "description": "요청 스레드",
            "required": False,
            "visible": True,
            "spec_patterns": [r"maxThreads\s*=\s*(\d+)"],
        }
    ]
    p = _plan()
    p["harness"]["server_threads"] = 64  # 자료 어디에도 없음
    res = _check(plan=p, manifest=_manifest(harness_params=params))
    assert not res.spec_gaps and _has(res.problems, "server_threads=64", "선택 조절값")


# ── 스모크(v1)에서 드러난 빈틈 — 기존 API 누락·운영 값 누락·목록 응답 (2026-11-07) ──


def test_api_named_in_spec_must_be_in_contract() -> None:
    spec = _spec(public_texts=("기존 API(`PATCH /orders/{id}/status`)의 동작은 그대로 둔다.",))
    assert _check(spec=spec).ok  # 계약에 e1 PATCH /orders/{id}/status 가 있음
    c = _contract()
    c["endpoints"] = [c["endpoints"][1]]  # 조회만 남김
    c["covers"] = {"r1": ["e2", "m1", "m2"], "r2": ["e2", "m2"]}
    res = _check(c, spec=spec)
    assert _has(res.problems, "명세", "PATCH /orders/{}/status", "계약에 없음")


def test_tests_and_cases_may_only_use_contract_apis() -> None:
    c = _contract()
    c["public_tests"][0]["checks"] = "POST /orders 로 만든 뒤 확인"
    p = _plan()
    p["cases"][0]["scenario"] = "DELETE /orders/{id} 를 동시에 32건"
    res = _check(c, p)
    assert _has(res.problems, "계약에 없는 API", "POST /orders", "DELETE /orders/{}")


def test_visible_operating_value_in_spec_must_be_in_plan() -> None:
    m = _manifest()
    params = [p.model_dump() for p in m.harness_params] + [
        {
            "name": "server_threads",
            "kind": "int",
            "description": "요청 스레드",
            "required": False,
            "visible": True,
            "spec_patterns": [r"maxThreads\s*=\s*(\d+)"],
        }
    ]
    spec = _spec(visible_texts=("pool: 8\nthreads: 32\nmaxThreads=32",))
    res = _check(spec=spec, manifest=_manifest(harness_params=params))
    assert _has(res.problems, "운영 값 server_threads(32)", "넣는다")


def test_returns_must_point_to_contract_entity() -> None:
    c = _contract()
    c["endpoints"][1]["responses"][0] |= {"body": [], "returns": "m2[]"}
    assert _check(c).ok
    c["endpoints"][1]["responses"][0]["returns"] = "m9[]"
    assert _has(_check(c).problems, "returns", "m9[]")


def test_concrete_ids_in_tests_match_contract_path_variables() -> None:
    """2026-11-07 v3 스모크: 공개 테스트·숨은 케이스가 `PATCH /orders/15/status` 처럼 구체 id 를 쓰면 오탐했다."""
    c = _contract()
    c["public_tests"][0]["checks"] = "PATCH /orders/1/status 후 GET /orders/1/history 가 1건"
    p = _plan()
    p["cases"][0]["scenario"] = "PATCH /orders/150/status 를 32건 동시에"
    assert not _has(_check(c, p).problems, "계약에 없는 API")
    p["cases"][0]["scenario"] = "DELETE /orders/150 과 GET /orders/150/status"
    assert _has(
        _check(c, p).problems, "계약에 없는 API", "DELETE /orders/150", "GET /orders/150/status"
    )
