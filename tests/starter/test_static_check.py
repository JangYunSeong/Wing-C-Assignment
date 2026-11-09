"""오버레이 정적 검사 static_check (starter/static_check.py, spec-to-code §6) — 가상 데이터만, 네거티브 위주."""

from __future__ import annotations

from typing import Any

from task_spec.schemas import GeneratedFile, HiddenPlan
from task_spec.starter.contract import SpecView
from task_spec.starter.render import render_params, render_readme
from task_spec.starter.static_check import Overlay, identifier_words, static_check
from task_spec.starter.templates import load_template
from tests.starter.test_contract import BACKEND_SEED, _plan
from tests.starter.test_plan_contract import _task
from tests.starter.test_render import _approved, _contract_with_list

JAVA = load_template("backend/java-spring-boot3-jpa")
PKG = "src/main/java/com/example/service"
CASES = "src/test/java/com/example/service/grading/cases"
PLAN = HiddenPlan.model_validate(
    _plan(harness={"pool_size": 8, "concurrency": 32, "connection_timeout_ms": 5000})
)

ORDER = """package com.example.service.order;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;

@Entity
public class Order {
    @Id
    private Long id;
    private String status;
}
"""
HC1 = """package com.example.service.grading.cases;

import com.example.service.grading.Burst;
import com.example.service.grading.GraderParams;
import com.example.service.grading.Reasons;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

class OrderStatusBurstTest extends ContractCase {
    @Test
    @DisplayName("hc1 피크와 같은 수의 동시 변경")
    void hc1() throws Exception {
        int n = GraderParams.intValue("concurrency", 32);
        var outcomes = Burst.fire(n, i -> 200, i -> 200, REQUEST_TIMEOUT);
        org.assertj.core.api.Assertions.assertThat(Burst.failures(outcomes))
                .as(Reasons.as("pool_acquire_timeout", "버스트 %d", 1)).isZero();
    }
}
"""


def _f(path: str, content: str) -> GeneratedFile:
    return GeneratedFile(path=path, content=content)


def _overlays(**over: Any) -> list[Overlay]:
    base = {
        "starter": [
            _f(f"{PKG}/order/Order.java", ORDER),
            _f(
                "src/main/resources/schema.sql",
                "CREATE TABLE orders (id BIGINT PRIMARY KEY, status VARCHAR(20));\n",
            ),
            _f(
                "src/main/resources/application.yml", "spring:\n  application:\n    name: service\n"
            ),
        ],
        "hidden": [
            _f(f"{CASES}/OrderStatusBurstTest.java", HC1),
            render_params(PLAN, JAVA.manifest),
        ],
        "ref_naive": [
            _f(
                f"{PKG}/order/OrderService.java",
                "package com.example.service.order;\nclass OrderService {}\n",
            )
        ],
    }
    base |= over
    return [
        Overlay(kind=k, files=tuple(v), label="naive-all" if k == "ref_naive" else "")
        for k, v in base.items()
    ]  # type: ignore[arg-type]


def _run(overlays: list[Overlay] | None = None, **kw: Any):  # type: ignore[no-untyped-def]
    task = kw.pop("task", _task())
    return static_check(
        overlays if overlays is not None else _overlays(),
        template=JAVA,
        plan=kw.pop("plan", PLAN),
        spec=SpecView.from_task(task),
        seed=kw.pop("seed", BACKEND_SEED),
        readme=kw.pop("readme", None),
        avoid_terms=kw.pop("avoid_terms", ("우아한형제들", "hibernate_sequence")),
    )


def _has(msgs: list[str], *needles: str) -> bool:
    return any(all(n in m for n in needles) for m in msgs)


def test_clean_overlays_pass() -> None:
    readme = render_readme(_approved(), _contract_with_list(), JAVA)
    res = _run(readme=readme)
    assert res.ok and res.review_flags == [], ([p.message for p in res.problems], res.review_flags)


def test_paths_and_locked_harness_are_assigned_to_their_generator() -> None:
    res = _run(
        _overlays(
            starter=[_f("build.gradle", "plugins {}"), _f(f"{CASES}/Sneaky.java", "class S {}")],
            hidden=[
                _f("src/test/java/com/example/service/grading/GraderSupport.java", "x"),
                render_params(PLAN, JAVA.manifest),
            ],
            ref_naive=[_f("src/test/java/com/example/service/order/OrderTest.java", "x")],
        )
    )
    assert _has(res.for_target("gen_starter"), "build.gradle", "쓸 수 없는 경로")
    assert _has(res.for_target("gen_starter"), "Sneaky.java")
    assert _has(res.for_target("gen_hidden"), "GraderSupport.java", "잠긴 채점 하네스")
    assert _has(res.for_target("gen_naive"), "ref_naive(naive-all)", "OrderTest.java")


def test_limits() -> None:
    many = [_f(f"{PKG}/gen/C{i}.java", "class C {}") for i in range(61)]
    big = [_f(f"{PKG}/Big.java", "x" * 100_001)]
    assert _has(_run(_overlays(starter=many)).for_target("gen_starter"), "파일 수 61", "상한 60")
    assert _has(_run(_overlays(starter=big)).for_target("gen_starter"), "크기 상한")


def test_hidden_terms_in_public_code_names_and_comments() -> None:
    leak = ORDER.replace(
        "private String status;",
        "private int connectionPoolSize; // 피크 대비\n    private String status;",
    )
    res = _run(_overlays(starter=[_f(f"{PKG}/order/Order.java", leak)]))
    assert _has(res.for_target("gen_starter"), "Order.java", "connection pool", "피크")
    named = _run(_overlays(starter=[_f(f"{PKG}/order/OrderStatusBurstTest.java", "class X {}")]))
    assert _has(named.for_target("gen_starter"), "OrderStatusBurstTest")  # 숨은 테스트 이름
    # 숨은 테스트 파일 자체는 숨은 요구를 써도 된다 (공개 파일이 아님)
    assert not _run().for_target("gen_hidden")


def test_operating_value_baked_into_starter_config() -> None:
    yml = "spring:\n  datasource:\n    hikari:\n      maximum-pool-size: 8\n"
    res = _run(_overlays(starter=[_f("src/main/resources/application.yml", yml)]))
    assert _has(res.for_target("gen_starter"), "maximum-pool-size=8", "운영 값(pool_size)")
    dev = yml.replace(": 8", ": 10")  # 개발용 다른 값은 괜찮다
    assert not _run(_overlays(starter=[_f("src/main/resources/application.yml", dev)])).for_target(
        "gen_starter"
    )


def test_accepted_impl_relying_on_harness_overridden_config() -> None:
    """알파 테스트 2026-11-07: '풀 크기 산정' 설계를 하네스가 pool_size 로 덮어써 어떤 구현도 통과할 수 없었다."""
    yml = "spring:\n  datasource:\n    hikari:\n      maximum-pool-size: {}\n"
    starter = _f("src/main/resources/application.yml", yml.format(10))
    base = _overlays(starter=[starter])

    def ref(n: int) -> Overlay:
        return Overlay(
            kind="ref_accepted",
            files=(_f("src/main/resources/application.yml", yml.format(n)),),
            label="d1",
        )

    res = _run([*base, ref(200)])
    assert _has(
        res.for_target("gen_accepted"),
        "ref_accepted(d1)",
        "maximum-pool-size=200",
        "하네스가 덮어쓰는",
    )
    assert not _run([*base, ref(10)]).for_target("gen_accepted")  # 시작 코드 값 그대로는 괜찮다


def test_secrets_urls_and_real_names() -> None:
    code = (
        'class P { String url = "https://api.payments.example.io/v1"; String local = "http://localhost:8080";\n'
        '  String password = "Abcdef123456789"; /* 우아한형제들 */ }'
    )
    res = _run(_overlays(ref_naive=[_f(f"{PKG}/order/OrderService.java", code)]))
    msgs = res.for_target("gen_naive")
    assert _has(msgs, "비밀 값")
    assert _has(msgs, "외부 네트워크 주소", "api.payments.example.io") and not _has(
        msgs, "localhost"
    )
    assert _has(msgs, "실존 이름", "우아한형제들")


def test_hidden_case_rules() -> None:
    bad = (
        HC1.replace(
            "import com.example.service.grading.Burst;",
            "import com.example.service.order.OrderService;\nimport com.example.service.grading.Burst;",
        )
        .replace('"pool_acquire_timeout"', '"timeout"')
        .replace("hc1", "hc9")
    )
    params = GeneratedFile(
        path="src/test/resources/grader-params.properties", content="pool_size=16\n"
    )
    res = _run(_overlays(hidden=[_f(f"{CASES}/OrderStatusBurstTest.java", bad), params]))
    msgs = res.for_target("gen_hidden")
    assert _has(msgs, "지원자 구현 내부", "OrderService")
    assert _has(msgs, "실패 사유 코드", "timeout")
    assert _has(msgs, "HiddenPlan 케이스가 숨은 테스트에 없음", "hc1")
    assert _has(msgs, "HiddenPlan 에 없는 케이스 id", "hc9")
    assert _has(msgs, "조절값 파일", "손으로 씀")


def test_hidden_case_calls_only_contract_apis() -> None:
    """2026-11-07 생성 스모크: 숨은 테스트가 계약에 없는 GET /deliveries/{id} 로 상태를 읽어 늘 404."""
    calls = HC1.replace(
        "i -> 200, REQUEST_TIMEOUT",
        'i -> send("PATCH", "/orders/" + (400 + i) + "/status", Map.of("X-Actor", "g"), body).statusCode(), REQUEST_TIMEOUT',
    ).replace(
        "        org.assertj",
        '        var h = get("/orders/" + ids.get(0) + "/history?page=" + (p + 1));\n'
        '        var s = get("/orders/" + id);\n'
        '        var d = send("DELETE", "/orders/" + id, Map.of(), null);\n'
        '        var actor = row.get("/orders/actor");\n'  # 앞에 . 이 붙은 get 은 HTTP 호출이 아님
        '        var x = get(base + "/x");\n'  # 첫 조각이 문자열이 아니면 판단 안 함
        "        org.assertj",
    )
    res = static_check(
        _overlays(hidden=[_f(f"{CASES}/OrderStatusBurstTest.java", calls)]),
        template=JAVA,
        plan=PLAN,
        spec=SpecView.from_task(_task()),
        contract=_contract_with_list(),
        seed=BACKEND_SEED,
    )
    msgs = [m for m in res.for_target("gen_hidden") if "공개 계약에 없는 API" in m]
    assert len(msgs) == 1 and "DELETE /orders/{}, GET /orders/{}" in msgs[0], msgs
    assert "/status" not in msgs[0].split(" — ")[0] and "history" not in msgs[0].split(" — ")[0]
    # 계약을 주지 않으면 이 검사는 하지 않는다
    assert not _has(
        _run(_overlays(hidden=[_f(f"{CASES}/OrderStatusBurstTest.java", calls)])).for_target(
            "gen_hidden"
        ),
        "공개 계약에 없는 API",
    )


def test_case_id_in_two_files() -> None:
    hidden = [
        _f(f"{CASES}/OrderStatusBurstTest.java", HC1),
        _f(
            f"{CASES}/OrderStatusBurst2Test.java",
            HC1.replace("OrderStatusBurstTest", "OrderStatusBurst2Test"),
        ),
    ]
    assert _has(_run(_overlays(hidden=hidden)).for_target("gen_hidden"), "여러 파일", "hc1")


def test_readme_leak_is_a_review_flag_not_a_regeneration() -> None:
    task = _approved()
    leaky = task.model_copy(
        update={
            "scenario": task.scenario.replace(
                "가상의 물류 회사가", "피크 때 몰리는 가상의 물류 회사가"
            )
        }
    )
    res = _run(readme=render_readme(leaky, _contract_with_list(), JAVA), task=leaky)
    assert res.ok  # 생성기 재생성 사유가 아니다
    assert _has(res.review_flags, "README.md", "피크", "명세 수정")


def test_identifier_words() -> None:
    assert (
        identifier_words("int connectionPoolSize; long max_pool; String id;")
        == "connection pool size\nmax pool"
    )


def test_duplicate_reason_labels_in_one_case_file() -> None:
    """2026-11-07 루프: 'FAILURE 기록 있음' 단언에 'SUCCESS 없음' 단언과 같은 사유·문구가 붙어 흔한 구현의 실패 사유를 잘못 읽었다."""
    twice = HC1.replace(
        '                .as(Reasons.as("pool_acquire_timeout", "버스트 %d", 1)).isZero();',
        '                .as(Reasons.as("pool_acquire_timeout", "버스트 %d", 1)).isZero();\n'
        "        org.assertj.core.api.Assertions.assertThat(Burst.failures(outcomes))\n"
        '                .as(Reasons.as("pool_acquire_timeout", "버스트 %d", 2)).isZero();',
    )
    assert twice != HC1
    res = _run(_overlays(hidden=[_f(f"{CASES}/OrderStatusBurstTest.java", twice)]))
    assert _has(
        res.review_flags, "같은 사유·문구의 단언이 둘 이상", 'pool_acquire_timeout "버스트 %d"'
    )
    # 막지 않는다 — 표시만 (2026-11-07: 재요청 문제로 두었더니 숨은 테스트 생성 자체가 실패)
    assert not _has(res.for_target("gen_hidden"), "같은 사유·문구")
    assert not _has(_run().review_flags, "같은 사유·문구")


def test_numeric_fields_are_not_asserted_as_raw_json_values() -> None:
    """2026-11-07 알파 테스트: assertThat(log.get("transactionId")).isEqualTo(txId) — Integer 105 ≠ 105L 로 모든 구현 실패.
    HARNESS.md 규칙이 있었는데도 어겨서 정적 검사로."""
    raw = HC1.replace(
        "        org.assertj",
        '        var row = jsonObject(get("/orders/1/history").body());\n'
        '        org.assertj.core.api.Assertions.assertThat(row.get("orderId")).isEqualTo(1L);\n'
        '        org.assertj.core.api.Assertions.assertThat(((Number) row.get("orderId")).longValue()).isEqualTo(1L);\n'
        '        org.assertj.core.api.Assertions.assertThat(((Number)row.get("orderId")).longValue()).isEqualTo(1L);\n'
        '        org.assertj.core.api.Assertions.assertThat(row.get("orderId")).isNotNull();\n'
        '        org.assertj.core.api.Assertions.assertThat(row.get("orderId")).isEqualTo(((Number) id).intValue());\n'
        '        org.assertj.core.api.Assertions.assertThat(row.get("requestedBy")).isEqualTo("g");\n'
        "        org.assertj",
    )

    def hidden(text: str) -> list[str]:
        return static_check(
            _overlays(hidden=[_f(f"{CASES}/OrderStatusBurstTest.java", text)]),
            template=JAVA,
            plan=PLAN,
            spec=SpecView.from_task(_task()),
            contract=_contract_with_list(),
            seed=BACKEND_SEED,
        ).for_target("gen_hidden")

    msgs = [m for m in hidden(raw) if "JSON Map 값 그대로" in m]
    assert len(msgs) == 1 and 'assertThat(row.get("orderId"))' in msgs[0]
    assert '((Number) x.get("orderId")).longValue()' in msgs[0]
    # 캐스트한 꼴·null 확인·Integer 로 맞춘 비교·문자열 필드만 있으면 통과
    ok = raw.replace(
        '        org.assertj.core.api.Assertions.assertThat(row.get("orderId")).isEqualTo(1L);\n',
        "",
    )
    assert not [m for m in hidden(ok) if "JSON Map 값 그대로" in m]


def test_starter_existing_tests_must_not_call_new_apis() -> None:
    """2026-11-07 알파 4회차: 배송 감사 시작 코드가 감사 기록 조회 API 와 그 '기존 기능 테스트'까지 써서 시작 코드만으로 hc1 통과."""
    contract = _contract_with_list()
    flagged = contract.model_copy(
        update={
            "endpoints": [
                e.model_copy(update={"existing": e.method == "PATCH"}) for e in contract.endpoints
            ]
        }
    )
    test = (
        "src/test/java/com/example/service/order/OrderControllerTest.java",
        'class T { void a() throws Exception { mockMvc.perform(patch("/orders/1/status")); }\n'
        '  void b() throws Exception { mockMvc.perform(get("/orders/" + 2 + "/history")); } }\n',
    )

    def starter_msgs(c: Any, *files: tuple[str, str]) -> list[str]:
        starter = _overlays()[0]
        o = Overlay(kind="starter", files=(*starter.files, *(_f(p, x) for p, x in files)))
        res = static_check(
            [o],
            template=JAVA,
            plan=PLAN,
            spec=SpecView.from_task(_task()),
            contract=c,
            seed=BACKEND_SEED,
        )
        return [m for m in res.for_target("gen_starter") if "새로 만드는 API" in m]

    msgs = starter_msgs(flagged, test)
    assert (
        len(msgs) == 1
        and "GET /orders/{}/history" in msgs[0]
        and "PATCH" not in msgs[0].split(" — ")[0]
    )
    assert "이미 있는 API(PATCH /orders/{id}/status)" in msgs[0]
    # 기존 API 만 부르면 통과, 무엇이 기존인지 모르면(표시·명세 경로 없음) 판단하지 않음
    only_existing = (test[0], test[1].split("\n  void b")[0] + " }\n")
    assert not starter_msgs(flagged, only_existing)
    assert not starter_msgs(contract, test)  # 이 명세 글에는 경로가 없고 계약 표시도 없음


def test_imports_missing_from_the_template_classpath() -> None:
    """2026-11-08 재평가: 시작 코드가 jakarta.validation 을 써 컴파일 실패 (그 뒤 템플릿에 검증 스타터를 넣음 — 지금은 Lombok)."""
    ctl = _f(
        f"{PKG}/order/OrderController.java",
        "package com.example.service.order;\nimport lombok.Getter;\nclass OrderController {}\n",
    )
    res = _run(_overlays(starter=[*_overlays()[0].files, ctl]))
    assert _has(res.for_target("gen_starter"), "템플릿에 없는 라이브러리", "lombok")
    valid = _f(
        f"{PKG}/order/OrderController.java",
        "package com.example.service.order;\nimport jakarta.validation.Valid;\nclass OrderController {}\n",
    )
    assert not _has(
        _run(_overlays(starter=[*_overlays()[0].files, valid])).for_target("gen_starter"),
        "템플릿에 없는 라이브러리",
    )
    naive = _run(_overlays(ref_naive=[ctl]))
    assert _has(naive.for_target("gen_naive"), "템플릿에 없는 라이브러리")


def test_injection_regex_built_from_helper_table_constants() -> None:
    """2026-11-08: ContractCase 의 TABLE_ 상수를 이어 붙인 주입 정규식을 첫 조각만 보고 '넓은 주입'으로 오탐해 숨은 테스트 생성 실패."""
    from task_spec.starter.static_check import inline_table_constants

    consts = {"TABLE_ORDER": "orders"}
    assert (
        inline_table_constants(
            'Pattern.compile("^\\\\s*update\\\\s+" + TABLE_ORDER + "\\\\b")', consts
        )
        == 'Pattern.compile("^\\\\s*update\\\\s+orders\\\\b")'
    )
    assert inline_table_constants(
        'Pattern.compile("^\\\\s*update\\\\s+" + TABLE_ORDER)', consts
    ).endswith('orders")')

    def hidden(line: str) -> list[str]:
        text = HC1.replace(
            "        org.assertj",
            f"        GraderJdbcHarness.failCommitAfter = {line};\n        org.assertj",
        )
        return static_check(
            _overlays(hidden=[_f(f"{CASES}/OrderStatusBurstTest.java", text)]),
            template=JAVA,
            plan=PLAN,
            spec=SpecView.from_task(_task()),
            contract=_contract_with_list(),
            seed=BACKEND_SEED,
        ).for_target("gen_hidden")

    joined = (
        'Pattern.compile("^\\\\s*update\\\\s+" + TABLE_ORDER + "\\\\b", Pattern.CASE_INSENSITIVE)'
    )
    assert not [m for m in hidden(joined) if "주입 정규식" in m]
    bare = 'Pattern.compile("^\\\\s*update\\\\s+", Pattern.CASE_INSENSITIVE)'
    assert any("넓음" in m for m in hidden(bare))


def test_reason_on_counting_all_failures_is_regenerated() -> None:
    """2026-11-08 숨은 테스트 검토: 14개 케이스 모두 사유를 Burst.wrong(...) 에 붙였다 — 어떤 실패든 그 사유로 보인다."""
    on_wrong = HC1.replace("Burst.failures(outcomes)", "Burst.wrong(outcomes)")
    res = _run(
        _overlays(
            hidden=[
                _f(f"{CASES}/OrderStatusBurstTest.java", on_wrong),
                render_params(PLAN, JAVA.manifest),
            ]
        )
    )
    assert _has(res.for_target("gen_hidden"), "모든 실패를 세는 단언에 사유", "Burst.failures")
    # 사유 없는 Burst.wrong 단언은 괜찮다
    bare = HC1.replace(
        'assertThat(Burst.failures(outcomes))\n                .as(Reasons.as("pool_acquire_timeout", "버스트 %d", 1)).isZero();',
        'assertThat(Burst.failures(outcomes))\n                .as(Reasons.as("pool_acquire_timeout", "버스트 %d", 1)).isZero();\n'
        "        org.assertj.core.api.Assertions.assertThat(Burst.wrong(outcomes)).isZero();",
    )
    assert bare != HC1
    res = _run(
        _overlays(
            hidden=[
                _f(f"{CASES}/OrderStatusBurstTest.java", bare),
                render_params(PLAN, JAVA.manifest),
            ]
        )
    )
    assert not _has(res.for_target("gen_hidden"), "모든 실패를 세는 단언")


ROLLBACK = """package com.example.service.grading.cases;

import com.example.service.grading.GraderJdbcHarness;
import com.example.service.grading.Reasons;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

class OrderRollbackTest extends ContractCase {
    @Test
    @DisplayName("hc1 저장 실패 시 성공 기록 없음")
    void hc1() throws Exception {
        INJECT
        var r = patchOrdersIdStatus(HC1_ORDER_READY_FIRST, java.util.Map.of(), "{\\"status\\":\\"SHIPPED\\"}");
        org.assertj.core.api.Assertions.assertThat(r.statusCode())
                .as(Reasons.as("rollback_left_success", "성공 기록 남음")).isNotEqualTo(200);
    }
}
"""


def test_reason_requires_what_causes_it() -> None:
    """2026-11-08 숨은 테스트 검토: rollback_left_success 5건 중 4건이 주입 없이 거절되는 전이를 보내 롤백될 변경이 없었다."""
    plan = HiddenPlan.model_validate(
        _plan(
            harness={"pool_size": 8, "concurrency": 32, "connection_timeout_ms": 5000},
            cases=[
                {
                    **_plan()["cases"][0],
                    "intended_reason": "rollback_left_success",
                    "scenario": "저장 실패",
                }
            ],
        )
    )

    def gen_hidden(case: str) -> list[str]:
        files = [_f(f"{CASES}/OrderRollbackTest.java", case), render_params(plan, JAVA.manifest)]
        return _run(_overlays(hidden=files), plan=plan).for_target("gen_hidden")

    no_inject = gen_hidden(ROLLBACK.replace("INJECT", "// 거절되는 전이"))
    assert _has(no_inject, "hc1", "rollback_left_success", "GraderJdbcHarness 주입")
    injected = gen_hidden(
        ROLLBACK.replace(
            "INJECT",
            'GraderJdbcHarness.failCommitAfter = java.util.regex.Pattern.compile("(?i)insert");',
        )
    )
    assert not _has(injected, "그 사유를 일으키는 것을 쓰지 않음")
    # 운영 피크 사유는 동시 요청을 써야 한다
    single = HC1.replace(
        "var outcomes = Burst.fire(n, i -> 200, i -> 200, REQUEST_TIMEOUT);",
        "var outcomes = java.util.List.<Burst.Outcome>of();",
    )
    assert single != HC1
    res = _run(
        _overlays(
            hidden=[
                _f(f"{CASES}/OrderStatusBurstTest.java", single),
                render_params(PLAN, JAVA.manifest),
            ]
        )
    )
    assert _has(res.for_target("gen_hidden"), "hc1", "pool_acquire_timeout", "peak(...)")
    assert not _has(_run().for_target("gen_hidden"), "그 사유를 일으키는 것을 쓰지 않음")


def test_unknown_harness_members_are_regenerated() -> None:
    """2026-11-08 상류 평가: 하네스 문서의 `IntUnaryOperator expectedStatus` 를 `Burst.IntUnaryOperator` 로 써 컴파일 실패."""
    nested = HC1.replace(
        "var outcomes = Burst.fire(n, i -> 200, i -> 200, REQUEST_TIMEOUT);",
        "Burst.IntUnaryOperator exp = i -> 200;\n        var outcomes = Burst.fire(n, exp, i -> 200, REQUEST_TIMEOUT);"
        "\n        // Burst.notInComments 는 무시\n        long w = Burst.wrongs(outcomes);",
    )
    assert nested != HC1
    res = _run(
        _overlays(
            hidden=[
                _f(f"{CASES}/OrderStatusBurstTest.java", nested),
                render_params(PLAN, JAVA.manifest),
            ]
        )
    )
    msgs = res.for_target("gen_hidden")
    assert _has(
        msgs, "하네스에 없는 멤버", "Burst.IntUnaryOperator", "import 해서 그냥 IntUnaryOperator"
    )
    assert _has(msgs, "Burst.wrongs (가까운 이름: Burst.wrong")
    assert not _has(msgs, "notInComments")
    assert not _has(_run().for_target("gen_hidden"), "하네스에 없는 멤버")


def test_injection_helper_counts_as_what_causes_a_rollback() -> None:
    """2026-11-08: 도우미 injectCommitFailure(verb, TABLE_…) 도 롤백 사유의 필수 패턴을 채운다."""
    plan = HiddenPlan.model_validate(
        _plan(
            harness={"pool_size": 8, "concurrency": 32, "connection_timeout_ms": 5000},
            cases=[{**_plan()["cases"][0], "intended_reason": "rollback_left_success"}],
        )
    )
    case = ROLLBACK.replace("INJECT", 'injectCommitFailure("update", TABLE_ORDER);')
    files = [_f(f"{CASES}/OrderRollbackTest.java", case), render_params(plan, JAVA.manifest)]
    assert not _has(
        _run(_overlays(hidden=files), plan=plan).for_target("gen_hidden"),
        "그 사유를 일으키는 것을 쓰지 않음",
    )


def test_status_pinned_after_an_injected_failure_is_regenerated() -> None:
    """2026-11-09 알파: hc2 가 커밋 실패 주입 뒤 `statusCode() < 500` 을 단언해 실패를 알린(500) 구현이 모두 떨어졌다."""
    plan = HiddenPlan.model_validate(
        _plan(
            harness={"pool_size": 8, "concurrency": 32, "connection_timeout_ms": 5000},
            cases=[{**_plan()["cases"][0], "intended_reason": "rollback_left_success"}],
        )
    )

    def msgs(inject: str, check: str) -> list[str]:
        case = ROLLBACK.replace("INJECT", inject).replace(".isNotEqualTo(200)", check)
        files = [_f(f"{CASES}/OrderRollbackTest.java", case), render_params(plan, JAVA.manifest)]
        return static_check(
            _overlays(hidden=files),
            template=JAVA,
            plan=plan,
            spec=SpecView.from_task(_task()),
            contract=_contract_with_list(),
            seed=BACKEND_SEED,
        ).for_target("gen_hidden")

    inject = 'injectCommitFailure("update", TABLE_ORDER);'
    for pinned in (".isLessThan(500)", ".isEqualTo(200)", ".isEqualTo(500)", ".isIn(200, 409)"):
        assert _has(msgs(inject, pinned), "주입한 요청의 응답 코드를 단정"), pinned
    # '성공 아님'만 보는 단언은 그대로, 주입이 없는 케이스의 상태 단언도 그대로
    for loose in (".isNotEqualTo(200)", ".isGreaterThanOrEqualTo(400)"):
        assert not _has(msgs(inject, loose), "응답 코드를 단정"), loose
    assert not _has(msgs("", ".isEqualTo(200)"), "응답 코드를 단정")
