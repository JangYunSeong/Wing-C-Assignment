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
import com.example.service.grading.GraderSupport;
import com.example.service.grading.Reasons;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

class OrderStatusBurstTest extends GraderSupport {
    @Test
    @DisplayName("hc1 피크와 같은 수의 동시 변경")
    void hc1() throws Exception {
        int n = GraderParams.intValue("concurrency", 32);
        var outcomes = Burst.fire(n, i -> 200, i -> 200, REQUEST_TIMEOUT);
        org.assertj.core.api.Assertions.assertThat(Burst.wrong(outcomes))
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
