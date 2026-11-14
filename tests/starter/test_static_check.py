"""오버레이 정적 검사 static_check (starter/static_check.py, spec-to-code §6) — 가상 데이터만, 네거티브 위주."""

from __future__ import annotations

import re
from typing import Any

from task_spec.schemas import GeneratedFile, HiddenPlan
from task_spec.starter.contract import SpecView
from task_spec.starter.render import render_params, render_readme
from task_spec.starter.static_check import Overlay, identifier_words, static_check
from task_spec.starter.templates import load_template
from tests.starter.test_contract import BACKEND_SEED, _plan
from tests.starter.test_plan_contract import _task
from tests.starter.test_render import _approved, _contract_with_list

# 뼈대 없는 v1 로 고정 — 이 파일·생성 테스트의 예시 오버레이는 뼈대 전 모양이다 (뼈대 검사는 test_skeleton.py, CURRENT v2)
JAVA = load_template("backend/java-spring-boot3-jpa", 1)
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
    assert _has(
        res.for_target("gen_starter"), "build.gradle", "잠긴 파일"
    )  # 막힌 까닭마다 안내 (#178)
    assert _has(res.for_target("gen_starter"), "Sneaky.java")
    assert _has(res.for_target("gen_hidden"), "GraderSupport.java", "잠긴 채점 하네스")
    assert _has(res.for_target("gen_naive"), "ref_naive(naive-all)", "OrderTest.java")


def test_limits() -> None:
    many = [_f(f"{PKG}/gen/C{i}.java", "class C {}") for i in range(61)]
    big = [_f(f"{PKG}/Big.java", "x" * 100_001)]
    assert _has(_run(_overlays(starter=many)).for_target("gen_starter"), "파일 수 61", "상한 60")
    assert _has(_run(_overlays(starter=big)).for_target("gen_starter"), "크기 상한")
    # 코드가 계약에서 쓰는 시드 파일(data.sql)은 크기 상한에서 뺀다 — 2026-11-11 N+1 백엔드: 시드 2천여 행으로 100000B 초과
    seed = [_f("src/main/resources/data.sql", "INSERT INTO t VALUES (1);\n" * 5000)]
    assert not _has(_run(_overlays(starter=seed)).for_target("gen_starter"), "크기 상한")
    # 코드가 쓰는 다른 파일(조절값·계약 도우미)도 같다 — 생성기가 고칠 수 없다 (#178)
    m = JAVA.manifest
    assert m.hidden_layout is not None and m.hidden_helper is not None
    coded = [
        _f(m.hidden_layout.params, "x=1\n" * 30_000),
        _f(m.hidden_helper.path, "// x\n" * 30_000),
        _f(f"{CASES}/OrderStatusBurstTest.java", HC1),
    ]
    assert not _has(_run(_overlays(hidden=coded)).for_target("gen_hidden"), "크기 상한")


def test_data_files_in_blocked_paths_get_the_same_hint() -> None:
    """#178: 데이터 파일 안내가 `test/resources` 꼴에만 붙었다 — 막힌 경로면 까닭(잠김·채점 자리·쓸 수 있는 자리 밖)과 함께 모두 안내한다."""
    hint = "테스트용 데이터·설정 파일을 따로 두지 않는다"
    starter = _run(_overlays(starter=[_f("src/test/resources/seed-test.sql", "INSERT …;")]))
    assert _has(starter.for_target("gen_starter"), "seed-test.sql", "fill_slots", hint)
    hidden = _run(
        _overlays(
            hidden=[
                _f(f"{CASES}/OrderStatusBurstTest.java", HC1),
                render_params(PLAN, JAVA.manifest),
                _f("src/main/resources/test-data.sql", "INSERT …;"),
            ]
        )
    )
    assert _has(hidden.for_target("gen_hidden"), "test-data.sql", hint)


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
    # 기대값만 Integer 로 맞춘 비교(.intValue())도 면제하지 않는다 — 큰 값(Long)에서 같은 오류 (#178)
    only_int = raw.replace(
        '        org.assertj.core.api.Assertions.assertThat(row.get("orderId")).isEqualTo(1L);\n',
        "",
    )
    assert [m for m in hidden(only_int) if "JSON Map 값 그대로" in m]
    # 캐스트한 꼴·null 확인·문자열 필드만 있으면 통과
    ok = only_int.replace(
        '        org.assertj.core.api.Assertions.assertThat(row.get("orderId")).isEqualTo(((Number) id).intValue());\n',
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
    """2026-11-09 알파: hc2 가 커밋 실패 주입 뒤 `statusCode() < 500` 을 단언해 실패를 알린(500) 구현이 모두 떨어졌다.
    2026-11-12 코드 리뷰(PR #121): '성공이 아님'(isNotEqualTo(200)·≥400)도 단정 — 응답을 먼저 주고 뒤에 저장하는 허용 설계는 2xx."""
    plan = HiddenPlan.model_validate(
        _plan(
            harness={"pool_size": 8, "concurrency": 32, "connection_timeout_ms": 5000},
            cases=[{**_plan()["cases"][0], "intended_reason": "rollback_left_success"}],
        )
    )
    status_assert = (
        "        org.assertj.core.api.Assertions.assertThat(r.statusCode())\n"
        '                .as(Reasons.as("rollback_left_success", "성공 기록 남음")).isNotEqualTo(200);'
    )
    assert status_assert in ROLLBACK

    def msgs(inject: str, body: str) -> list[str]:
        case = ROLLBACK.replace("INJECT", inject).replace(status_assert, body)
        files = [_f(f"{CASES}/OrderRollbackTest.java", case), render_params(plan, JAVA.manifest)]
        return static_check(
            _overlays(hidden=files),
            template=JAVA,
            plan=plan,
            spec=SpecView.from_task(_task()),
            contract=_contract_with_list(),
            seed=BACKEND_SEED,
        ).for_target("gen_hidden")

    def on_status(matcher: str) -> str:
        return f"        org.assertj.core.api.Assertions.assertThat(r.statusCode()){matcher};"

    # 응답 코드 대신 조회로 판정 — 기록을 읽는 GET 의 200 단언도 맞다
    # (2026-11-09 재실행: 처음 검사가 감사 조회 단언까지 잡아 숨은 테스트 생성 실패)
    by_record = (
        "        var audit = getOrders(java.util.Map.of());\n"
        "        org.assertj.core.api.Assertions.assertThat(audit.statusCode()).isEqualTo(200);\n"
        "        org.assertj.core.api.Assertions.assertThat(audit.body())"
        '.as(Reasons.as("rollback_left_success", "성공 기록 남음")).isNotNull();'
    )
    inject = 'injectCommitFailure("update", TABLE_ORDER);'
    for pinned in (
        ".isLessThan(500)",
        ".isEqualTo(200)",
        ".isEqualTo(500)",  # 계약이 선언하지 않은 5xx
        ".isIn(200, 409)",
        ".isNotEqualTo(200)",
        ".isGreaterThanOrEqualTo(400)",
    ):
        assert _has(msgs(inject, on_status(pinned)), "주입한 요청의 응답 코드를 단정"), pinned
    # #178: 원래 사례 꼴(비교식을 단언)도 응답 코드 단정 — 예전엔 `assertThat(r.statusCode())` 로 닫힌 꼴만 봐 지나갔다
    compared = "        org.assertj.core.api.Assertions.assertThat(r.statusCode() < 500).isTrue();"
    assert _has(msgs(inject, compared), "주입한 요청의 응답 코드를 단정")
    assert not _has(msgs(inject, by_record), "응답 코드를 단정")
    # 주입이 없는 케이스의 상태 단언은 그대로
    assert not _has(msgs("", on_status(".isEqualTo(200)")), "응답 코드를 단정")
    # reset 뒤에 보낸 상태 변경 요청도 주입 밖이다
    after = (
        '\n        GraderJdbcHarness.reset();\n        var again = patchOrdersIdStatus(1L, java.util.Map.of(), "{}");\n'
        + "        org.assertj.core.api.Assertions.assertThat(again.statusCode()).isEqualTo(200);"
    )
    assert not _has(msgs(inject, by_record + after), "응답 코드를 단정")


def test_code_that_builds_or_resizes_the_pool_is_a_harness_bypass() -> None:
    # 2026-11-09: 허용 구현 d1·d2·d3 가 DataSource 빈·BeanPostProcessor 로 풀을 80 으로 바꿔 하네스가 넣는 운영 값을 우회했다
    # ("so the grading harness's property override does not reduce it" 라는 주석까지) — 지원자도 같은 길로 함정을 피할 수 있다
    from task_spec.starter.static_check import HARNESS_OVERRIDE, _code_overrides

    t = JAVA
    bean = (
        "@Configuration class C {\n  @Bean @Primary\n  public DataSource dataSource(DataSourceProperties p) {\n"
        "    HikariDataSource ds = p.initializeDataSourceBuilder().type(HikariDataSource.class).build();\n"
        "    ds.setMaximumPoolSize(80); return ds; } }"
    )
    post = (
        "@Component class P implements BeanPostProcessor { public Object postProcessAfterInitialization(Object b, String n) {\n"
        "  if (b instanceof HikariDataSource ds) { ds.setMaximumPoolSize(80); } return b; } }"
    )
    plain = "@Service class S { @Transactional public void change() { repo.save(x); } }"
    for content, bad in ((bean, True), (post, True), (plain, False)):
        o = Overlay(
            kind="ref_accepted",
            files=(GeneratedFile(path="src/main/java/x/C.java", content=content),),
            label="d3",
        )
        got = _code_overrides(o, t)
        assert bool(got) is bad, (content, got)
        # 허용 구현이 이것만으로 재요청에도 못 고치면 그 설계를 빼는 기존 장치(build._voided)가 알아보게 같은 말
        assert all(HARNESS_OVERRIDE in p.message for p in got)


def test_datasource_settings_not_in_the_template_are_rejected() -> None:
    # 2026-11-10 v19-be: 고정 H2 URL 로 테스트 컨텍스트끼리 DB 를 나눠 써 schema.sql 이 두 번 돌아 기동 실패
    from task_spec.starter.static_check import _datasource_config

    fixed = "spring:\n  datasource:\n    url: jdbc:h2:mem:testdb;DB_CLOSE_DELAY=-1\n  sql:\n    init:\n      mode: always\n"
    plain = "spring:\n  jpa:\n    hibernate:\n      ddl-auto: none\n  sql:\n    init:\n      mode: always\n"
    for content, bad in ((fixed, True), (plain, False)):
        o = Overlay(
            kind="starter",
            files=(GeneratedFile(path="src/main/resources/application.yml", content=content),),
        )
        got = _datasource_config(o, JAVA)
        assert bool(got) is bad, got
        assert not got or "spring.datasource.url" in got[0].message


def test_java_runtime_only_pitfalls_in_starter_tests_and_schema() -> None:
    # 2026-11-10 stab3·v19-be: 시작 코드의 기존 기능 테스트가 실행 때만 깨져 재생성 예산을 다 씀 (작은 프로젝트로 재현)
    from task_spec.starter.static_check import _java_test_pitfalls

    seed = JAVA.manifest.seed_sql.path if JAVA.manifest.seed_sql else None

    def probs(*files: tuple[str, str]) -> list[str]:
        o = Overlay(kind="starter", files=tuple(GeneratedFile(path=p, content=c) for p, c in files))
        return [p.message for p in _java_test_pitfalls(o, JAVA, seed)]

    test = "src/test/java/x/RankTest.java"
    patch = 'class T { @Autowired TestRestTemplate rest; void t() { rest.exchange("/a", HttpMethod.PATCH, e, Map.class); } }'
    mvc = 'class T { @Autowired MockMvc mvc; void t() throws Exception { mvc.perform(patch("/a")); } }'
    assert any("Invalid HTTP method: PATCH" in p for p in probs((test, patch)))
    assert not probs((test, mvc))
    schema = (
        "src/main/resources/schema.sql",
        'CREATE TABLE m (id BIGINT, "rank" VARCHAR(20), note VARCHAR(9) DEFAULT \'"x"\');',
    )
    plain = ("src/main/java/x/M.java", '@Column(name = "rank") private String rank;')
    quoted = ("src/main/java/x/M.java", '@Column(name = "\\"rank\\"") private String rank;')
    [p] = probs(schema, plain)
    assert "rank" in p and "따옴표 없이 매핑" in p and '"x"' not in p.split("—")[0]
    assert not probs(
        schema, quoted
    )  # 엔티티가 같은 표기 — 시드 SQL 도 스키마 표기를 따른다 (stab1 통과)
    # 코드가 쓰는 시드 파일은 보지 않는다
    assert not probs(
        ("src/main/resources/data.sql", "INSERT INTO m (id, \"rank\") VALUES (1, 'A');"), plain
    )


def test_peak_on_a_case_without_burst_rows_is_rejected() -> None:
    # 2026-11-10 nplus1-g: 목록 한 번 조회 케이스를 peak 로 동시 버스트해 조인 조회 허용 구현까지 시간 초과
    from task_spec.starter.static_check import _reason_rules

    plan = HiddenPlan.model_validate(
        _plan()
        | {
            "cases": [
                {
                    **_plan()["cases"][0],
                    "case_id": "hc1",
                    "intended_reason": "request_timeout",
                    "data": [{"entity_id": "m1", "rows": 1, "initial": {}, "burst": False}],
                }
            ]
        }
    )
    peak = '@DisplayName("hc1 목록") class Hc1Test { void t() { var o = peak(i -> 200, i -> getOrders().statusCode()); } }'
    once = '@DisplayName("hc1 목록") class Hc1Test { void t() { var o = Burst.fire(1, i -> 200, i -> getOrders().statusCode(), W); } }'
    path = "src/test/java/com/example/service/grading/cases/Hc1Test.java"
    rules = JAVA.manifest.hidden_rules
    args = (
        JAVA.manifest,
        plan,
        re.compile(rules.case_id_pattern),
        [re.compile(p) for p in rules.reason_patterns],
    )
    bad = _reason_rules([GeneratedFile(path=path, content=peak)], *args)
    assert any("버스트 행이 없는 케이스인데 peak" in p for p in bad)
    good = _reason_rules([GeneratedFile(path=path, content=once)], *args)
    assert not any("버스트 행이 없는" in p for p in good)


def test_path_problem_names_the_allowed_slots_and_test_data_alternative() -> None:
    # 2026-11-10 retrydupx #030: 갈 곳을 알려 주지 않아 시작 코드가 같은 테스트 리소스 경로에 두 번 써 생성 실패
    from task_spec.starter.static_check import _paths

    o = Overlay(
        kind="starter",
        files=(GeneratedFile(path="src/test/resources/seed-test.sql", content="INSERT 1"),),
    )
    [p] = _paths(o, JAVA)
    assert "쓸 수 있는 경로: src/main/java/" in p.message
    assert "시드의 테스트 몫 행" in p.message
    other = Overlay(kind="starter", files=(GeneratedFile(path="build.gradle", content="x"),))
    assert all("시드의 테스트 몫 행" not in x.message for x in _paths(other, JAVA))


def test_hidden_reason_outside_material_list_is_reasked() -> None:
    """재료가 사유를 적으면 빌드의 템플릿도 그 목록만 (BuildInputs — restrict_reasons) — 숨은 테스트가 목록 밖 사유를 붙이면 재요청.
    2026-11-13 갱신 손실 알파: '시간 초과·5xx 수' 확인에 재료에 없는 pool_acquire_timeout 을 붙여 500 이 '풀 대기 초과'로 채점됐다."""
    from task_spec.nodes.starter.generate import BuildInputs, hidden_input
    from task_spec.starter.templates import restrict_reasons

    seed = BACKEND_SEED.model_copy(update={"reasons": ["lost_update", "server_error"]})
    b = BuildInputs(
        task=_task(), contract=_contract_with_list(), plan=PLAN, template=JAVA, seed=seed
    )
    assert b.template.manifest.reason_codes == {"lost_update", "server_error"}
    assert restrict_reasons(b.template, seed.reasons) is b.template  # 몇 번 불러도 같다
    shown = hidden_input(b.task, b.contract, b.plan, b.template).failure_reasons
    assert (
        "server_error" in shown and "pool_acquire_timeout" not in shown
    )  # 생성기 입력도 같은 목록
    res = static_check(
        _overlays(),
        template=b.template,
        plan=PLAN,
        spec=SpecView.from_task(_task()),
        seed=seed,
        avoid_terms=("우아한형제들", "hibernate_sequence"),
    )
    assert _has(
        res.for_target("gen_hidden"),
        "쓸 수 있는 목록 밖: pool_acquire_timeout",
        "lost_update, server_error",
    )
    assert not _has(
        _run().for_target("gen_hidden"), "목록 밖"
    )  # 재료가 사유를 적지 않으면 템플릿 목록 전체


def test_generated_tests_may_not_skip() -> None:
    """#173: 건너뛰기 API 를 쓴 숨은·공개 테스트는 재요청 — 스택 언어별 표 (skip_apis.SKIP_APIS)."""
    from task_spec.starter.skip_apis import skip_hits

    skip_case = HC1.replace("    @Test\n", "    @Test\n    @Disabled\n").replace(
        "        int n =",
        '        Assumptions.assumeTrue(ready(), "출력이 없음");\n        int n =',
    )
    overlays = _overlays(
        hidden=[
            _f(f"{CASES}/OrderStatusBurstTest.java", skip_case),
            render_params(PLAN, JAVA.manifest),
        ]
    )
    msgs = _run(overlays).for_target("gen_hidden")
    assert _has(msgs, "테스트를 건너뜀", "@Disabled", "Assumptions"), msgs
    assert not _has(_run().for_target("gen_hidden"), "테스트를 건너뜀")
    fe = load_template("frontend/react-vite-ts").manifest
    py = load_template("data/python-duckdb").manifest
    assert (
        skip_hits(
            fe,
            "a.test.tsx",
            "it.skip('x', () => {})\ntest.skipIf(!ok)('y', fn)\nit('z', (ctx) => { ctx.skip() })",
        )
        and len(
            skip_hits(
                fe,
                "a.test.tsx",
                "it.skip('x', () => {})\ntest.skipIf(!ok)('y', fn)\nit('z', (ctx) => { ctx.skip() })",
            )
        )
        == 3
    )
    assert not skip_hits(fe, "a.test.tsx", "it('x', () => { rows.slice(1) })")
    assert (
        len(
            skip_hits(
                py,
                "t.py",
                "@pytest.mark.skipif(True, reason='x')\ndef t():\n    pytest.skip('no')\n    pytest.xfail('x')",
            )
        )
        == 3
    )
