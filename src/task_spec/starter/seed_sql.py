"""계약 시드 → 시드 SQL (템플릿 manifest `seed_sql`) — README·조절값 파일처럼 코드가 쓴다. LLM 은 시드의 **규칙**(행 수·id 범위별 처음 값)만 쓴다.

2026-11-07 알파 3×3 평가: 시작 코드 생성기가 수백 행 data.sql 을 "-- id 81~360도 같은 방식으로" 같은 주석으로 생략해(3/7), 숨은 테스트가
배정받은 큰 id 가 없어 시작 코드·흔한 구현·허용 구현이 모두 같은 404 로 떨어졌다. id 직접 입력과 `GENERATED ALWAYS` 가 부딪힌 H2 오류도 있었다.
여기서 막는 것:
- 행 생략 — 행은 코드가 펼친다 (계약 `seed.rows`·`groups` 그대로).
- id 문법 — id 는 코드가 넣고, identity 열이면 템플릿이 정한 재설정 문장으로 다음 값을 시드 끝 다음으로 (앱이 새 행을 넣어도 안 겹침).
- 스키마 불일치 — `schema_problems` 가 시작 코드 schema.sql 에 시드 테이블·열이 있는지, id 가 직접 넣을 수 있는 열인지 Gradle 전에 본다.
그룹에 없는 필수 열은 형식별 결정적 기본값, 그룹에 없는 선택 열은 넣지 않는다(DB 기본값·NULL).
날짜·시각 칸(형식, 또는 글자 형식이면 제약 글·…At 이름)의 기본값은 행마다 다르고 id 순서와 어긋난다 (2026-11-13, `time_value`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Literal

from task_spec.schemas.build import ContractField, Entity, GeneratedFile, SeedRule, TaskContract
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.field_types import non_null, parse
from task_spec.starter.tables import contract_tables, spring_snake

HEADER = "-- 시드 데이터 — 계약의 시드(행 수·id 범위별 처음 값)로 빌드 파이프라인이 쓴다. 손으로 고치지 않는다 (가상 데이터)."
FIXED_DATETIME = "2026-01-01T00:00:00"
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


_BASE_TIME = datetime.fromisoformat(FIXED_DATETIME)
# 문자열 칸이지만 날짜·시각을 담는 칸 — 제약 글로 알아본다 (보조: 계약이 형식을 datetime 으로 적는 것이 원칙, plan_contract 몫)
_TIME_WORDS = re.compile(r"\bhh\b|시간|\btime\b|'?t'?hh|\dT\d|yyyy-mm-dd'?t", re.I)
_DATE_WORDS = re.compile(r"yyyy-mm-dd", re.I)
_DATETIME_WORDS = re.compile(
    r"iso[\s-]?8601|date[\s-]?time|datetime|timestamp|날짜[\s·-]?시간", re.I
)
_AT_NAME = re.compile(r"[a-z0-9]At$")
TimeKind = Literal["datetime", "date", "time"]


def time_kind(f: ContractField) -> TimeKind | None:
    """날짜·시각 칸인가 — 형식(datetime·date·time…)이 먼저, 형식이 글자면 제약 글(ISO 8601·YYYY-MM-DD·날짜-시간)과 이름(…At)으로.
    2026-11-12 행마다 조회 알파: `createdAt`(string, 제약 'ISO 8601 날짜-시간 문자열')에 시드 값이 없어 'createdAt-<id>' 가 들어가
    '최신순'이 뜻을 잃었다 (풀이 6개 모두 진행자에게 물음)."""
    ft = non_null(
        parse(f.type)
    )  # 형식 판정은 계약 형식 해석 한 곳 (#172 — 'date-time' 을 날짜로, 'localdate' 를 글자로 읽었다)
    if ft.kind in ("datetime", "date", "time"):
        return ft.kind  # type: ignore[return-value]
    if ft.kind not in ("string", "unknown"):
        return None
    text = f.constraints or ""
    if _DATE_WORDS.search(text):  # YYYY-MM-DD — 시각 표기가 함께면 날짜-시각
        return "datetime" if _TIME_WORDS.search(text) else "date"
    if _DATETIME_WORDS.search(text) or _AT_NAME.search(f.name):
        return "datetime"
    return None


def _spread(row: int, step: int, mod: int) -> int:
    """행 번호 → 서로 다른 칸 수 — mod 가 소수라 행 1~mod-1 은 모두 다르고, 곱수 때문에 행 번호 순서와 어긋난다
    (6007·10007: 행 1·2·3 → 6007·2007·8014). mod 를 넘는 행은 mod 씩 더해 앞 행과 겹치지 않는다."""
    return (row * step) % mod + (row // mod) * mod


def time_value(kind: TimeKind, row: int) -> str:
    """행마다 다른 결정적 날짜·시각 — **id 순서와 어긋나게** (id 로만 정렬한 구현이 '최신순'을 통과하지 않게, 2026-11-13 리뷰).
    같은 시각이 필요한 과제(같은 시각의 순서 정하기 등)는 계약 시드 groups 가 그 값을 적는다 — 기본값은 '모두 다름'만 보장한다."""
    if kind == "date":
        return (_BASE_TIME + timedelta(days=_spread(row, 211, 367))).date().isoformat()
    minutes = _spread(row, 6007, 10007)  # 약 7일 안
    if kind == "time":
        return f"{minutes % 1440 // 60:02d}:{minutes % 60:02d}:00"
    return (_BASE_TIME + timedelta(minutes=minutes)).isoformat(timespec="seconds")


# 글자 기본값 길이 상한 — 도우미(hidden_helpers 의 <칸>Of·loginBody)가 같은 규칙을 쓴다
TEXT_MAX = 20


def text_default(name: str, row: int) -> str:
    """글자 칸 기본값 `<칸>-<행>` (20자 안). 넘치면 칸 이름 쪽을 잘라 행 번호를 남긴다 — 행마다 다르게.
    2026-11-25 #308 팩트체크: 끝을 20자로 자르면 이름이 긴 칸은 행 번호가 잘려 겹쳤다 (18자 이름 300행 중 291개, 21자 이름 전부).
    20자 안의 값은 예전과 같다."""
    suffix = f"-{row}"
    return name[: max(0, TEXT_MAX - len(suffix))] + suffix


def default_value(f: ContractField, row: int) -> object:
    """그룹 값이 없는 필수 필드의 결정적 기본값 (행 id 로 서로 다르게)."""
    ft = non_null(parse(f.type))
    if ft.kind == "enum" and ft.values:
        return ft.values[0]
    if ft.kind in ("integer", "number"):
        return row
    if ft.kind == "boolean":
        return False
    if (kind := time_kind(f)) is not None:
        return time_value(kind, row)
    return text_default(f.name, row)


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


def declared_name(cols: dict[str, str], name: str) -> str:
    """스키마가 그 열을 따옴표로 선언했으면 그 표기 그대로 (`"rank"`), 아니면 이름 그대로.
    2026-11-09 알파 (2df170e2): 시작 코드가 `"rank"` 로 선언(엔티티도 같은 표기)했는데 시드가 따옴표 없이 넣어 H2 가 대문자 RANK 를
    찾아 기동 실패 — 시작 코드를 다시 만들어도 시드는 코드가 쓰므로 같은 오류가 되풀이됐다."""
    first = cols.get(name.lower(), "").split()
    return first[0] if first and first[0][0] in '`"' else name


def _seeded(
    contract: TaskContract, manifest: TemplateManifest
) -> Iterator[tuple[Entity, SeedRule, str]]:
    tables = contract_tables(contract, manifest.naming.table)
    ents = {m.entity_id: m for m in contract.entities}
    for s in _fk_order(contract):
        m, table = ents.get(s.entity_id), tables.get(s.entity_id)
        if m is not None and table is not None:
            yield m, s, table


def _fk_order(contract: TaskContract) -> list[SeedRule]:
    """외래 키 대상 엔티티를 먼저 — 2026-11-10 목록 재료: 시드 순서(Order → Customer)대로 넣어 첫 INSERT 부터
    'Referential integrity constraint violation' 으로 시작 코드가 기동하지 못했다 (값은 대상 범위 안이었다).
    순환이 있으면 남은 것은 계약 순서 그대로."""
    by_name = {m.name: m.entity_id for m in contract.entities}
    deps = {
        m.entity_id: {by_name.get(target, target) for _, target in m.fk_pairs()} - {m.entity_id}
        for m in contract.entities
    }
    seeded = {s.entity_id for s in contract.seed}
    pending = list(contract.seed)
    out: list[SeedRule] = []
    while pending:
        done = {s.entity_id for s in out}
        s = next(
            (
                p
                for p in pending
                if all(d in done or d not in seeded for d in deps.get(p.entity_id, ()))
            ),
            None,
        )
        if s is None:  # 순환 — 남은 것은 계약 순서 그대로
            out += pending
            break
        out.append(s)
        pending.remove(s)
    return out


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
        block = table_block(schema_sql or "", table)
        declared = table_columns(block) if block else {}
        cols = [
            declared_name(declared, c)
            for c in ["id", *(column(f.name, rule.column_style) for f in fields)]
        ]
        groups = sorted(s.groups, key=lambda g: g.first)
        values: list[str] = []
        for row in range(1, s.rows + 1):
            g = next((x.values for x in groups if x.first <= row <= x.last), {})
            cells = [str(row), *(_literal(_value(f, row, dict(g), fk_rows)) for f in fields)]
            values.append("(" + ", ".join(cells) + ")")
        out.append(f"-- {m.name}: {s.rows}행 (id 1~{s.rows})")
        for part in _chunks(values, rule.chunk):
            out.append(f"INSERT INTO {table} ({', '.join(cols)}) VALUES\n" + ",\n".join(part) + ";")
        id_def = declared.get("id", "")
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
