"""계약 엔티티 → DB 테이블 이름 (템플릿 manifest `naming.table`, spec-to-code §3.3) — 결정적.

숨은 테스트 생성기는 시작 코드를 보지 않으므로(§2) SQL 을 가리키는 오류 주입 정규식의 테이블 이름을 계약에서 알아야 한다.
2026-11-07 생성 스모크: 테이블을 경로에서 짐작(`deliveries`, 실제 `delivery`)해 주입이 걸리지 않았다.
이름 규칙은 스택마다 다르다 (2026-11-07 문서·소스로 확인, `TableNaming` docstring 표).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from task_spec.schemas import GeneratedFile, TaskContract
from task_spec.schemas.template import TableNaming

_VOWELS = set("aeiou")


def spring_snake(name: str) -> str:
    """Spring Boot `CamelCaseToUnderscoresNamingStrategy` 와 같은 규칙 — 앞이 소문자·자신이 대문자·뒤가 소문자인 글자 앞에 `_`, 점은 `_`, 전부 소문자.
    예: DeliveryAuditLog → delivery_audit_log, HTTPRequestLog → httprequest_log."""
    s = name.replace(".", "_")
    out = [s[0]] if s else []
    for i in range(1, len(s) - 1):
        before, cur, after = s[i - 1], s[i], s[i + 1]
        if before.islower() and cur.isupper() and after.islower():
            out.append("_")
        out.append(cur)
    if len(s) > 1:
        out.append(s[-1])
    return "".join(out).lower()


def rails_underscore(name: str) -> str:
    """ActiveSupport `underscore` 꼴 — 약어 덩어리도 나눈다. 예: HTTPRequestLog → http_request_log."""
    s = re.sub(r"([A-Z\d]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.replace("-", "_").lower()


def pluralize(word: str) -> str:
    """영어 규칙 복수형 (Rails 의 불규칙 사전은 다루지 않음 — person → persons). log → logs, category → categories, box → boxes."""
    if re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in _VOWELS:
        return word[:-1] + "ies"
    return word + "s"


def _plural_last(name: str) -> str:
    head, sep, last = name.rpartition("_")
    return f"{head}{sep}{pluralize(last)}"


def _base_name(entity: str, naming: TableNaming) -> str | None:
    match naming.style:
        case "snake":
            return spring_snake(entity)
        case "snake_plural":
            return _plural_last(rails_underscore(entity))
        case "app_lower":
            return f"{naming.app_label}_{entity.lower()}"
        case "as_is":
            return entity
        case _:
            return None


def table_name(entity: str, naming: TableNaming) -> str | None:
    """코드(DDL·ORM)가 쓰는 테이블 이름. none 이면 None.
    규칙으로 나온 이름이 템플릿의 SQL 예약어면 복수형으로 (Order → order → orders), 그래도 예약어면 `_data` 를 붙인다."""
    name = _base_name(entity, naming)
    if name is None:
        return None
    reserved = {w.lower() for w in naming.reserved}
    if name.lower() in reserved:
        name = _plural_last(name)
        if name.lower() in reserved:
            name += "_data"
    return name


def stored_name(name: str, naming: TableNaming) -> str | None:
    """DB 에 실제로 저장되는 이름이 코드의 이름과 다르면 그 이름 — 따옴표 없는 이름을 DB 가 접을 때만 (PostgreSQL 소문자 등)."""
    if naming.quoted or naming.unquoted_case is None:
        return None
    folded = name.lower() if naming.unquoted_case == "lower" else name.upper()
    return folded if folded != name else None


def contract_tables(contract: TaskContract, naming: TableNaming | None) -> dict[str, str]:
    """엔티티 id → 테이블 이름 (규칙이 없거나 none 이면 빈 dict)."""
    if naming is None:
        return {}
    out: dict[str, str] = {}
    for m in contract.entities:
        name = table_name(m.name, naming)
        if name:
            out[m.entity_id] = name
    return out


def table_label(name: str, naming: TableNaming) -> str:
    """README·생성기 입력의 표기 — 예: 테이블 `delivery` / 테이블 `DeliveryAuditLog` (DB 에서는 `deliveryauditlog`)."""
    stored = stored_name(name, naming)
    return f"테이블 `{name}`" + (f" (DB 에서는 `{stored}`)" if stored else "")


def same_table(a: str, b: str, naming: TableNaming) -> bool:
    """따옴표로 감싸는 스택은 대소문자까지, 아니면 대소문자 무시 (DB 가 접는다)."""
    return a == b if naming.quoted else a.lower() == b.lower()


def declared_tables(files: Iterable[GeneratedFile], patterns: Sequence[str]) -> list[str]:
    """시작 코드가 선언한 테이블 이름 (나온 순서, 중복 없음)."""
    found: list[str] = []
    for f in files:
        for pat in patterns:
            for m in re.finditer(pat, f.content, re.I):
                if m.group(1) not in found:
                    found.append(m.group(1))
    return found


def referenced_tables(content: str, patterns: Sequence[str], literal: str) -> list[str]:
    """숨은 테스트 파일의 문자열 상수 중 SQL 을 가리키는 것에서 뽑은 테이블 이름."""
    found: list[str] = []
    for lit in re.finditer(literal, content):
        for pat in patterns:
            for m in re.finditer(pat, lit.group(1), re.I):
                if m.group(1) not in found:
                    found.append(m.group(1))
    return found
