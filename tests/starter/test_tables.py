"""엔티티 → 테이블 이름 (starter/tables.py) 과 그 정적 검사 — 가상 데이터만.

스택별 기대값은 2026-11-07 공식 문서·소스로 확인한 표 (schemas/template.py TableNaming docstring).
"""

from __future__ import annotations

import pytest

from task_spec.schemas import GeneratedFile, TaskContract
from task_spec.schemas.template import TableNaming
from task_spec.starter.contract import SpecView
from task_spec.starter.render import contract_markdown
from task_spec.starter.static_check import Overlay, static_check
from task_spec.starter.tables import (
    declared_tables,
    pluralize,
    referenced_tables,
    same_table,
    table_label,
    table_name,
)
from tests.starter.test_contract import BACKEND_SEED, _contract
from tests.starter.test_plan_contract import _task
from tests.starter.test_static_check import CASES, HC1, JAVA, PLAN


@pytest.mark.parametrize(
    ("naming", "table", "label"),
    [
        # Spring Boot JPA — CamelCaseToUnderscoresNamingStrategy, 소문자
        (TableNaming(style="snake"), "delivery_audit_log", "테이블 `delivery_audit_log`"),
        # Rails ActiveRecord — 복수형, 단어 사이 _
        (TableNaming(style="snake_plural"), "delivery_audit_logs", "테이블 `delivery_audit_logs`"),
        # Django — <앱>_ + 클래스 이름 소문자 (단어 사이 _ 없음)
        (
            TableNaming(style="app_lower", app_label="myapp"),
            "myapp_deliveryauditlog",
            "테이블 `myapp_deliveryauditlog`",
        ),
        # Hibernate 기본 (Spring 없이) — 이름 그대로·따옴표 없음 → PostgreSQL 은 소문자로 접음
        (
            TableNaming(style="as_is", unquoted_case="lower"),
            "DeliveryAuditLog",
            "테이블 `DeliveryAuditLog` (DB 에서는 `deliveryauditlog`)",
        ),
        # 같은 이름을 표준대로 대문자로 접는 DB (Oracle 등)
        (
            TableNaming(style="as_is", unquoted_case="upper"),
            "DeliveryAuditLog",
            "테이블 `DeliveryAuditLog` (DB 에서는 `DELIVERYAUDITLOG`)",
        ),
        # Prisma (@@map 없음) — 따옴표로 감싸 대소문자 유지
        (
            TableNaming(style="as_is", quoted=True),
            "DeliveryAuditLog",
            "테이블 `DeliveryAuditLog`",
        ),
    ],
)
def test_table_names_per_stack(naming: TableNaming, table: str, label: str) -> None:
    assert table_name("DeliveryAuditLog", naming) == table
    assert table_label(table, naming) == label


def test_no_tables_and_app_label_required() -> None:
    assert table_name("DeliveryAuditLog", TableNaming(style="none")) is None
    with pytest.raises(ValueError, match="app_label"):
        TableNaming(style="app_lower")


@pytest.mark.parametrize(
    ("word", "plural"),
    [
        ("log", "logs"),
        ("category", "categories"),
        ("day", "days"),
        ("box", "boxes"),
        ("match", "matches"),
    ],
)
def test_pluralize(word: str, plural: str) -> None:
    assert pluralize(word) == plural


def test_acronyms_follow_each_stack() -> None:
    # Spring: 앞 소문자·자신 대문자·뒤 소문자일 때만 _ / Rails: 약어 덩어리도 나눔
    assert table_name("HTTPRequestLog", TableNaming(style="snake")) == "httprequest_log"
    assert table_name("HTTPRequestLog", TableNaming(style="snake_plural")) == "http_request_logs"


def test_reserved_words_become_plural() -> None:
    naming = TableNaming(style="snake", reserved=["order", "user", "values", "value"])
    assert table_name("Order", naming) == "orders"
    assert table_name("OrderHistory", naming) == "order_history"
    assert table_name("Value", naming) == "values_data"  # 복수형도 예약어
    assert table_name("Order", TableNaming(style="snake")) == "order"  # 목록이 없으면 그대로


def test_case_rules_for_matching() -> None:
    assert same_table("DELIVERY", "delivery", TableNaming(style="snake"))
    assert not same_table("delivery", "Delivery", TableNaming(style="as_is", quoted=True))


JAVA_TABLES = JAVA.manifest.naming.table
assert JAVA_TABLES is not None


def test_java_template_declares_and_refs() -> None:
    files = [
        GeneratedFile(
            path="src/main/resources/schema.sql",
            content="CREATE TABLE IF NOT EXISTS orders (id BIGINT);\ncreate table `order_history` (id BIGINT);\n",
        ),
        GeneratedFile(
            path="src/main/java/com/example/service/order/Order.java",
            content='@Entity\n@Table(schema = "app", name = "orders")\nclass Order {}\n',
        ),
    ]
    assert declared_tables(files, JAVA_TABLES.declare_patterns) == ["orders", "order_history"]
    rules = JAVA.manifest.hidden_rules
    src = (
        'a = Pattern.compile("^\\\\s*update\\\\s+orders\\\\b", Pattern.CASE_INSENSITIVE);\n'
        'b = Pattern.compile("(?i)^\\\\s*insert\\\\s+into\\\\s+order_history\\\\b");\n'
        'c = Pattern.compile("delete from \\"Shipment\\" where");\n'
        'assertThat(x).as("update failed");\n'  # Pattern.compile 이 아닌 글은 보지 않는다
    )
    assert referenced_tables(src, rules.table_refs, rules.string_literal) == [
        "orders",
        "order_history",
        "Shipment",
    ]


def _check(starter: str, hidden: str, contract: TaskContract | None = None):  # type: ignore[no-untyped-def]
    overlays = [
        Overlay(
            kind="starter",
            files=(GeneratedFile(path="src/main/resources/schema.sql", content=starter),),
        ),
        Overlay(
            kind="hidden",
            files=(GeneratedFile(path=f"{CASES}/OrderStatusBurstTest.java", content=hidden),),
        ),
    ]
    return static_check(
        overlays,
        template=JAVA,
        plan=PLAN,
        spec=SpecView.from_task(_task()),
        contract=contract or TaskContract.model_validate(_contract()),
        seed=BACKEND_SEED,
    )


def test_static_check_tables() -> None:
    """2026-11-07 생성 스모크: 숨은 테스트가 테이블을 경로에서 짐작(`deliveries`)해 오류 주입이 걸리지 않았다."""
    inject = HC1.replace(
        "        int n =",
        '        GraderJdbcHarness.failStatements = Pattern.compile("^\\\\s*update\\\\s+{t}\\\\b");\n        int n =',
    )
    ok = _check(
        "CREATE TABLE orders (id BIGINT);\nCREATE TABLE order_history (id BIGINT);\n",
        inject.replace("{t}", "ORDERS"),  # 대소문자는 무시 (따옴표 없는 스택)
    )
    assert not [p for p in ok.problems if "테이블" in p.message], ok.problems
    bad = _check("CREATE TABLE order_items (id BIGINT);\n", inject.replace("{t}", "order"))
    starter = bad.for_target("gen_starter")
    hidden = bad.for_target("gen_hidden")
    assert any("order_items" in p and "Order → orders" in p for p in starter), starter
    assert any("계약에 없는 테이블" in p and ": order " in p for p in hidden), hidden


def test_contract_markdown_shows_tables() -> None:
    md = contract_markdown(TaskContract.model_validate(_contract()), tables=JAVA_TABLES)
    assert "#### `Order` — 테이블 `orders`" in md
    assert "#### `OrderHistory` — 테이블 `order_history`" in md
    assert "테이블" not in contract_markdown(TaskContract.model_validate(_contract()))


def test_broad_injection_pattern_is_reasked() -> None:
    """2026-11-07 루프: `failCommitAfter = "delivery|delivery_audit_log"` 가 실패 기록 INSERT 의 커밋까지 막아 어떤 설계도 통과 못 했다."""
    broad = HC1.replace(
        "        int n =",
        '        GraderJdbcHarness.failCommitAfter = Pattern.compile("orders|order_history", Pattern.CASE_INSENSITIVE);\n'
        '        var iso = Pattern.compile("\\\\d{4}-\\\\d{2}-\\\\d{2}T");\n'  # SQL 이 아닌 정규식은 보지 않는다
        "        int n =",
    )
    res = _check("CREATE TABLE orders (id BIGINT);\n", broad)
    msgs = [p for p in res.for_target("gen_hidden") if "SQL 종류 없이 넓음" in p]
    assert len(msgs) == 1 and '"orders|order_history"' in msgs[0] and "\\d{4}" not in msgs[0]
    narrow = broad.replace('"orders|order_history"', '"^\\\\s*update\\\\s+orders\\\\b"')
    assert not [
        p
        for p in _check("CREATE TABLE orders (id BIGINT);\n", narrow).for_target("gen_hidden")
        if "넓음" in p
    ]


def test_starter_must_not_invent_foreign_keys() -> None:
    """2026-11-07 루프: 계약에 관계가 없는데 시작 코드가 감사 기록 → 배송 건 외래 키를 걸어 없는 배송 건 요청의 기록이 거부됐다."""
    schema = "CREATE TABLE orders (id BIGINT);\nCREATE TABLE order_history (id BIGINT, order_id BIGINT, FOREIGN KEY (order_id) REFERENCES orders(id));\n"
    msgs = [p for p in _check(schema, HC1).for_target("gen_starter") if "외래 키" in p]
    assert len(msgs) == 1 and "order_id" in msgs[0] and "외래 키(없음)" in msgs[0]
    # 계약 foreign_keys 에 적힌 관계는 된다
    c = _contract()
    c["entities"][1]["foreign_keys"] = ["orderId → Order"]
    contract = TaskContract.model_validate(c)
    assert not [
        p for p in _check(schema, HC1, contract).for_target("gen_starter") if "외래 키" in p
    ]
    # JPA 관계 애너테이션도 외래 키 — 계약에 없으면 막는다
    jpa = Overlay(
        kind="starter",
        files=(
            GeneratedFile(
                path="src/main/java/com/example/service/order/OrderHistory.java",
                content="@ManyToOne\nOrder order;\n",
            ),
        ),
    )
    from task_spec.starter.static_check import _starter_foreign_keys

    assert _starter_foreign_keys(jpa, JAVA, TaskContract.model_validate(_contract()))
    assert not _starter_foreign_keys(jpa, JAVA, contract)


def test_contract_foreign_keys_are_checked_and_shown() -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    from task_spec.starter.contract import _foreign_keys

    c = _contract()
    c["entities"][1]["foreign_keys"] = ["orderId → Order", "nope -> Ghost"]
    contract = TaskContract.model_validate(c)
    problems = _foreign_keys(contract)
    assert any("OrderHistory.nope" in p and "없는 필드" in p for p in problems) and any(
        "Ghost" in p for p in problems
    )
    c["entities"][1]["foreign_keys"] = ["orderId references Order"]
    with _pytest.raises(ValidationError, match="필드 → 엔티티"):
        TaskContract.model_validate(c)
    c["entities"][1]["foreign_keys"] = ["orderId → Order"]
    md = contract_markdown(TaskContract.model_validate(c), tables=JAVA_TABLES)
    assert "외래 키: `orderId` → `Order`" in md and "외래 키는 위 엔티티에 적힌 것만 둔다" in md
