"""공개 계약·HiddenPlan 결정적 검사 (starter/contract.py, spec-to-code §3.3) — 가상 데이터만, LLM 없음."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from task_spec.nodes.starter.plan_contract import fit_unevidenced
from task_spec.schemas import FeatureSeed, HarnessRule, HiddenPlan, TaskContract, TemplateManifest
from task_spec.spec.incidents import load_incidents
from task_spec.starter.contract import (
    SpecView,
    check_contract,
    contract_text,
    describe_rule,
    evidence_check,
    evidence_fragment,
    extract_values,
    fit_covers,
    hidden_terms,
    rule_check,
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
                    {
                        "status": 409,
                        "description": "허용되지 않는 상태 변경 — 허용 전이: READY→SHIPPED, SHIPPED→DELIVERED",
                    },
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
    """근거 인용 (2026-11-07): 패턴이 못 찾아도 LLM 이 근거를 대게 하고, 근거가 '없음'이면 그때 명세 결함."""
    spec = _spec(visible_texts=("pool: 8",))  # 동시 요청 수가 자료·답 어디에도 없음
    plan = _plan()
    plan["evidence"] = {"concurrency": "없음"}
    res = _check(plan=plan, spec=spec)
    assert not res.problems
    assert _has(res.spec_gaps, "concurrency", "명세 수정 필요")
    assert spec_missing_params(spec.visible_texts, _manifest()) == ["concurrency"]
    # 근거를 아예 안 적으면 명세 결함으로 단정하지 않고 근거를 요구 (재요청)
    res = _check(spec=spec)
    assert not res.spec_gaps and _has(res.problems, "concurrency", "근거가 없음")


def test_evidence_quote_confirms_values_patterns_miss() -> None:
    """2026-11-07 종이 단계: 'active_requests=40', '동시 요청 수가 최대 40건에' 를 템플릿 패턴이 못 읽어 LLM 전에 실패했다."""
    text = "피크 시간대에는 동시 요청 수가 최대 40건에 도달합니다."
    spec = _spec(visible_texts=("pool: 8", text))
    plan = _plan(harness={"pool_size": 8, "concurrency": 40, "query_delay_ms": 20})
    plan["evidence"] = {"concurrency": "동시 요청 수가 최대 40건에 도달합니다"}
    assert not _check(plan=plan, spec=spec).problems
    for quote, value in (
        ("동시 요청 수가 최대 40건에 도달합니다", 32),
        ("동시 요청은 최대 40건", 40),
        ("40", 40),
    ):
        plan = _plan(harness={"pool_size": 8, "concurrency": value, "query_delay_ms": 20})
        plan["evidence"] = {"concurrency": quote}
        assert _has(_check(plan=plan, spec=spec).problems, "concurrency", "근거"), quote


DIST = "동시 실행 창 수 분포: 1개: 38% 2개: 41% 3개: 14% 4개 이상: 7%"  # 가상 사용 통계 (알파 테스트 형식)


def _tabs_manifest(pick: str) -> TemplateManifest:
    m = _manifest().model_dump()
    m["harness_params"][1] |= {"spec_patterns": [r"(\d+)\s*개\s*이상"], "pick": pick}
    return TemplateManifest.model_validate(m)


@pytest.mark.parametrize(("value", "ok"), [(4, True), (3, False), (2, False), (1, False)])
def test_pattern_value_wins_over_any_number_in_quote(value: int, ok: bool) -> None:
    """알파 테스트 2026-11-07: 분포 전체를 인용하면 1~4 아무것이나 통과했다 — 패턴 값(pick=max)만 정답."""
    spec = _spec(visible_texts=("pool: 8", DIST, "3개 이상은 21%, 2개 이상 비율은 71%"))
    plan = _plan(harness={"pool_size": 8, "concurrency": value})
    plan["evidence"] = {"concurrency": DIST}
    res = _check(plan=plan, spec=spec, manifest=_tabs_manifest("max"))
    assert res.ok is ok, res.problems
    if not ok:
        assert _has(res.problems, f"concurrency={value}", "명세 자료의 값(4)")


def test_unique_pick_with_several_values_is_flagged() -> None:
    spec = _spec(visible_texts=("pool: 8", "3개 이상 21%", "4개 이상 7%"))
    plan = _plan(harness={"pool_size": 8, "concurrency": 4})
    res = _check(plan=plan, spec=spec, manifest=_tabs_manifest("unique"))
    assert res.ok and _has(res.review_flags, "concurrency", "후보가 자료에 여럿(3, 4)")
    plan = _plan(harness={"pool_size": 8, "concurrency": 2})
    assert _has(
        _check(plan=plan, spec=spec, manifest=_tabs_manifest("unique")).problems, "concurrency=2"
    )


def test_evidence_check_ignores_percent_and_time_and_flags_ambiguity() -> None:
    texts = [DIST, "피크(19:00~22:00) 동시 요청 40건", "17–18시 피크, 풀 8, 스레드 32"]
    assert evidence_check(1, DIST, texts) == "ambiguous"  # 1·2·3·4 가 남음
    assert evidence_check(38, DIST, texts) == "bad"  # 38% 는 값 후보가 아니다
    assert evidence_check(40, "피크(19:00~22:00) 동시 요청 40건", texts) == "ok"
    assert evidence_check(8, "17–18시 피크, 풀 8, 스레드 32", texts) == "ambiguous"
    assert evidence_check(4, DIST * 5, [DIST * 5]) == "bad"  # MAX_QUOTE 넘는 통째 인용
    spec = _spec(visible_texts=("pool: 8", "17–18시 피크, 풀 8, 스레드 32"))
    plan = _plan(harness={"pool_size": 8, "concurrency": 32})  # 패턴 없음 → 근거 인용 경로
    plan["evidence"] = {"concurrency": "17–18시 피크, 풀 8, 스레드 32"}
    res = _check(plan=plan, spec=spec)
    assert res.ok and _has(res.review_flags, "concurrency=32", "숫자가 여럿")


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
    c["endpoints"][0]["headers"] = [
        {"name": "X-Actor", "type": "string", "per_request": False, "refs": ["r1"]}
    ]
    assert _check(c).ok
    c["endpoints"][0]["headers"] = [
        {"name": "actor", "type": "string", "per_request": False, "refs": ["r1"]}
    ]
    res = _check(c)
    assert _has(res.problems, "헤더 이름", "actor")
    # 요청마다 새 값인지(per_request)를 적지 않은 헤더는 재요청 (#181 — 이름 낱말로 짐작하지 않는다)
    c["endpoints"][0]["headers"] = [{"name": "X-Actor", "type": "string", "refs": ["r1"]}]
    assert _has(_check(c).problems, "X-Actor", "per_request 가 없음")


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
    assert not res.spec_gaps and _has(res.problems, "server_threads=64", "근거가 없음")
    # 근거를 '없음'으로 쓴 선택 조절값은 보정(fit_unevidenced)이 뺀다 — 검사에는 같은 규칙의 가지를 두지 않는다 (#179)
    from task_spec.nodes.starter.plan_contract import fit_unevidenced

    p["evidence"] = {"server_threads": "없음"}
    manifest = _manifest(harness_params=params)
    fixed, notes = fit_unevidenced(HiddenPlan.model_validate(p), manifest, _spec())
    assert "server_threads" not in fixed.harness and any("server_threads" in n for n in notes)
    res = _check(plan=fixed.model_dump(), manifest=manifest)
    assert not res.spec_gaps and not _has(res.problems, "server_threads")


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
    assert _has(
        res.problems,
        "계약에 없는 API",
        "POST /orders (pt1)",
        "DELETE /orders/{id} (hc1)",
        "계약에 있는 API(PATCH /orders/{id}/status",
    )


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


def test_changing_api_needs_target_rows_and_case_data() -> None:
    """2026-11-07 알파 테스트 (새 공고): covers 재요청에 대상 엔티티를 지워 감사 기록(0행)만 남음 → 없는 id 로만 요청, 모두 404."""
    assert not _has(_check().problems, "바꿀 행", "행이 하나도 없는데")
    c = _contract()
    c["seed"] = [{"entity_id": "m2", "rows": 0}]
    p = _plan()
    p["cases"][0]["data"] = []
    res = _check(c, p)
    assert _has(res.problems, "시드에 행이 하나도 없는데", "PATCH /orders/{id}/status")
    assert _has(res.problems, "숨은 케이스 hc1 가 바꿀 행(data)을 선언하지 않음")
    # 경로 변수로 바꾸는 API 가 없으면 (조회만) 묻지 않는다
    c["endpoints"] = [e for e in c["endpoints"] if e["method"] == "GET"]
    c["covers"] = {k: [i for i in v if i != "e1"] for k, v in c["covers"].items()}
    assert not _has(_check(c, p).problems, "바꿀 행", "행이 하나도 없는데")


def test_loose_element_message_says_connect_before_dropping() -> None:
    c = _contract()
    c["covers"] = {k: [i for i in v if i != "m1"] for k, v in c["covers"].items()}
    msg = next(m for m in _check(c).problems if "연결되지 않은 계약 요소: m1" in m)
    assert "covers 에 잇는다" in msg and "관련 없을 때만 뺀다" in msg


def test_expect_naming_harness_params_is_reasked() -> None:
    """2026-11-07 알파 5회차: 통과 조건이 'pool_size(30) 내에서 연결을 점유' 라 숨은 테스트가 단언을 거꾸로 씀.
    2026-11-08 상류 기준선 9회 중 3회 — 표시만으로는 줄지 않아 재요청으로."""
    p = _plan()
    p["cases"][0]["expect"] = "모든 요청이 pool_size(30) 내에서 연결을 점유"
    res = _check(plan=p)
    assert _has(
        res.problems,
        "hc1",
        "통과 조건(expect)이 조절값(pool_size)",
        "HTTP 로 관찰할 수 있는 결과로만",
    )
    assert not [p for p in _check().problems if "통과 조건" in p]


def test_seed_declared_contract_needs_are_checked() -> None:
    """2026-11-07~08 알파 평가: 지난 계약 10개 모두 상태 전이 규칙이 없었고, 대상 엔티티 대신 감사 기록에 시드를 둔 계약도 있었다."""
    assert BACKEND_SEED is not None and BACKEND_SEED.contract_needs
    assert not _has(_check().problems, "숨은 요구에 필요한 것")
    # 전이 규칙 없음
    c = _contract()
    c["endpoints"][0]["responses"][1]["description"] = "허용되지 않는 상태 변경"
    assert _has(_check(c).problems, "상태 전이 규칙", "A→B")
    # 처음 0행인 기록 엔티티 없음 (감사 기록에도 시드)
    c = _contract()
    c["seed"][1] = {
        "entity_id": "m2",
        "rows": 30,
        "groups": [{"first": 1, "last": 30, "values": {}}],
    }
    assert _has(_check(c).problems, "요청마다 쌓이는 기록 엔티티")
    # 재료에 선언이 없으면 묻지 않는다
    c = _contract()
    c["endpoints"][0]["responses"][1]["description"] = "허용되지 않는 상태 변경"
    assert not _has(_check(c, seed=None).problems, "숨은 요구에 필요한 것")


def test_case_data_must_be_on_existing_entities_once_marked() -> None:
    c = _contract()
    c["entities"][0]["existing"] = True  # Order 는 이미 있음, OrderHistory 는 새로 만듦
    assert not _has(_check(c).problems, "새로 만드는 엔티티")
    p = _plan()
    p["cases"][0]["data"] = [{"entity_id": "m2", "rows": 96, "initial": {}, "burst": True}]
    c["seed"][1] = {
        "entity_id": "m2",
        "rows": 120,
        "groups": [{"first": 1, "last": 120, "values": {}}],
    }
    res = _check(c, p)
    assert _has(res.problems, "숨은 케이스 hc1 의 data 가 새로 만드는 엔티티(OrderHistory)")
    assert any("새로 만드는 엔티티 OrderHistory 에 시드 120행" in f for f in res.review_flags)
    # 표시가 하나도 없으면(옛 계약) 판단하지 않는다
    assert not _has(_check(_contract(), p).problems, "새로 만드는 엔티티")


def test_fit_covers_connects_loose_elements_by_their_own_refs() -> None:
    """2026-11-08 상류 평가: 응답 모양 엔티티(필드 refs r1·p1)를 covers 에 잇지도 빼지도 않아 재요청 2회 모두 실패 — 역색인은 코드가."""
    c = _contract()
    c["covers"] = {"r1": ["e1", "m1", "m2"]}  # e2 (refs r2·p2) 가 빠짐
    contract = TaskContract.model_validate(c)
    fitted, notes = fit_covers(contract, _spec())
    assert fitted.covers["r2"] == ["e2"] and _has(notes, "e2", "covers r2")
    assert not _has(
        check_contract(
            fitted, HiddenPlan.model_validate(_plan()), _spec(), _manifest(), BACKEND_SEED
        ).problems,
        "연결되지 않은 계약 요소",
    )
    # 이미 다 이어져 있으면 그대로
    assert fit_covers(TaskContract.model_validate(_contract()), _spec()) == (
        TaskContract.model_validate(_contract()),
        [],
    )
    # refs 가 공개 수용 기준뿐이면 잇지 않는다 — 어느 요구와 관련인지 계약에서 알 수 없다 (#180: 예전엔 covers[p2] 를 만들어 통과)
    only_ac = _spec(req_ids=("r1",))
    fitted, _ = fit_covers(contract, only_ac)
    assert "p2" not in fitted.covers
    assert _has(
        check_contract(
            fitted, HiddenPlan.model_validate(_plan()), only_ac, _manifest(), BACKEND_SEED
        ).problems,
        "연결되지 않은 계약 요소",
        "e2",
    )


def test_acceptance_criterion_keys_are_dropped_from_covers() -> None:
    """#180: covers 키는 공개 요구만 — 수용 기준(p…) 키는 공개 테스트가 대응하므로 코드가 빼고, 그 아래에만 있던 요소는 요소의 refs 로
    다시 잇는다. 예전엔 p… 값까지 '연결됨'으로 세어 요소를 수용 기준에만 이어도 통과했다."""
    c = _contract()
    c["covers"] = {"r1": ["e1", "m1", "m2"], "p2": ["e2"]}  # e2 의 refs 는 r2·p2
    fitted, notes = fit_covers(TaskContract.model_validate(c), _spec())
    assert "p2" not in fitted.covers and fitted.covers["r2"] == ["e2"]
    assert _has(notes, "공개 수용 기준 키 p2 를 뺌") and _has(notes, "e2", "covers r2")
    # 맞추지 않은 계약을 그대로 검사하면 수용 기준 키는 문제
    assert _has(_check(c).problems, "covers 의 키 p2", "공개 수용 기준은 covers 키가 아니다")


def test_case_trap_id_is_fixed_from_the_hidden_acceptance_criterion() -> None:
    """2026-11-09 데이터 알파 재검증: 계약 첫 답이 8회 모두 trap_id 에 실패 사유 코드를 적어 재요청 — ac_id → trap 은 명세가 정한다."""
    from task_spec.starter.contract import fit_traps

    spec = _spec()
    plan = HiddenPlan.model_validate(_plan())
    ac, trap = next(iter(spec.hidden_acs.items()))
    wrong = plan.model_copy(
        update={
            "cases": [
                c.model_copy(update={"ac_id": ac, "trap_id": "some_reason_code"})
                for c in plan.cases
            ]
        }
    )
    fixed, notes = fit_traps(wrong, spec)
    assert {c.trap_id for c in fixed.cases} == {trap} and len(notes) == len(plan.cases)
    assert fit_traps(fixed, spec) == (fixed, [])  # 맞으면 그대로


def test_trap_fix_follows_each_acceptance_criterion_with_several_hidden_requirements() -> None:
    """숨은 요구가 여럿이면 케이스마다 그 ac_id 의 숨은 요구로 — 명세의 다른 숨은 요구를 적은 것은 고치지 않는다(검사가 알린다)."""
    from task_spec.starter.contract import fit_traps

    spec = SpecView(
        req_ids=("r1",),
        public_ac_ids=("p1",),
        hidden_acs={"h1": "t1", "h2": "t2"},
        trap_ids=("t1", "t2"),
        visible_texts=(),
    )
    base = HiddenPlan.model_validate(_plan())
    proto = base.cases[0]
    plan = base.model_copy(
        update={
            "cases": [
                proto.model_copy(
                    update={"case_id": "hc1", "ac_id": "h1", "trap_id": "lost_update"}
                ),
                proto.model_copy(
                    update={"case_id": "hc2", "ac_id": "h2", "trap_id": "pool_exhausted"}
                ),
                proto.model_copy(
                    update={"case_id": "hc3", "ac_id": "h1", "trap_id": "t2"}
                ),  # 명세의 다른 숨은 요구
            ]
        }
    )
    fixed, notes = fit_traps(plan, spec)
    assert [c.trap_id for c in fixed.cases] == ["t1", "t2", "t2"] and len(notes) == 2


def test_timed_evidence_accepts_unit_conversion_and_rounding() -> None:
    # 2026-11-10 nplus1-e: '왕복 p50은 11.4ms' → query_delay_ms=12, '목표는 500ms' → request_timeout_s=1 을 거절했다
    from task_spec.starter.contract import evidence_check, param_unit

    texts = [
        "단일 SELECT 왕복 p50은 11.4ms 이고",
        "목록 API 응답 목표는 500ms 이며 1초를 넘으면 이탈한다",
    ]
    assert param_unit("query_delay_ms") == "ms" and param_unit("request_timeout_s") == "s"
    assert param_unit("pool_size") is None
    assert evidence_check(12, "단일 SELECT 왕복 p50은 11.4ms", texts, "ms") == "ok"
    assert evidence_check(1, "목록 API 응답 목표는 500ms", texts, "s") == "ok"
    assert evidence_check(1, "1초를 넘으면 이탈한다", texts, "s") == "ok"  # 조사가 붙은 단위
    assert evidence_check(3, "목록 API 응답 목표는 500ms", texts, "s") == "bad"  # 환산해도 다른 값
    assert (
        evidence_check(12, "단일 SELECT 왕복 p50은 11.4ms", texts) == "bad"
    )  # 단위 없는 조절값은 예전처럼


def test_timed_hints_list_lines_with_time_values_for_a_missed_quote() -> None:
    # 2026-11-10 nplus1-f: 자료의 '응답 p99 ≤ 800 ms' 를 두고 request_timeout_s 근거를 '없음' — 후보를 짚어 한 번 더 묻는다
    from task_spec.nodes.starter.plan_contract import plan_evidence_missing
    from task_spec.starter.contract import timed_hints

    texts = ["목록 응답 한도\n- p99 응답 시간 ≤ 800 ms\n- 건수 200", "풀 크기 50"]
    assert timed_hints("request_timeout_s", texts) == ["- p99 응답 시간 ≤ 800 ms"]
    assert timed_hints("pool_size", texts) == []  # 단위 없는 조절값은 표기 패턴 몫
    plan = HiddenPlan.model_validate(
        _plan() | {"harness": {"request_timeout_s": 2}, "evidence": {"request_timeout_s": "없음"}}
    )
    assert plan_evidence_missing(plan, "request_timeout_s")
    assert not plan_evidence_missing(plan, "query_delay_ms")  # 계획에 없는 조절값


def test_no_evidence_timed_value_matching_material_is_flagged_not_a_gap() -> None:
    # 2026-11-10 nplus1-i: '응답 p99 ≤ 800 ms' 로 request_timeout_s=1 을 맞게 정하고 근거를 세 번 다 '없음' — 명세 결함으로 끝났다
    from task_spec.starter.contract import _converted, timed_hints

    texts = ["목록 응답 한도\n- p99 응답 시간 ≤ 800 ms"]
    [line] = timed_hints("request_timeout_s", texts)
    assert 1.0 in _converted(line, "s") and 3.0 not in _converted(line, "s")


def test_api_field_names_fixed_by_the_spec_are_accepted() -> None:
    # 2026-11-10 확장 알파 C #062: 공개 요구가 API 필드를 member_id·recharge_id 로 정해 camelCase 규칙과 충돌 — 명세가 우선 (사용자 결정 A)
    from dataclasses import replace

    from task_spec.starter.contract import spec_named_fields

    spec = _spec()
    named = replace(
        spec, public_texts=("요청 본문에 member_id와 amount를 받고, 응답에 recharge_id와 balance",)
    )
    assert spec_named_fields(named) == {"member_id", "recharge_id"}  # 조사가 붙어도
    c = _contract()
    c["endpoints"][0]["request"] = [{"name": "member_id", "type": "integer", "refs": ["r1"]}]
    c["entities"][0]["fields"].append({"name": "order_no", "type": "string", "refs": ["r1"]})
    m = _manifest()
    assert _has(_check(c, spec=spec, manifest=m).problems, "member_id")  # 명세에 없으면 예전처럼
    probs = _check(
        c, spec=replace(named, public_texts=(*named.public_texts, "order_no 도")), manifest=m
    ).problems
    assert not _has(probs, "member_id")  # API 필드는 명세 이름 인정
    assert _has(probs, "order_no")  # 엔티티 필드는 규칙대로
    # #180: 거울 사례 — 명세가 camel 이름(rechargeId)을 정하고 템플릿 규칙이 snake 여도 명세가 우선
    camel = replace(spec, public_texts=("응답에 rechargeId와 balance 를 돌려준다",))
    assert spec_named_fields(camel) == {"rechargeId"}
    c["endpoints"][0]["request"] = [{"name": "rechargeId", "type": "integer", "refs": ["r1"]}]
    snake = m.model_copy(
        update={"naming": m.naming.model_copy(update={"field": r"^[a-z][a-z0-9_]*$"})}
    )
    assert _has(_check(c, spec=spec, manifest=snake).problems, "rechargeId")
    assert not _has(_check(c, spec=camel, manifest=snake).problems, "rechargeId")


def test_rounded_quote_value_passes_with_flag() -> None:
    """2026-11-11 백엔드 N+1: 'p50: 8.1' 을 인용하고 8 을 썼다 — 정수로 맞춘 값은 통과, 승인자 확인 [확정: 2026-11-11 사용자]."""
    texts = ["metric: db_query_roundtrip_ms\np50: 8.1\np95: 14.7"]
    assert evidence_check(8, "p50: 8.1", texts) == "rounded"
    assert evidence_check(9, "p50: 8.1", texts) == "rounded"  # 올림
    assert evidence_check(10, "p50: 8.1", texts) == "bad"  # 1 이상 차이
    assert evidence_check(8, "p50: 8.1\np95: 14.7", texts) == "bad"  # 후보가 여럿
    spec = _spec(visible_texts=("pool: 8\nthreads: 32", "p50: 8.1"))
    m = _manifest().model_dump()
    m["harness_params"][2] |= {"visible": True, "spec_patterns": [r"쿼리\s*지연\s*(\d+)\s*ms"]}
    plan = _plan(harness={"pool_size": 8, "concurrency": 32, "query_delay_ms": 8})
    plan["evidence"] = {"query_delay_ms": "p50: 8.1"}
    res = _check(plan=plan, spec=spec, manifest=TemplateManifest.model_validate(m))
    assert not _has(res.problems, "query_delay_ms"), res.problems
    assert _has(res.review_flags, "query_delay_ms=8", "정수로 맞춘")


def test_unevidenced_optional_param_is_dropped_by_code() -> None:
    """2026-11-11 백엔드 중복 처리: 근거를 '없음'으로 쓴 선택 조절값을 재요청에도 못 뺐다 — 코드가 뺀다. 필수·자료에 있는 값은 두고."""
    m = _manifest().model_dump()
    m["harness_params"][2] |= {"visible": True, "spec_patterns": [r"쿼리\s*지연\s*(\d+)\s*ms"]}
    manifest = TemplateManifest.model_validate(m)
    spec = _spec(visible_texts=("pool: 8\nthreads: 32", "자료에 지연 수치 없음"))
    plan = HiddenPlan.model_validate(
        _plan(harness={"pool_size": 8, "concurrency": 32, "query_delay_ms": 20})
        | {"evidence": {"query_delay_ms": "없음", "concurrency": "없음"}}
    )
    fixed, notes = fit_unevidenced(plan, manifest, spec)
    assert "query_delay_ms" not in fixed.harness and "query_delay_ms" not in fixed.evidence
    assert fixed.harness["concurrency"] == 32  # 자료(threads: 32)에 있으니 둔다
    assert notes and "query_delay_ms" in notes[0]
    assert fit_unevidenced(HiddenPlan.model_validate(_plan()), manifest, spec) == (
        HiddenPlan.model_validate(_plan()),
        [],
    )


def test_partial_quote_uses_longest_verbatim_fragment_with_flag() -> None:
    """2026-11-12 갱신 손실: 'YYYY-MM-DD HH:MM' 을 'YYYY-MM-DDTHH:MM' 로 인용해 막혔다 — 자료에 그대로 있는 가장 긴 조각으로 바꾸고
    그 조각을 같은 판정으로 보되, 결과와 상관없이 승인자 표시 [확정: 2026-11-12 사용자]."""
    from task_spec.nodes.starter.plan_contract import fit_evidence_fragments
    from task_spec.starter.contract import EVIDENCE_FRAGMENT, evidence_fragment

    src = "2024-11-15 10:00:01 시각대 로그에서 차감 요청이 1초 안에 30건 이상 기록되어 있습니다. 다음 줄"
    texts = ["pool: 8\nthreads: 32", src]
    llm = "2024-11-15T10:00:01 시각대 로그에서 차감 요청이 1초 안에 30건 이상 기록되어 있습니다"
    assert evidence_check(30, llm, texts) == "bad"
    frag = evidence_fragment(30, llm, texts)
    # 가장 짧은 '30건 이상 기록되어'(ok)가 아니라 가장 긴 조각 — 후보 숫자 여럿이면 ambiguous 로 남는다
    assert (
        frag is not None
        and frag.startswith("10:00:01")
        and evidence_check(30, frag, texts) == "ambiguous"
    )
    assert evidence_fragment(30, "피크 동시 요청은 30건입니다", texts) is None  # 지어낸 인용
    assert evidence_fragment(45, llm, texts) is None  # 조각에 그 값이 없음
    assert evidence_fragment(30, src[:40], texts) is None  # 그대로 있으면 대상이 아니다

    spec = _spec(visible_texts=tuple(texts))
    plan = HiddenPlan.model_validate(
        _plan(harness={"pool_size": 8, "concurrency": 30, "query_delay_ms": 20})
        | {"evidence": {"concurrency": llm}}
    )
    fixed, notes = fit_evidence_fragments(plan, _manifest(), spec)
    assert fixed.evidence["concurrency"] == frag
    assert (
        len(notes) == 1 and notes[0].startswith(EVIDENCE_FRAGMENT) and "concurrency=30" in notes[0]
    )
    assert fit_evidence_fragments(fixed, _manifest(), spec) == (
        fixed,
        [],
    )  # 다시 돌려도 같다 (결정적)


# ── 재료 규칙으로 근거 대조 (FeatureSeed.harness_rules) ─────────────────

_P50 = HarnessRule(labels=["p50", "중앙값", "평균"], round="nearest")
_LIMIT = HarnessRule(pick="max", round="up")
_BURST = HarnessRule(units=["건"], pick="max")


@pytest.mark.parametrize(
    ("rule", "unit", "value", "quote", "verdict", "expected"),
    [
        # 2026-11-13 목록 재료 hana006: 숫자가 둘이라 일반 검사가 세 번 거절 — 규칙은 p50 을 골라 반올림
        (_P50, "ms", 20, "단일 SELECT 왕복 시간 (ms) 평균: 21.3 p50: 19.8", "ok", 20),
        (_P50, "ms", 21, "단일 SELECT 왕복 시간 (ms) 평균: 21.3 p50: 19.8", "mismatch", 20),
        (
            _P50,
            "ms",
            21,
            "왕복 시간이 평균 21ms, p95 28ms입니다",
            "ok",
            21,
        ),  # p50 이 없으면 다음 이름표
        (_P50, "ms", 28, "p95 28ms 수준입니다", "missing", None),  # 규칙에 없는 백분위만
        (_P50, "ms", 11, "쿼리 한 번에 11ms 가 걸립니다", "ok", 11),  # 이름표 없는 수
        (_P50, "ms", 12, "p50 은 11.4ms 입니다", "mismatch", 11),  # 반올림이지 올림이 아니다
        (_P50, "ms", 3, "p50: 2.5 p95: 4.0", "ok", 3),  # .5 는 올림 (은행가 반올림 아님)
        (_LIMIT, "s", 2, "목록 화면 초기 로딩: 2초 이내", "ok", 2),
        (_LIMIT, "s", 1, "응답 시간 목표 p95 ≤ 500 ms · p99 ≤ 800 ms (200건 기준)", "ok", 1),
        (
            _LIMIT,
            "s",
            2,
            "SLA 기준(2000ms) 초과 비율: 전체 요청의 34%",
            "ok",
            2,
        ),  # 백분율은 값이 아니다
        (_BURST, None, 17, "개시 직후 3초 동안 EV-2024-011에 대한 신청이 17건 이상 기록", "ok", 17),
        (
            _BURST,
            None,
            3,
            "개시 직후 3초 동안 EV-2024-011에 대한 신청이 17건 이상 기록",
            "mismatch",
            17,
        ),
        (_BURST, None, 47, "오픈 시각 14:00:00부터 14:00:02 구간에서 47건의 차감 요청", "ok", 47),
        (_BURST, None, 42, "초당 최대 42건이 같은 대상 id에 몰립니다.", "ok", 42),
        (
            HarnessRule(units=["건"]),
            None,
            5,
            "평소 3건, 피크 5건",
            "ambiguous",
            None,
        ),  # only 인데 여럿
    ],
)
def test_rule_check_derives_value_from_quote(
    rule: HarnessRule,
    unit: str | None,
    value: int,
    quote: str,
    verdict: str,
    expected: int | None,
) -> None:
    got = rule_check(value, quote, [quote], unit, rule)
    assert got.verdict == verdict, got
    assert got.expected == (None if expected is None else float(expected))


def test_rule_check_bare_number_takes_header_unit() -> None:
    """단위 없는 수는 머리 줄의 단위 표기('(초)')로, 없으면 조절값 단위로 본다."""
    rule = HarnessRule(labels=["p99"], round="up")
    assert (
        rule_check(
            1, "응답 시간 (초) p95: 0.6 p99: 0.9", ["응답 시간 (초) p95: 0.6 p99: 0.9"], "s", rule
        ).verdict
        == "ok"
    )
    assert (
        rule_check(1, "응답 시간 (ms) p99: 900", ["응답 시간 (ms) p99: 900"], "s", rule).verdict
        == "ok"
    )
    assert (
        rule_check(900, "p99: 900 정도", ["p99: 900 정도"], "s", rule).verdict == "ok"
    )  # 단위 없음 → 조절값 단위


def test_rule_check_quote_must_be_in_material() -> None:
    texts = ["p50: 19.8\np95: 28.4"]
    assert rule_check(20, "p50: 19.8 p95: 28.4", texts, "ms", _P50).verdict == "ok"  # 공백 무시
    assert rule_check(20, "p50 은 19.8 입니다", texts, "ms", _P50).verdict == "bad_quote"


def test_material_rule_replaces_generic_evidence_check() -> None:
    """재료 규칙이 있으면 계약 검사는 그 규칙으로 대조 — 숫자가 여럿인 인용도 규칙이 고른 값이면 통과, 아니면 규칙을 적어 재요청."""
    block = "단일 SELECT 왕복 시간 (ms)\n  평균: 21.3\n  p50: 19.8\n  p95: 28.4"
    quote = "단일 SELECT 왕복 시간 (ms) 평균: 21.3 p50: 19.8"
    spec = _spec(visible_texts=("pool: 8\nthreads: 32", block))
    m = _manifest().model_dump()
    m["harness_params"][2] |= {"visible": True, "spec_patterns": [r"쿼리\s*지연\s*(\d+)\s*ms"]}
    manifest = TemplateManifest.model_validate(m)
    assert (
        evidence_check(20, quote, spec.visible_texts, "ms") == "bad"
    )  # 일반 검사로는 거절 (hana006)
    seed = BACKEND_SEED.model_copy(
        update={
            "harness": ["pool_size", "concurrency", "query_delay_ms"],
            "harness_rules": {"query_delay_ms": _P50},
        }
    )
    plan = _plan(harness={"pool_size": 8, "concurrency": 32, "query_delay_ms": 20})
    plan["evidence"] = {"query_delay_ms": quote}
    res = _check(plan=plan, spec=spec, manifest=manifest, seed=seed)
    assert not _has(res.problems, "query_delay_ms"), res.problems
    assert not _has(res.review_flags, "query_delay_ms"), (
        res.review_flags
    )  # 규칙으로 확인 — 승인자 표시 없음
    plan["harness"]["query_delay_ms"] = 21
    res = _check(plan=plan, spec=spec, manifest=manifest, seed=seed)
    assert _has(res.problems, "query_delay_ms=21", "재료 규칙", "p50", "20", "19.8"), res.problems
    # 규칙이 없으면 예전 검사 그대로 (숫자가 여럿이면 거절)
    res = _check(
        plan=plan | {"harness": {**plan["harness"], "query_delay_ms": 20}},
        spec=spec,
        manifest=manifest,
    )
    assert _has(res.problems, "query_delay_ms=20")


def test_fragment_fitting_uses_material_rule() -> None:
    """근거 인용 글자를 바꾼 경우 자료 원문 조각을 고를 때도 규칙으로 — 규칙이 받는 조각만."""
    texts = ["단일 SELECT 왕복 시간 (ms)\n  평균: 21.3\n  p50: 19.8\n  p95: 28.4"]
    llm = "단일 SELECT 왕복시간 (ms) 평균: 21.3 p50: 19.8"
    assert evidence_fragment(20, llm, texts, "ms") is None  # 일반 검사로는 받을 조각이 없다
    frag = evidence_fragment(20, llm, texts, "ms", _P50)
    assert frag is not None and "p50: 19.8" in frag
    assert rule_check(20, frag, texts, "ms", _P50).verdict == "ok"


def test_harness_rules_must_name_material_params() -> None:
    with pytest.raises(ValueError, match="harness 에 없음"):
        BACKEND_SEED.model_validate(
            BACKEND_SEED.model_dump()
            | {"harness": ["concurrency"], "harness_rules": {"pool_size": {}}}
        )


def test_describe_rule_reads_like_the_material_note() -> None:
    assert (
        describe_rule(_P50, "ms") == "이름표 p50(없으면 중앙값·평균) 의 값, ms 단위로 바꿔 반올림"
    )
    assert describe_rule(_LIMIT, "s") == "여럿이면 가장 큰 값, 초 단위로 바꿔 올림"
    assert describe_rule(_BURST, None) == "건 단위가 붙은 수, 여럿이면 가장 큰 값"


# ── #179 계약 검사·보정의 중복 정리 ─────────────────────────────


def test_unknown_param_survives_fitting_and_the_check_asks_again() -> None:
    """보정(fit_material_params)이 재료로 좁힌 목록 밖 키를 모두 지워 '템플릿에 없는 조절값' 검사가 운영에서 걸리지 않았다 —
    재료가 고르지 않은 템플릿 조절값만 빼고, 템플릿에 없는 키는 검사가 다시 묻는다."""
    from task_spec.nodes.starter.plan_contract import fit_output
    from task_spec.schemas import PlanContractOutput

    m = _manifest()
    known = {p.name for p in m.harness_params}
    plan = _plan(harness={"pool_size": 8, "concurrency": 32, "pool": 3})
    raw = PlanContractOutput.model_validate({"contract": _contract(), "hidden_plan": plan})
    fitted, _ = fit_output(raw, m, _spec(), None, known)
    assert fitted.hidden_plan.harness.get("pool") == 3
    res = check_contract(fitted.contract, fitted.hidden_plan, _spec(), m, BACKEND_SEED)
    assert _has(res.problems, "템플릿에 없는 조절값", "pool")


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("기존 `PATCH /orders/{id}/status` 는 그대로", [("PATCH", "/orders/{id}/status")]),
        ("제출하면 POST /x. 로 보낸다", [("POST", "/x")]),  # 끝 문장 부호는 경로가 아니다
        ("GET /orders/:id 로 읽는다", [("GET", "/orders/:id")]),
        ("GET / 로 확인", [("GET", "/")]),
    ],
)
def test_one_api_mention_parser(text: str, want: list[tuple[str, str]]) -> None:
    """#179: 'METHOD /path' 정규식이 둘이라 같은 글을 다르게 잘랐다 (`POST /x.` → '/x.' 와 '/x')."""
    from task_spec.starter.contract import _api_key, api_mentions

    assert api_mentions(text) == want
    assert (
        _api_key("get", "/orders/:id") == _api_key("GET", "/orders/{orderId}/") == "GET /orders/{}"
    )


def test_timed_hints_show_the_lines_the_check_accepts() -> None:
    """#179: 근거 대조는 앞 20줄, 재질문 힌트는 앞 4줄을 봐 — 대조가 받는 줄을 힌트가 보여 주지 않을 수 있었다."""
    from task_spec.starter.contract import TIMED_CANDIDATES, timed_hints, timed_matches

    texts = ["\n".join(f"- 지표 {i}: {i * 100} ms" for i in range(1, 8))]
    hits = timed_matches("request_timeout_s", 1, texts)  # 700 ms → 1 초 (올림)
    assert hits and all(h in timed_hints("request_timeout_s", texts) for h in hits)
    assert len(timed_hints("request_timeout_s", texts)) == 7 <= TIMED_CANDIDATES
