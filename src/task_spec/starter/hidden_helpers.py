"""숨은 테스트 도우미를 계약에서 만든다 (템플릿 manifest `hidden_helper`) — 숨은 테스트가 어긋날 수 없게 구성으로 막는다.

2026-11-07~08 알파 평가: 숨은 테스트가 계약·시드와 어긋나는 실패가 반복됐다 — 배정받지 않은 id 를 쓰고, 응답 본문을 계약과 다른 모양으로
파싱하고, `Duration.ofSeconds(REQUEST_TIMEOUT)` 처럼 하네스 상수를 잘못 감싸 컴파일이 안 됐다(재평가 1 의 컴파일 오류 대부분).
검사로 잡는 대신, 이 과제의 계약에서 코드가 Java 추상 클래스를 만들어 숨은 케이스가 상속하게 한다:
- 케이스별 데이터 범위 상수 (`allocate_rows` 가 나눈 그대로 — 시드에 반드시 있는 행)
- 계약 테이블 이름 상수 (오류 주입 정규식용)
- 계약 API 호출 함수 (경로·메서드 그대로) + 200 응답 본문을 계약 `returns` 모양(목록/객체)으로 읽는 함수
  — 쿼리 매개변수(`Endpoint.query`)가 있으면 그 칸을 계약 순서로 받는 오버로드 (null 이면 빼고, 값은 인코딩. 2026-11-13)
- `peak(expectedStatus, call)` — 조절값(동시 요청 수·버스트 수)과 시간 한도를 넣은 동시 요청
- 요청 헤더 값 상수 (`HEADER_<헤더>_OK`·`_DENIED`)와 `okHeaders()` — 계약 공개 테스트 예시에서 (starter/header_values.py)
- `num(v)` — JSON 숫자 → long
조절값 파일처럼 숨은 테스트 생성기는 이 파일을 쓰지 않는다 (gen_hidden 이 코드 렌더본으로 바꾼다).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from task_spec.schemas.build import (
    Endpoint,
    GeneratedFile,
    HiddenPlan,
    RowUse,
    SeedScalar,
    TaskContract,
)
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.header_values import PER_REQUEST, header_values
from task_spec.starter.seed_rows import allocate_rows, matches, public_free
from task_spec.starter.seed_sql import default_value, time_kind
from task_spec.starter.state_rule import next_states, sentence
from task_spec.starter.tables import contract_tables, spring_snake
from task_spec.starter.templates import is_java, is_python

_SEG = re.compile(r"\{([A-Za-z][A-Za-z0-9]*)\}")


def _camel(parts: list[str]) -> str:
    words = [w for p in parts for w in re.split(r"[^A-Za-z0-9]+", p) if w]
    return "".join(w[:1].upper() + w[1:] for w in words)


def method_name(e: Endpoint) -> str:
    """POST /api/transactions/{id}/status-change → postApiTransactionsIdStatusChange."""
    segs = [s for s in e.path.strip("/").split("/") if s]
    return (e.method or "call").lower() + _camel([_SEG.sub(r"\1", s) for s in segs])


def _path_expr(path: str) -> tuple[str, list[str]]:
    """'/orders/{id}/status' → ('"/orders/" + id + "/status"', ['id'])."""
    out: list[str] = []
    names: list[str] = []
    pos = 0
    for m in _SEG.finditer(path):
        if m.start() > pos:
            out.append('"' + path[pos : m.start()] + '"')
        out.append(m.group(1))
        names.append(m.group(1))
        pos = m.end()
    if pos < len(path):
        out.append('"' + path[pos:] + '"')
    return " + ".join(out) or '"/"', names


def _const(name: str) -> str:
    return spring_snake(name).upper()


def _body_kind(e: Endpoint) -> str | None:
    ok = e.success()
    if ok is None:
        return None
    if ok.returns:
        return "list" if ok.returns.endswith("[]") else "object"
    return "object" if ok.body else None


def public_rows(contract: TaskContract, plan: HiddenPlan) -> dict[str, list[int]]:
    """공개 테스트 몫 행 — 상수 이름(PUBLIC_<엔티티>_<처음 값>) → id 들 (seed_rows.public_free — fit_seed 가 몫을 채운다)."""
    names = {m.entity_id: m.name for m in contract.entities}
    out: dict[str, list[int]] = {}
    for eid, values, ids in public_free(contract, plan):
        if eid not in names or not ids:
            continue
        out[public_const(names[eid], values)] = ids
    return out


def public_const(entity_name: str, values: Mapping[str, SeedScalar]) -> str:
    """공개 몫 상수 이름 — PUBLIC_<엔티티>_<처음 값…> (처음 값은 그룹에 적힌 칸 순서대로)."""
    vals = "_".join(
        re.sub(r"[^A-Za-z0-9]+", "_", str(v)).strip("_").upper() for v in values.values()
    )
    return f"PUBLIC_{_const(entity_name)}" + (f"_{vals}" if vals else "")


def row_use_consts(
    contract: TaskContract, use: RowUse, plan: HiddenPlan | None = None
) -> list[str]:
    """공개 테스트 rows 한 항목 → 그 처음 값을 담는 그룹의 공개 몫 상수 이름들 (public_free 와 같이 같은 처음 값은 처음 본 그룹 이름).
    fit_seed 가 그룹을 보장하므로 보통 하나 이상 — 공개 테스트 입력의 '쓸 행'과 정적 검사가 같은 이름을 본다."""
    names = {m.entity_id: m.name for m in contract.entities}
    if use.entity_id not in names:
        return []
    out: list[str] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for s in contract.seed:
        if s.entity_id != use.entity_id:
            continue
        for g in sorted(s.groups, key=lambda x: x.first):
            key = tuple(sorted((k, str(v)) for k, v in g.values.items()))
            if key in seen:
                continue
            seen.add(key)
            if matches(g.values, use.values):
                out.append(public_const(names[use.entity_id], g.values))
    if plan is not None:  # 공개 몫 id 가 비면 도우미에 상수가 없다 (public_rows 와 같은 이름만)
        have = set(public_rows(contract, plan))
        out = [n for n in out if n in have]
    return out


def _value_const(v: object) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(v)).strip("_").upper()


def _state_lines(contract: TaskContract) -> list[str]:
    """처음 값별 다음 상태 — 계약 state 칸에서 (state_rule.next_states). 테스트가 전이를 짐작하지 않게."""
    steps = list(next_states(contract))
    if not steps:
        return []
    lines = [
        "",
        "    // ── 상태 전이 (계약 state 칸): 처음 값별로 허용되는 다음 값(NEXT_OK_)과 거절되는 다음 값(NEXT_BAD_) ──",
    ]
    for m in contract.entities:
        if m.state is not None:
            lines.append(f"    // {m.name}.{m.state.field} — {sentence(m.state)}")
    for name, v, ok, bad in steps:
        key = f"{_const(name)}_{_value_const(v)}"
        if ok is not None:
            lines.append(f'    protected static final String NEXT_OK_{key} = "{ok}";')
        if bad is not None:
            lines.append(f'    protected static final String NEXT_BAD_{key} = "{bad}";')
    return lines


def setting_const(key: str) -> str:
    """설정 키 → 상수 이름 조각 — 'marketplace.listing.page-size' → 'MARKETPLACE_LISTING_PAGE_SIZE'."""
    return re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").upper()


def setting_probe(default: int) -> int:
    """테스트가 설정을 덮을 값 — 기본값과 다르고 작은 값 (시드에 그 조건의 행이 이보다 많으면 '정확히 이만큼'을 가른다, #161).
    값을 코드에 박은 구현·제한이 없는 구현이 둘 다 다른 개수를 돌려준다."""
    return 3 if default != 3 else 2


def _setting_lines(contract: TaskContract) -> list[str]:
    """계약 settings — 키·기본값·덮을 값. 테스트는 자기 @DynamicPropertySource 에서 KEY 를 PROBE 로 덮고 결과가 PROBE 를 따르는지 본다."""
    if not contract.settings:
        return []
    lines = [
        "",
        "    // ── 설정 (계약 settings): 구현이 이 키에서 읽어야 하는 값. 그 요구를 보는 테스트는 자기 클래스에",
        "    //    @DynamicPropertySource static void …(DynamicPropertyRegistry r) { r.add(SETTING_…_KEY, () -> SETTING_…_PROBE); }",
        "    //    로 덮고 결과가 PROBE 를 정확히 따르는지 단언한다 (기본값을 단언하면 값을 코드에 박은 구현도 통과한다) ──",
    ]
    for st in contract.settings:
        c = setting_const(st.key)
        lines += [
            f"    /** {st.meaning} (기본 {st.default}) */",
            f'    protected static final String SETTING_{c}_KEY = "{st.key}";',
            f"    protected static final int SETTING_{c}_DEFAULT = {st.default};",
            f"    protected static final int SETTING_{c}_PROBE = {setting_probe(st.default)};",
        ]
    return lines


_PATH_PARAM = re.compile(r"\{(\w+)\}")


def key_fields(contract: TaskContract) -> list[tuple[str, str, bool]]:
    """경로로 한 행을 가리키는데 시드가 값을 정하지 않아 코드 기본값(행 번호에서)이 들어가는 필드 — (필드, 앞붙이, 숫자인가).
    숫자면 그 행 번호, 글자면 `필드이름-행번호`(20자까지) — seed_sql.default_value 와 같다.

    2026-11-09 v16-be2: 시드는 memberId='memberId-191' 인데 숨은·공개 테스트가 "member-" + HC2_FIRST 로 짐작해 모두 대상 없음.
    테스트는 행 번호만 받았다 — 키 값을 만드는 함수를 도우미가 준다 (헤더 상수와 같은 방식)."""
    params = {p for e in contract.endpoints for p in _PATH_PARAM.findall(e.path)} - {"id"}
    seeded = {s.entity_id: s for s in contract.seed if s.rows > 0}
    out: list[tuple[str, str, bool]] = []
    for m in contract.entities:
        s = seeded.get(m.entity_id)
        if s is None:
            continue
        for f in m.fields:
            if f.name not in params or any(f.name in g.values for g in s.groups):
                continue
            a, b = default_value(f, 7), default_value(f, 8)
            if isinstance(a, int) and not isinstance(a, bool) and (a, b) == (7, 8):
                out.append((f.name, "", True))
            elif a == f"{f.name}-7"[:20] and b == f"{f.name}-8"[:20]:
                out.append((f.name, f"{f.name}-", False))
    return list(dict.fromkeys(out))


def time_values(contract: TaskContract, plan: HiddenPlan) -> dict[str, dict[int, str]]:
    """공개 몫 행의 날짜·시각 칸 실제 값 — 함수 이름(<엔티티><칸>Of) → 행 → 시드에 들어간 값 (그룹 값, 없으면 seed_sql 기본값).
    2026-11-13 #164 N+1 알파: 공개 테스트가 시각을 그룹 값으로 짐작해 '최신순 100건 안에 05-09 행이 있다'(나올 수 없음)·
    '같은 시각 행 중 이 행이 앞'(정해지지 않음)을 단언했다 — 기준 시각·순서를 실제 값으로 정하게 도우미가 값을 준다."""
    names = {m.entity_id: m for m in contract.entities}
    seeds = {x.entity_id: x for x in contract.seed if x.rows > 0}
    rows_by_entity: dict[str, list[int]] = {}
    for eid, _, ids in public_free(contract, plan):
        rows_by_entity.setdefault(eid, []).extend(ids)
    out: dict[str, dict[int, str]] = {}
    for eid, ids in rows_by_entity.items():
        m, seed = names.get(eid), seeds.get(eid)
        if m is None or seed is None or not ids:
            continue
        for f in m.fields:
            if time_kind(f) is None:
                continue
            fn = f"{m.name[:1].lower()}{m.name[1:]}{f.name[:1].upper()}{f.name[1:]}Of"
            values: dict[int, str] = {}
            for row in sorted(set(ids)):
                g = next((g for g in seed.groups if g.first <= row <= g.last), None)
                v = (
                    g.values[f.name]
                    if g is not None and f.name in g.values
                    else default_value(f, row)
                )
                values[row] = str(v)
            out[fn] = values
    return out


def _time_lines(contract: TaskContract, plan: HiddenPlan) -> list[str]:
    vals = time_values(contract, plan)
    if not vals:
        return []
    lines = [
        "",
        "    // ── 공개 몫 행의 날짜·시각 실제 값 (시드에 들어간 그대로): 기준 시각·순서·포함 여부는 이 값으로 정한다 — 지어 쓰거나 그룹 값으로 짐작하지 않는다 ──",
    ]
    for fn, values in vals.items():
        entries = ", ".join(f'java.util.Map.entry({row}L, "{v}")' for row, v in values.items())
        lines += [
            f"    protected static String {fn}(long row) {{",
            f"        return java.util.Map.ofEntries({entries}).get(row);",
            "    }",
        ]
    return lines


def _key_lines(contract: TaskContract) -> list[str]:
    keys = key_fields(contract)
    if not keys:
        return []
    lines = [
        "",
        "    // ── 경로 키 값: 시드 행 번호(HC…_FIRST·PUBLIC_…) → 그 행의 경로 값. 키 값을 직접 만들지 않는다 ──",
    ]
    for name, prefix, numeric in keys:
        if numeric:
            lines.append(f"    protected static long {name}Of(long row) {{ return row; }}")
        else:
            lines += [
                f"    protected static String {name}Of(long row) {{",
                f'        String v = "{prefix}" + row;',
                "        return v.length() > 20 ? v.substring(0, 20) : v;",
                "    }",
            ]
    return lines


def _header_lines(contract: TaskContract) -> list[str]:
    """요청 헤더 값 — 테스트가 요청자 등을 지어내지 않게 (2026-11-08 알파: 지어낸 `grader` 로 모든 요청이 403).
    값을 정할 수 없는 헤더는 상수를 두지 않는다 (지어내지 않는다)."""
    values = header_values(contract)
    if not values:
        return []
    lines = [
        "",
        "    // ── 요청 헤더 값: 규칙(권한)을 통과하는 값(_OK)과 권한이 거절되는 값(_DENIED) — 값을 직접 쓰지 않는다 ──",
    ]
    keyed = [h for h in values if h.source == PER_REQUEST]
    for h in values:
        if h.source == PER_REQUEST:
            lines.append(
                f"    // {h.name}: 요청마다 새 값 — okHeaders() 가 만든다, 재요청은 okHeaders(key) 로 같은 값"
            )
            continue
        key = _value_const(h.name)
        src = {
            "예시": "계약 공개 테스트 예시",
            "권한 규칙": "계약 권한 규칙의 요청자 행",
        }.get(h.source, "계약상 아무 값이나 받음")
        if h.ok is not None:
            lines.append(f'    protected static final String HEADER_{key}_OK = "{h.ok}"; // {src}')
        if h.denied is not None:
            lines.append(
                f'    protected static final String HEADER_{key}_DENIED = "{h.denied}"; // {src}'
            )
    oks = [h for h in values if h.ok is not None]
    if keyed:
        pairs = ", ".join(
            [
                *(f'"{h.name}", key' for h in keyed),
                *(f'"{h.name}", HEADER_{_value_const(h.name)}_OK' for h in oks),
            ]
        )
        lines += [
            "",
            "    /** 요청마다 다른 요청 키 — 새 요청은 이 값 하나로 */",
            "    protected static String newRequestId() {",
            "        return java.util.UUID.randomUUID().toString();",
            "    }",
            "",
            "    /** 규칙(권한)을 통과하는 헤더 묶음 — 부를 때마다 새 요청 키 (서로 다른 요청). 기능 단계에 닿아야 하는 요청에 쓴다 */",
            "    protected static Map<String, String> okHeaders() {",
            "        return okHeaders(newRequestId());",
            "    }",
            "",
            "    /** 같은 요청을 다시 보낼 때 — 처음 요청의 키(newRequestId())를 그대로 넘긴다 */",
            "    protected static Map<String, String> okHeaders(String key) {",
            f"        return Map.of({pairs});",
            "    }",
        ]
    elif oks:
        pairs = ", ".join(f'"{h.name}", HEADER_{_value_const(h.name)}_OK' for h in oks)
        lines += [
            "",
            "    /** 규칙(권한)을 통과하는 헤더 묶음 — 기능 단계(저장·기록)에 닿아야 하는 요청에 쓴다 */",
            "    protected static Map<String, String> okHeaders() {",
            f"        return Map.of({pairs});",
            "    }",
        ]
    return lines


def _inject_lines() -> list[str]:
    """오류 주입 함수 — 테이블 이름(TABLE_…)과 SQL 종류만 받아 정규식은 코드가 쓴다. 2026-11-08 상류 평가: 롤백 사유 케이스의 첫 답에
    주입이 빠지거나(8/9) 정규식을 손으로 써 테이블·SQL 종류를 넓게 잡던 일(기록 INSERT 까지 막음)을 줄인다."""
    return [
        "",
        '    /** 저장 단계 실패 주입 — verb("update"·"insert"·"delete")로 table(TABLE_…)을 바꾸는 SQL 을 실행한 연결의 다음 commit 을',
        "     *  실패시킨다. 규칙을 통과하는 요청(NEXT_OK_…)에 쓴다 — 거절되는 요청은 저장 전에 끝나 롤백될 변경이 없다.",
        "     *  케이스가 끝나면 finally 에서 GraderJdbcHarness.reset(). */",
        "    protected static void injectCommitFailure(String verb, String table) {",
        "        GraderJdbcHarness.failCommitAfter = sqlOn(verb, table);",
        "    }",
        "",
        "    /** SQL 실행 실패 주입 — verb 로 table 을 바꾸는 SQL 자체를 실패시킨다. 끝나면 finally 에서 GraderJdbcHarness.reset(). */",
        "    protected static void injectStatementFailure(String verb, String table) {",
        "        GraderJdbcHarness.failStatements = sqlOn(verb, table);",
        "    }",
        "",
        "    private static Pattern sqlOn(String verb, String table) {",
        '        return Pattern.compile("^\\\\s*" + verb + "\\\\s+(?:into\\\\s+|from\\\\s+)?" + Pattern.quote(table) + "\\\\b", Pattern.CASE_INSENSITIVE);',
        "    }",
    ]


def api_functions(contract: TaskContract) -> list[tuple[Endpoint, str]]:
    """도우미의 계약 API 호출 함수 — (엔드포인트, 함수 이름). 이름이 겹치면 엔드포인트 id 를 붙인다 (정적 검사가 같은 이름을 센다)."""
    seen: set[str] = set()
    out: list[tuple[Endpoint, str]] = []
    for e in contract.endpoints:
        if not e.method:
            continue
        name = method_name(e)
        if name in seen:
            name += e.endpoint_id.upper()
        seen.add(name)
        out.append((e, name))
    return out


_JAVA_KEYWORDS = frozenset(
    "abstract assert boolean break byte case catch char class const continue default do double else enum extends "
    "final finally float for goto if implements import instanceof int interface long native new package private "
    "protected public return short static strictfp super switch synchronized this throw throws transient try void "
    "volatile while var record yield true false null".split()
)


def _java_param(name: str, taken: set[str]) -> str:
    """계약 칸 이름 → Java 매개변수 이름 — 이름 그대로가 원칙, 식별자가 아니거나 예약어·경로 변수와 겹치면 q 를 붙인다."""
    ident = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else _camel([name])
    if not ident or ident in _JAVA_KEYWORDS or ident in taken:
        ident = "q" + _camel([name])
    taken.add(ident)
    return ident


def _api_fn(e: Endpoint, name: str, expr: str, vars_: list[str], query: bool = False) -> list[str]:
    """계약 API 호출 함수 하나 — query=True 면 쿼리 매개변수를 계약 순서대로 받는 오버로드 (null 이면 그 칸을 보내지 않는다).

    2026-11-12 행마다 조회 알파: 도우미가 경로 변수만 받아, 숨은 테스트가 `send("GET", "/orders?size=100", …)` 를 손으로 쓰다
    기본 크기 호출(`getOrders()`)과 섞었다 — 시간을 재는 요청 안에서 목록을 두 번 부르고, 기본 50건 목록에 100건을 기대해 허용 구현까지 떨어짐.
    값은 Object 로 받는다 (경로 변수와 같게 — `Long` 칸에 정수 글자 100 을 넘기면 컴파일되지 않는다)."""
    method = (e.method or "GET").upper()
    params = [f"Object {v}" for v in vars_]
    target = expr
    doc = f"    /** {e.endpoint_id} {method} {e.path} — {e.summary} */"
    if query:
        taken = set(vars_) | {"headers", "jsonBody"}
        pairs: list[str] = []
        for f in e.query:
            p = _java_param(f.name, taken)
            params.append(f"Object {p}")
            pairs += [f'"{f.name}"', p]
        target = f"{expr} + queryOf({', '.join(pairs)})"
        shown = ", ".join(f"{f.name}({f.type})" for f in e.query)
        doc = (
            f"    /** {e.endpoint_id} {method} {e.path} — 쿼리 매개변수 {shown} 를 이 순서로 (null 이면 보내지 않음, 값은 인코딩). "
            "경로·쿼리 문자열을 손으로 쓰지 않는다 */"
        )
    has_body = method not in ("GET", "DELETE")
    if e.headers or has_body:
        params.append("Map<String, String> headers")
        if has_body:
            params.append("String jsonBody")
        call = f'send("{method}", {target}, headers, {"jsonBody" if has_body else "null"})'
    else:
        call = f"get({target})"
    return [
        doc,
        f"    protected HttpResponse<String> {name}({', '.join(params)}) throws Exception {{",
        f"        return {call};",
        "    }",
    ]


def _query_lines() -> list[str]:
    return [
        "",
        "    /** 쿼리 문자열 — (계약 칸 이름, 값) 쌍. 값이 null 이면 그 칸을 빼고, 이름·값은 UTF-8 로 인코딩한다 (+·: 등이 깨지지 않게) */",
        "    private static String queryOf(Object... nameValues) {",
        "        StringBuilder sb = new StringBuilder();",
        "        for (int i = 0; i + 1 < nameValues.length; i += 2) {",
        "            if (nameValues[i + 1] == null) {",
        "                continue;",
        "            }",
        "            sb.append(sb.length() == 0 ? '?' : '&')",
        "                    .append(URLEncoder.encode(String.valueOf(nameValues[i]), StandardCharsets.UTF_8))",
        "                    .append('=')",
        "                    .append(URLEncoder.encode(String.valueOf(nameValues[i + 1]), StandardCharsets.UTF_8));",
        "        }",
        "        return sb.toString();",
        "    }",
    ]


def render_contract_case(
    contract: TaskContract,
    plan: HiddenPlan,
    manifest: TemplateManifest,
    audience: Literal["hidden", "public"] = "hidden",
) -> GeneratedFile | None:
    """audience=hidden: 숨은 케이스 도우미(ContractCase — 케이스 범위·테이블·peak). public: 공개 테스트 도우미(PublicCase —
    공개 테스트 몫 행, 동시 요청·오류 주입 없음 — 공개 테스트는 숨은 요구를 시험하지 않는다)."""
    if is_python(manifest):  # 데이터 템플릿 — contract.py (starter/py_helpers.py)
        from task_spec.starter.py_helpers import render_py_contract

        return render_py_contract(contract, manifest, audience)
    if not is_java(manifest):  # TypeScript 템플릿 — contract.ts (starter/ts_helpers.py)
        from task_spec.starter.ts_helpers import render_ts_contract

        return render_ts_contract(contract, manifest, audience, plan)
    rule = manifest.hidden_helper if audience == "hidden" else manifest.public_helper
    if rule is None:
        return None
    names = {m.entity_id: m.name for m in contract.entities}
    with_query = any(e.method and e.query for e in contract.endpoints)
    lines = [
        f"package {rule.package};",
        "",
        "import com.example.service.grading.Burst;",
        "import com.example.service.grading.GraderParams;",
        "import com.example.service.grading.GraderSupport;",
        "import com.fasterxml.jackson.core.type.TypeReference;",
        *(["import java.net.URLEncoder;"] if with_query else []),
        "import java.net.http.HttpResponse;",
        *(["import java.nio.charset.StandardCharsets;"] if with_query else []),
        "import java.util.List;",
        "import java.util.Map;",
        "import java.util.function.IntUnaryOperator;",
        *(
            [
                "import com.example.service.grading.GraderJdbcHarness;",
                "import java.util.regex.Pattern;",
            ]
            if audience == "hidden"
            else []
        ),
        "",
        "/**",
        f" * 이 과제의 공개 계약에서 빌드 파이프라인이 만든 도우미 — 손으로 고치지 않는다. {'숨은 케이스' if audience == 'hidden' else '공개 테스트'}는 이 클래스를 상속한다.",
        (
            " * id·경로·응답 파싱·동시 요청 시간 한도를 직접 쓰지 말고 아래 상수·함수를 쓴다."
            if audience == "hidden"
            else " * id·경로·응답 파싱을 직접 쓰지 말고 아래 상수·함수를 쓴다."
        ),
        " */",
        f"abstract class {rule.class_name} extends GraderSupport {{",
        "",
    ]
    if audience == "public":
        lines += [
            "    // ── 공개 테스트 몫 행: 계약 시드에 반드시 있고 처음 값이 이름대로, 숨은 케이스·지원자 테스트와 겹치지 않는다 ──",
            "    // 테스트 메서드마다 서로 다른 id 를 쓴다 (테스트끼리 같은 DB 를 함께 쓰고 순서는 정해지지 않음)",
        ]
        lines += [
            f"    protected static final long[] {k} = {{{', '.join(f'{i}L' for i in ids)}}};"
            for k, ids in public_rows(contract, plan).items()
        ]
    else:
        lines += [
            "    // ── 케이스별 데이터 범위: 계약 시드에 반드시 있는 행, 다른 케이스·지원자 테스트(작은 id)와 겹치지 않는다 ──"
        ]
    ranges = allocate_rows(contract, plan).ranges if audience == "hidden" else []
    per_case: dict[str, list] = {}
    for r in ranges:
        per_case.setdefault(r.case_id, []).append(r)
    for case_id, rs in per_case.items():
        for r in rs:
            prefix = (
                case_id.upper()
                if len(rs) == 1
                else f"{case_id.upper()}_{_const(names.get(r.entity_id, r.entity_id))}"
            )
            init = ", ".join(f"{k}={v}" for k, v in r.initial.items())
            lines += [
                f"    /** {case_id.upper()} {names.get(r.entity_id, r.entity_id)} id {r.first}~{r.last} ({r.rows}행"
                + (f", 처음 {init}" if init else "")
                + (", 버스트 — 요청마다 FIRST + index" if r.burst else "")
                + ") */",
                f"    protected static final long {prefix}_FIRST = {r.first}L;",
                f"    protected static final long {prefix}_LAST = {r.last}L;",
                f"    protected static final int {prefix}_ROWS = {r.rows};",
            ]
    lines += _state_lines(contract)
    lines += _setting_lines(contract)
    lines += _header_lines(contract)
    lines += _key_lines(contract)
    if audience == "public":
        lines += _time_lines(contract, plan)
    tables = contract_tables(contract, manifest.naming.table) if audience == "hidden" else {}
    if tables:
        lines += ["", "    // ── 계약 테이블 이름 (오류 주입 정규식에는 이 이름을 그대로) ──"]
        lines += [
            f'    protected static final String TABLE_{_const(names[eid])} = "{t}";'
            for eid, t in tables.items()
        ]
    lines += ["", "    // ── 계약 API — 경로·메서드는 계약 그대로 ──"]
    for e, name in api_functions(contract):
        expr, vars_ = _path_expr(e.path)
        lines += _api_fn(e, name, expr, vars_)
        if e.query:  # 쿼리 매개변수 오버로드 — 계약 칸 이름 그대로, 계약 순서 (2026-11-13)
            lines += _api_fn(e, name, expr, vars_, query=True)
        kind = _body_kind(e)
        if kind == "list":
            lines += [
                f"    /** {e.endpoint_id} 의 200 본문 — 계약상 목록 */",
                f"    protected List<Map<String, Object>> {name}Body(HttpResponse<String> response) throws Exception {{",
                "        return json(response.body(), new TypeReference<List<Map<String, Object>>>() {});",
                "    }",
            ]
        elif kind == "object":
            lines += [
                f"    /** {e.endpoint_id} 의 200 본문 — 계약상 객체 하나 */",
                f"    protected Map<String, Object> {name}Body(HttpResponse<String> response) throws Exception {{",
                "        return jsonObject(response.body());",
                "    }",
            ]
    burst = manifest.burst_rows if audience == "hidden" else []
    if burst:
        c_name, r_name = burst[0], (burst[1] if len(burst) > 1 else None)
        c_raw = plan.harness.get(c_name, 1)
        c_default = c_raw if isinstance(c_raw, int) else 1
        r_default = plan.harness.get(r_name, 1) if r_name else 1
        rounds = (
            f'GraderParams.intValue("{r_name}", {int(r_default) if isinstance(r_default, int) else 1})'
            if r_name
            else "1"
        )
        lines += [
            "",
            "    /** 운영 피크 재현 — 동시 요청 수만큼씩 버스트 수만큼 차례로, 시간 한도 REQUEST_TIMEOUT 까지. index 는 0 ~ 전체-1. */",
            "    protected List<Burst.Outcome> peak(IntUnaryOperator expectedStatus, Burst.Call call) throws Exception {",
            f'        return Burst.rounds(GraderParams.intValue("{c_name}", {c_default}), {rounds}, expectedStatus, call, REQUEST_TIMEOUT);',
            "    }",
        ]
    if audience == "hidden":
        lines += _inject_lines()
    if with_query:
        lines += _query_lines()
    lines += [
        "",
        "    /** JSON 숫자 → long (크기에 따라 Integer·Long 으로 읽혀 그대로 비교하면 틀린다) */",
        "    protected static long num(Object value) {",
        "        return ((Number) value).longValue();",
        "    }",
        "}",
        "",
    ]
    return GeneratedFile(path=rule.path, content="\n".join(lines))


def persistence_check_path(public_helper_path: str) -> str:
    """저장 확인 테스트 경로 — 공개 도우미 옆 (Java)."""
    return public_helper_path.rsplit("/", 1)[0] + "/PersistenceCheckTest.java"


def code_written_paths(m: TemplateManifest) -> set[str]:
    """코드가 계약·계획에서 결정적으로 쓰는 파일 — 시드 SQL·조절값 파일·계약 도우미·저장 확인. 생성기가 고칠 수 없으므로 LLM 출력
    크기 상한(static_check)·생성기 컴파일 오류 배정(build)에서 뺀다 (#178: 크기 상한이 시드 파일 하나만 빼, 코드가 렌더한 조절값·도우미·
    저장 확인 파일이 커지면 생성기가 고칠 수 없는 문제로 걸렸다). 매니페스트로 정해진다 — 계약에 따라 없는 파일도 있다."""
    paths: set[str] = set()
    if m.seed_sql is not None:
        paths.add(m.seed_sql.path)
    if m.hidden_layout is not None:
        paths.add(m.hidden_layout.params)
    paths |= {h.path for h in (m.hidden_helper, m.public_helper) if h is not None}
    if m.public_helper is not None and is_java(m):
        paths.add(persistence_check_path(m.public_helper.path))
    return paths


def render_persistence_check(
    contract: TaskContract, manifest: TemplateManifest
) -> GeneratedFile | None:
    """새로 만드는 엔티티를 앱이 저장할 수 있는가 — 공개 테스트 자리에 코드가 쓰는 저장 확인 (LLM 없음).

    2026-11-08 알파 4케이스: 시작 코드의 새 감사 기록 엔티티에 id 생성 전략이 없어(`@Id` 만) 참조 구현 둘 다 기록을 저장하는 순간
    'must be manually assigned before persist()' 로 실패했다. 시작 코드는 기록을 쓰지 않으니 자기 테스트로는 드러나지 않는다.
    JPA 메타모델에서 계약 엔티티 이름으로 클래스를 찾아(클래스 이름을 미리 몰라도 됨) 필수 필드를 채워 저장해 본다.
    대상: 새로 만드는 엔티티(existing 아님) 중 처음 0행 — 옛 계약은 existing 이 모두 기본값 false 라 처음 0행 전부 (#184: 따로 두던
    옛 계약 갈래는 같은 결과라 지웠다).
    """
    rule = manifest.public_helper
    if rule is None or not is_java(manifest):  # JPA 메타모델로 저장해 보는 Java 전용 확인
        return None
    rows = {s.entity_id: s.rows for s in contract.seed}
    targets = [
        m.name for m in contract.entities if rows.get(m.entity_id, 0) == 0 and not m.existing
    ]
    if not targets:
        return None
    path = persistence_check_path(rule.path)
    names = ", ".join(f'"{t}"' for t in targets)
    src = f"""package {rule.package};

import static org.assertj.core.api.Assertions.assertThat;

import jakarta.persistence.EntityManager;
import jakarta.persistence.PersistenceContext;
import jakarta.persistence.metamodel.EntityType;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

/** 빌드 파이프라인이 계약에서 만든 저장 확인 — 새로 만드는 엔티티는 앱이 저장할 수 있어야 한다 (손으로 고치지 않는다). */
@DisplayName("저장 확인 — 새로 만드는 엔티티")
class PersistenceCheckTest extends {rule.class_name} {{

    @PersistenceContext
    EntityManager em;

    @Autowired
    PlatformTransactionManager tx;

    @Test
    void 새로_만드는_엔티티를_저장하면_id_가_생긴다() throws Exception {{
        for (String name : new String[] {{{names}}}) {{
            EntityType<?> type = em.getMetamodel().getEntities().stream()
                    .filter(e -> e.getName().equals(name) || e.getJavaType().getSimpleName().equals(name))
                    .findFirst()
                    .orElse(null);
            assertThat(type).as("계약 엔티티 %s 의 JPA 엔티티 클래스 (클래스 이름은 계약 엔티티 이름)", name).isNotNull();
            Object row = newRow(type.getJavaType());
            new TransactionTemplate(tx).executeWithoutResult(status -> em.persist(row));
            Object id = em.getEntityManagerFactory().getPersistenceUnitUtil().getIdentifier(row);
            assertThat(id).as("%s 를 저장하면 id 가 생긴다 — 앱이 행을 만드는 엔티티라 id 생성 전략이 필요", name).isNotNull();
        }}
    }}

    private static Object newRow(Class<?> type) throws Exception {{
        var ctor = type.getDeclaredConstructor();
        ctor.setAccessible(true);
        Object row = ctor.newInstance();
        for (Class<?> c = type; c != null && c != Object.class; c = c.getSuperclass()) {{
            for (Field f : c.getDeclaredFields()) {{
                if (Modifier.isStatic(f.getModifiers()) || f.isAnnotationPresent(jakarta.persistence.Id.class)) {{
                    continue;
                }}
                Object value = sample(f.getType());
                if (value != null) {{
                    f.setAccessible(true);
                    f.set(row, value);
                }}
            }}
        }}
        return row;
    }}

    private static Object sample(Class<?> t) {{
        if (t == String.class) return "x";
        if (t == Long.class || t == long.class) return 1L;
        if (t == Integer.class || t == int.class) return 1;
        if (t == Short.class || t == short.class) return (short) 1;
        if (t == Boolean.class || t == boolean.class) return false;
        if (t == Double.class || t == double.class) return 1.0;
        if (t == BigDecimal.class) return BigDecimal.ONE;
        if (t == LocalDateTime.class) return LocalDateTime.now();
        if (t == Instant.class) return Instant.now();
        if (t == OffsetDateTime.class) return OffsetDateTime.now();
        if (t == LocalDate.class) return LocalDate.now();
        if (t.isEnum() && t.getEnumConstants().length > 0) return t.getEnumConstants()[0];
        return null;
    }}
}}
"""
    return GeneratedFile(path=path, content=src)
