"""계약 시드 → 시드 SQL (템플릿 manifest `seed_sql`) — README·조절값 파일처럼 코드가 쓴다. LLM 은 시드의 **규칙**(행 수·id 범위별 처음 값)만 쓴다.

2026-11-07 알파 3×3 평가: 시작 코드 생성기가 수백 행 data.sql 을 "-- id 81~360도 같은 방식으로" 같은 주석으로 생략해(3/7), 숨은 테스트가
배정받은 큰 id 가 없어 시작 코드·흔한 구현·허용 구현이 모두 같은 404 로 떨어졌다. id 직접 입력과 `GENERATED ALWAYS` 가 부딪힌 H2 오류도 있었다.
여기서 막는 것:
- 행 생략 — 행은 코드가 펼친다 (계약 `seed.rows`·`groups` 그대로).
- id 문법 — id 는 코드가 넣고, identity 열이면 템플릿이 정한 재설정 문장으로 다음 값을 시드 끝 다음으로 (앱이 새 행을 넣어도 안 겹침).
- 스키마 불일치 — `schema_problems` 가 시작 코드 schema.sql 에 시드 테이블·열이 있는지, id 가 직접 넣을 수 있는 열인지 Gradle 전에 본다.
그룹에 없는 필수 열은 형식별 결정적 기본값, 그룹에 없는 선택 열은 넣지 않는다(DB 기본값·NULL).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from task_spec.schemas.build import ContractField, Entity, GeneratedFile, SeedRule, TaskContract
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.tables import contract_tables, spring_snake

HEADER = "-- 시드 데이터 — 계약의 시드(행 수·id 범위별 처음 값)로 빌드 파이프라인이 쓴다. 손으로 고치지 않는다 (가상 데이터)."
FIXED_DATETIME = "2026-01-01T00:00:00"
_NUMBER = re.compile(r"^(?:long|int|integer|bigint|short|number|numeric|decimal|double|float)\b")
_DATETIME = re.compile(
    r"^(?:datetime|timestamp|instant|offsetdatetime|localdatetime|zoneddatetime)\b"
)
_SKIP_ITEMS = {"primary", "constraint", "foreign", "unique", "check", "key", "index"}


def column(name: str, style: str) -> str:
    return spring_snake(name) if style == "snake" else name


def _literal(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int | float):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _enum_first(type_: str) -> str | None:
    m = re.match(r"^enum\s*\(([^)]*)\)", type_.strip(), re.I)
    if not m:
        return None
    first = re.split(r"[|,]", m.group(1))[0].strip().strip("'\"")
    return first or None


def default_value(f: ContractField, row: int) -> object:
    """그룹 값이 없는 필수 필드의 결정적 기본값 (행 id 로 서로 다르게)."""
    t = f.type.strip().lower()
    if (first := _enum_first(f.type)) is not None:
        return first
    if _NUMBER.match(t):
        return row
    if t.startswith("bool"):
        return False
    if _DATETIME.match(t):
        return FIXED_DATETIME
    if t.startswith("date"):
        return FIXED_DATETIME[:10]
    if t.startswith("time"):
        return "00:00:00"
    return f"{f.name}-{row}"[:20]


def _insert_fields(m: Entity, s: SeedRule) -> list[ContractField]:
    """id 를 뺀 필드 중 넣을 것 — 어느 그룹이 값을 주거나 필수인 것."""
    grouped = {k for g in s.groups for k in g.values}
    return [f for f in m.fields if f.name != "id" and (f.name in grouped or f.required)]


def _value(f: ContractField, row: int, group: dict[str, object], fk_rows: dict[str, int]) -> object:
    if f.name in group:
        return group[f.name]
    if f.name in fk_rows:  # 계약 외래 키 — 대상 시드 행 안에서 돌린다
        n = fk_rows[f.name]
        return ((row - 1) % n) + 1 if n else None
    return default_value(f, row)


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def table_block(sql: str, table: str) -> str | None:
    """`CREATE TABLE <table> ( … )` 의 괄호 안 (괄호 깊이를 세어 VARCHAR(255) 같은 안쪽 괄호를 넘는다). 없으면 None."""
    m = re.search(
        rf"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?[`\"]?{re.escape(table)}[`\"]?\s*\(",
        sql,
        re.I,
    )
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(sql) and depth:
        depth += {"(": 1, ")": -1}.get(sql[i], 0)
        i += 1
    return sql[m.end() : i - 1]


def table_columns(block: str) -> dict[str, str]:
    """열 이름(소문자) → 정의 글. 제약 줄(PRIMARY KEY(…)·CONSTRAINT·FOREIGN KEY…)은 뺀다."""
    items: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in block:
        if ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        cur.append(ch)
    items.append("".join(cur))
    out: dict[str, str] = {}
    for item in items:
        text = re.sub(r"--[^\n]*", "", item).strip()
        if not text:
            continue
        name = text.split()[0].strip('`"').lower()
        if name not in _SKIP_ITEMS:
            out[name] = text
    return out


def _seeded(
    contract: TaskContract, manifest: TemplateManifest
) -> Iterator[tuple[Entity, SeedRule, str]]:
    tables = contract_tables(contract, manifest.naming.table)
    ents = {m.entity_id: m for m in contract.entities}
    for s in contract.seed:
        m, table = ents.get(s.entity_id), tables.get(s.entity_id)
        if m is not None and table is not None:
            yield m, s, table


def render_seed_sql(
    contract: TaskContract, manifest: TemplateManifest, schema_sql: str | None = None
) -> GeneratedFile | None:
    """계약 시드 → 시드 SQL 파일. 템플릿에 seed_sql 이 없으면 None. schema_sql 은 identity 열 재설정 여부를 보려고."""
    rule = manifest.seed_sql
    if rule is None:
        return None
    rows_of = {s.entity_id: s.rows for s in contract.seed}
    by_name = {m.name: m.entity_id for m in contract.entities}
    out = [HEADER]
    for m, s, table in _seeded(contract, manifest):
        if s.rows == 0:
            out.append(f"-- {m.name}: 0행 (처음엔 비어 있음)")
            continue
        fields = _insert_fields(m, s)
        fk_rows = {fk: rows_of.get(by_name.get(target, ""), 0) for fk, target in m.fk_pairs()}
        cols = ["id", *(column(f.name, rule.column_style) for f in fields)]
        groups = sorted(s.groups, key=lambda g: g.first)
        values: list[str] = []
        for row in range(1, s.rows + 1):
            g = next((x.values for x in groups if x.first <= row <= x.last), {})
            cells = [str(row), *(_literal(_value(f, row, dict(g), fk_rows)) for f in fields)]
            values.append("(" + ", ".join(cells) + ")")
        out.append(f"-- {m.name}: {s.rows}행 (id 1~{s.rows})")
        for part in _chunks(values, rule.chunk):
            out.append(f"INSERT INTO {table} ({', '.join(cols)}) VALUES\n" + ",\n".join(part) + ";")
        block = table_block(schema_sql or "", table)
        id_def = table_columns(block).get("id", "") if block else ""
        if rule.restart and re.search(r"identity|auto_increment", id_def, re.I):
            out.append(rule.restart.format(table=table, next=s.rows + 1))
    return GeneratedFile(path=rule.path, content="\n".join(out) + "\n")


def schema_problems(
    schema_sql: str | None, contract: TaskContract, manifest: TemplateManifest
) -> list[str]:
    """시작 코드 schema.sql 이 코드가 쓸 시드와 맞는가 — 시드 테이블·넣을 열이 있고, id 를 직접 넣을 수 있는가 (Gradle 전에)."""
    rule = manifest.seed_sql
    if rule is None:
        return []
    out: list[str] = []
    for m, s, table in _seeded(contract, manifest):
        if s.rows == 0:
            continue
        block = table_block(schema_sql or "", table)
        if block is None:
            out.append(
                f"{rule.schema_path} 에 시드 테이블 {table} ({m.name}) 의 CREATE TABLE 이 없음 — 계약의 테이블 이름 그대로 만든다"
            )
            continue
        cols = table_columns(block)
        want = ["id", *(column(f.name, rule.column_style) for f in _insert_fields(m, s))]
        missing = [c for c in want if c.lower() not in cols]
        if missing:
            out.append(
                f"{rule.schema_path} 의 {table} 에 시드 열이 없음: {', '.join(missing)} — 열 이름은 계약 필드 이름의 "
                f"{'snake_case' if rule.column_style == 'snake' else '그대로'} (시드 데이터는 빌드 파이프라인이 이 열로 넣는다)"
            )
        if re.search(r"generated\s+always", cols.get("id", ""), re.I):
            out.append(
                f"{rule.schema_path} 의 {table}.id 가 GENERATED ALWAYS — 시드가 id 를 직접 넣으므로 GENERATED BY DEFAULT AS IDENTITY 로 둔다"
            )
    return out
