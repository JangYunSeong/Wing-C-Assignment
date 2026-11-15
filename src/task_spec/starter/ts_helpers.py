"""TypeScript 계약 도우미 — 프론트엔드 템플릿의 숨은·공개 테스트가 import 하는 `contract.ts` 를 계약에서 코드가 쓴다 (LLM 없음).

Java 템플릿의 ContractCase·PublicCase(starter/hidden_helpers.py)와 같은 자리: 테스트가 test id·화면 경로·API 경로를 손으로 쓰지 않게
상수와 함수로 준다. 없는 이름을 쓰면 tsc 컴파일 루프가 잡는다 (build.compile_and_fix). 2026-11-09 React 템플릿 파이프라인 연결.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable
from typing import Literal

from task_spec.schemas.build import ContractResponse, GeneratedFile, HiddenPlan, TaskContract
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.field_types import FieldType
from task_spec.starter.field_types import parse as parse_field
from task_spec.starter.hidden_helpers import method_name
from task_spec.starter.tables import spring_snake

_SEG = re.compile(r"\{([A-Za-z][A-Za-z0-9]*)\}")


def msw_path(path: str) -> str:
    """계약 경로 → MSW 꼴: '/api/items/{id}' → '/api/items/:id' (':id' 는 그대로)."""
    return _SEG.sub(r":\1", path)


_ROUTE_VAR = re.compile(r":([A-Za-z][A-Za-z0-9]*)|\{([A-Za-z][A-Za-z0-9]*)\}")


def screen_route(route: str) -> str:
    """화면 경로 → TS 값. 경로 변수(`:id`·`{id}`)가 있으면 그 값을 받는 함수 — 변수를 채우지 않고 열면 tsc 가 잡는다.
    2026-11-09 React 알파(중복 제출): 공개 테스트가 '/products/:productId' 를 그대로 열어 요청 본문에 ':productId' 가 들어갔다."""
    names = list(dict.fromkeys(a or b for a, b in _ROUTE_VAR.findall(route)))
    if not names:
        return _ts(route)
    body = _ROUTE_VAR.sub(
        lambda m: "${encodeURIComponent(p." + (m.group(1) or m.group(2)) + ")}",
        route.replace("`", ""),
    )
    params = "; ".join(f"{n}: string" for n in names)
    return f"(p: {{ {params} }}) => `{body}`"


def _const(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper() or "X"


def _ts(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_ts_contract(
    contract: TaskContract,
    manifest: TemplateManifest,
    audience: Literal["hidden", "public"],
    plan: HiddenPlan | None = None,
) -> GeneratedFile | None:
    rule = manifest.hidden_helper if audience == "hidden" else manifest.public_helper
    if rule is None:
        return None
    harness = "../harness/grader" if audience == "hidden" else "../__grading__/harness/grader"
    who = "숨은 케이스" if audience == "hidden" else "공개 테스트"
    registry = (
        "contract_registry" in rule.features
    )  # React v8+ 하네스 — 계약의 성공 상태·test id 를 하네스에 알린다
    lines = [
        f"// 이 과제의 공개 계약에서 빌드 파이프라인이 만든 도우미 — 손으로 고치지 않는다. {who}가 import 한다.",
        "// test id·화면 경로·API 경로를 직접 쓰지 말고 아래 상수·함수를 쓴다 (없는 이름은 tsc 가 잡는다).",
        f"import {{ api, type Call, type CallHandle{', type TestId' if registry else ''} }} from {_ts(harness)};",
        "",
    ]
    tids: dict[str, str] = {}
    actions: dict[str, str] = {}
    for sc in contract.screens:
        for el in sc.elements:
            tids.setdefault(_const(el.test_id), el.test_id)
            if el.action:
                actions.setdefault(_const(el.test_id), el.action)
    lines.append(
        "/** 화면 요소 test id (계약 screens) — screen.getByTestId(TID.X) 또는 byTestId(TID.X) */"
    )
    lines.append("export const TID = {")
    lines += [
        f"  {k}: {_ts(v)}{' as TestId' if registry else ''},"
        + (f" // 조작: {actions[k]}" if k in actions else "")
        for k, v in tids.items()
    ]
    lines += ["} as const;", ""]
    lines += options_lines(
        contract,
        "선택은 `await user.selectOptions(el, OPTIONS.X[0])` — 값을 짐작하거나 options[0] 으로 고르지 않는다",
    )
    lines.append(
        "/** 화면 경로 (계약 screens) — renderApp(SCREEN.S1). 경로 변수가 있는 화면은 함수: SCREEN.S2({ id: '1' }) */"
    )
    lines.append("export const SCREEN = {")
    lines += [
        f"  {_const(sc.screen_id)}: {screen_route(sc.route)}, // {sc.summary}"
        for sc in contract.screens
    ]
    lines += ["} as const;", ""]
    endpoints = [e for e in contract.endpoints if e.method]
    lines.append("/** 계약 API — 메서드와 경로(MSW 꼴) */")
    lines.append("export const API = {")
    for e in endpoints:
        lines.append(
            f"  {spring_snake(method_name(e)).upper()}: {{ method: {_ts(e.method or '')}, path: {_ts(msw_path(e.path))} }}, // {e.endpoint_id} {e.summary}"
        )
    lines += ["} as const;", ""]
    if registry:
        ok_status = {
            f"{e.method} {msw_path(e.path)}": ok.status
            for e in endpoints
            if (ok := e.success()) is not None
        }
        lines += [
            "/** 계약을 하네스에 알린다 (import 할 때 한 번) — 상태를 주지 않은 응답(respond(본문)·always·once·collection)은 계약의 성공 상태로 나가고,",
            " *  textsByRole 이 TID 를 범위로 안다 */",
            "api.contract({",
            "  ok: {",
            *[f"    {_ts(k)}: {v}," for k, v in ok_status.items()],
            "  },",
            "  testIds: Object.values(TID),",
            *(
                ["  idKinds: {", *[f"    {_ts(k)}: {_ts(v)}," for k, v in kinds.items()], "  },"]
                if (kinds := id_kinds(contract)) and "contract_id_kinds" in rule.features
                else []
            ),
            "});",
            "",
        ]
    lines += [
        "/** 이 과제의 계약 API 를 모두 가로챈다 — 케이스 파일의 beforeEach 에서 (가로챈 요청은 응답할 때까지 붙잡힌다) */",
        "export function routeContract(): void {",
        *[f"  api.route({_ts(e.method or '')}, {_ts(msw_path(e.path))});" for e in endpoints],
        "}",
    ]
    names = {m.entity_id: _ident(m.name) for m in contract.data_shapes()}
    by_name = {_ident(m.name): _ident(m.name) for m in contract.data_shapes()} | {
        m.entity_id: _ident(m.name) for m in contract.data_shapes()
    }  # 필드 타입에 엔티티 id(m2[]) 를 써도 (스모크 예시와 같게)
    lines += ["", "/** 계약 데이터 모양 (entities) — 응답 본문의 타입 */"]
    for m in contract.data_shapes():  # 테이블 엔티티 + 응답 모양
        lines.append(f"export interface {names[m.entity_id]} {{")
        lines += [
            f"  {_prop(f.name)}{'' if f.required else '?'}: {ts_type(f.type, by_name)};"
            for f in m.fields
        ]
        lines.append("}")
    for e in endpoints:
        q = ", ".join(f.name for f in e.query)
        base = method_name(e)[:1].upper() + method_name(e)[1:]
        ok = e.success()
        errs = [r for r in e.responses if r.status >= 400 and (r.body or r.returns)]
        err_types = list(dict.fromkeys(_body_type(r, names, by_name) for r in errs))
        req_body = (
            "{ "
            + "; ".join(
                f"{_prop(f.name)}{'' if f.required else '?'}: {ts_type(f.type, by_name)}"
                for f in e.request
            )
            + " }"
            if e.request
            else "unknown"
        )
        path_vars = list(dict.fromkeys(a or b for a, b in _ROUTE_VAR.findall(e.path)))
        req_params = (
            "{ " + "; ".join(f"{v}: string" for v in path_vars) + " }"
            if path_vars
            else "Record<string, string>"
        )
        lines += [
            "",
            f"/** {e.endpoint_id} {e.method} {e.path} 성공 응답 본문 ({ok.status if ok else '-'}) */",
            f"export type {base}Ok = {_body_type(ok, names, by_name) if ok else 'unknown'};",
            f"/** {e.endpoint_id} 오류 응답 본문 ({', '.join(str(r.status) for r in errs) or '-'}) */",
            f"export type {base}Error = {' | '.join(err_types) or 'unknown'};",
            f"/** {e.endpoint_id} 요청 기록 — 본문·경로 변수는 계약의 모양 (match 조건에서 call.body.<필드>·call.params.<변수> 로 바로 읽는다) */",
            f"export interface {base}Request extends Omit<Call, 'body' | 'params'> {{",
            f"  body: {req_body};",
            f"  params: {req_params};",
            "}",
            f"/** {e.endpoint_id} 의 붙잡힌 요청 — 응답 본문은 계약의 모양 그대로 (다르면 tsc 가 잡는다) */",
            f"export interface {base}Call extends Omit<CallHandle, 'respond' | 'fail' | 'body' | 'params'> {{",
            f"  body: {req_body};",
            f"  params: {req_params};",
            f"  respond(json: {base}Ok, status?: number): Promise<void>;",
            f"  respond(status: number, json: {base}Ok | {base}Error): Promise<void>;",
            f"  fail(status?: number, json?: {base}Error): Promise<void>;",
            f"  fail(json: {base}Error): Promise<void>;",
            "}",
            f"/** {e.endpoint_id} {e.method} {e.path} — {e.summary}"
            + (f" (쿼리: {q})" if q else "")
            + " */",
            f"export function waitFor{base}(match?: (call: {base}Request) => boolean): Promise<{base}Call> {{",
            f"  return api.waitForCall({_ts(e.method or '')}, {_ts(msw_path(e.path))}, match as ((call: Call) => boolean) | undefined) as unknown as Promise<{base}Call>;",
            "}",
        ]
    if audience == "hidden" and plan is not None:
        lines += fault_lines(contract, plan)
    return GeneratedFile(path=rule.path, content="\n".join(lines) + "\n")


def fault_fn(case_id: str) -> str:
    """숨은 케이스의 실패 지정 함수 이름 — hc1 → failHc1."""
    return "fail" + case_id[:1].upper() + case_id[1:]


def fault_lines(contract: TaskContract, plan: HiddenPlan) -> list[str]:
    """숨은 케이스마다 승인된 실패 지정(HiddenCase.faults)을 거는 함수 — 테스트는 부르기만 한다 (LLM 이 실패 거는 꼴을 정하지 않는다).
    본문으로 고른 요청에만 실패를 준다 (api.once 의 match) — 몇 번째 요청인지·하나씩인지 한꺼번에인지와 무관하다.
    2026-11-11 실패 지정 알파: 같은 지정을 hc1 은 api.once(match) 로 맞게, hc2 는 waitForCall → respond/networkError 로 써 —
    한꺼번에 보내는 구현에서 첫 응답이 붙잡힌 요청 모두에 가 두 번째 요청을 기다리다 예외로 끝났다."""
    from task_spec.starter.conformance import _Examples  # 순환 import 를 피해 여기서

    by_id = {e.endpoint_id: e for e in contract.endpoints if e.method}
    examples = _Examples(contract)
    out: list[str] = []
    for c in plan.cases:
        faults = [f for f in c.faults if f.endpoint in by_id]
        if not faults:
            continue
        out += [
            "",
            f"/** {c.case_id} 승인된 실패 지정 — 케이스 맨 앞(요청이 나가기 전)에 한 번 부른다. 그 항목의 요청만 실패하고 나머지는 평소대로",
            " *  (routeContract·api.collection 의 응답). 이 실패를 waitFor…·fail·networkError·api.once 로 따로 걸지 않는다. */",
            f"export function {fault_fn(c.case_id)}(): void {{",
        ]
        for f in faults:
            e = by_id[f.endpoint]
            if f.status is None:
                reply = "{ networkError: true }"
            else:
                err = next((r for r in e.responses if r.status == f.status), None) or next(
                    (r for r in e.responses if r.status >= 400 and (r.body or r.returns)), None
                )
                body = examples.response(err) if err is not None else None
                if body is None:
                    body = {"message": "일시 오류"}
                reply = f"{{ status: {f.status}, json: {json.dumps(body, ensure_ascii=False)} }}"
            match = (
                " && ".join(
                    f"String((c.body as Record<string, unknown>)[{_ts(k)}]) === {_ts(str(v))}"
                    for k, v in f.when.items()
                )
                or "true"
            )
            what = ", ".join(f"{k}={v}" for k, v in f.when.items()) or "모든 요청"
            for _ in range(f.times):
                out.append(
                    f"  api.once({_ts(e.method or '')}, {_ts(msw_path(e.path))}, {reply}, (c) => {match}); // {e.endpoint_id} {what}"
                )
        out.append("}")
    return out


def id_kinds(contract: TaskContract) -> dict[str, str]:
    """목록 경로(MSW 꼴) → 항목 id 형식, 숫자 id 인 것만 — 하네스 api.collection 이 서버가 정하는 id 를 계약 형식으로 매기게 (React v12).
    API 마다 성공 응답의 엔티티(returns) 또는 본문 칸의 `id` 형식을 본다. 항목 경로(`/memos/:id`)는 목록 경로(`/memos`)로.
    한 목록 경로에 형식이 엇갈리면 알리지 않는다 (글자가 기본)."""
    by_key = {m.entity_id: m for m in contract.data_shapes()} | {
        m.name: m for m in contract.data_shapes()
    }
    seen: dict[str, set[str]] = {}
    for e in contract.endpoints:
        ok = e.success()
        if not e.method or ok is None:
            continue
        if ok.returns:
            ent = by_key.get(ok.returns.removesuffix("[]"))
            fields = list(ent.fields) if ent is not None else []
        else:
            fields = list(ok.body)
        f = next((x for x in fields if x.name == "id"), None)
        if f is None:
            continue
        base = msw_path(e.path).rstrip("/")
        if re.search(r"/:[A-Za-z]\w*$", base):
            base = base.rsplit("/", 1)[0]
        kind = "number" if parse_field(f.type).kind in ("integer", "number") else "string"
        seen.setdefault(base or "/", set()).add(kind)
    return {k: "number" for k, v in seen.items() if v == {"number"}}


# 앱 쪽 계약 타입 — 시작 코드·참조 구현이 서버 응답을 같은 모양으로 읽게 (2026-11-10 알파 분류: 생성물끼리 계약 해석이 어긋난
# 실패의 대표 — fefix e99cbba0 시작 코드 api 모듈이 배열 응답을 `{ items }` 로 읽음. 전에는 구현마다 src/ 에 타입을 직접 정의했다)
APP_TYPES = "src/api/contract-types.ts"


def options_lines(contract: TaskContract, how: str) -> list[str]:
    """선택형 요소의 고를 수 있는 값 (계약 screens[].elements[].options) — 테스트 도우미와 앱 타입이 같은 상수를 낸다."""
    opts: dict[str, list[str]] = {}
    for sc in contract.screens:
        for el in sc.elements:
            if el.options:
                opts.setdefault(_const(el.test_id), list(el.options))
    return [
        f"/** 선택형 요소의 고를 수 있는 값 (계약 options) — {how} */",
        "export const OPTIONS = {",
        *[f"  {k}: {json.dumps(v, ensure_ascii=False)}," for k, v in opts.items()],
        "} as const;",
        "",
    ]


def app_type_names(contract: TaskContract) -> list[str]:
    """앱 계약 타입 파일이 내보내는 이름 — 엔티티 이름 + API 마다 <Api>Ok·<Api>Error (+ 본문 있는 요청은 <Api>Body)."""
    out = [_ident(m.name) for m in contract.data_shapes()]
    for e in contract.endpoints:
        if e.method:
            base = method_name(e)[:1].upper() + method_name(e)[1:]
            out += [f"{base}Ok", f"{base}Error", *([f"{base}Body"] if e.request else [])]
    return list(dict.fromkeys(out))


def render_app_types(contract: TaskContract, manifest: TemplateManifest) -> GeneratedFile | None:
    """`src/api/contract-types.ts` — 공개 계약의 데이터 모양·응답 본문 타입 (테스트 도우미 contract.ts 와 같은 이름·같은 모양).
    잠긴 파일로 시작 코드 오버레이에 코드가 넣는다 — 생성기가 같은 경로를 쓰면 버린다. 공개 계약만 (지원자에게 보여도 된다)."""
    language = manifest.stack.language.lower() if manifest.stack else ""
    if language != "typescript" or manifest.public_helper is None or not contract.screens:
        return None  # Java·Python 템플릿은 켜지 않는다 (백엔드는 2단계에서 타입 대응표로)
    names = {m.entity_id: _ident(m.name) for m in contract.data_shapes()}
    by_name = {_ident(m.name): _ident(m.name) for m in contract.data_shapes()} | {
        m.entity_id: _ident(m.name) for m in contract.data_shapes()
    }  # 필드 타입에 엔티티 id(m2[]) 를 써도 (스모크 예시와 같게)
    lines = [
        "// 이 과제의 공개 계약에서 빌드 파이프라인이 만든 타입 — 손으로 고치지 않는다 (잠김).",
        "// 서버 응답은 이 타입으로 읽는다: `const data: <Api>Ok = await res.json();` — 같은 이름의 타입을 다시 정의하지 않는다.",
        "",
        *options_lines(contract, "선택 목록은 이 값을 그대로 그린다 (안내 문구는 따로)"),
        "/** 계약 데이터 모양 (entities) */",
    ]
    for m in contract.data_shapes():  # 테이블 엔티티 + 응답 모양
        lines.append(f"export interface {names[m.entity_id]} {{")
        lines += [
            f"  {_prop(f.name)}{'' if f.required else '?'}: {ts_type(f.type, by_name)};"
            for f in m.fields
        ]
        lines.append("}")
    for e in contract.endpoints:
        if not e.method:
            continue
        base = method_name(e)[:1].upper() + method_name(e)[1:]
        ok = e.success()
        errs = [r for r in e.responses if r.status >= 400 and (r.body or r.returns)]
        err_types = list(dict.fromkeys(_body_type(r, names, by_name) for r in errs))
        lines += [
            "",
            f"/** {e.endpoint_id} {e.method} {e.path} 성공 응답 본문 ({ok.status if ok else '-'}) — {e.summary} */",
            f"export type {base}Ok = {_body_type(ok, names, by_name) if ok else 'unknown'};",
            f"/** {e.endpoint_id} 오류 응답 본문 ({', '.join(str(r.status) for r in errs) or '-'}) */",
            f"export type {base}Error = {' | '.join(err_types) or 'unknown'};",
        ]
        if e.request:
            fields = "; ".join(
                f"{_prop(f.name)}{'' if f.required else '?'}: {ts_type(f.type, by_name)}"
                for f in e.request
            )
            lines += [
                f"/** {e.endpoint_id} 요청 본문 */",
                f"export type {base}Body = {{ {fields} }};",
            ]
    return GeneratedFile(path=APP_TYPES, content="\n".join(lines) + "\n")


def _ident(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_$]", "", name)
    return out if out and not out[0].isdigit() else f"T{out}"


def _prop(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else _ts(name)


_TS_SCALAR = {
    "string": "string",
    "datetime": "string",
    "date": "string",
    "time": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "object": "Record<string, unknown>",
}


def ts_type(t: str, entities: dict[str, str]) -> str:
    """계약 필드 형식 → TypeScript 타입. 모르는 형식은 unknown (막지 않는다). 해석은 field_types (스모크 예시 값과 같은 해석)."""
    return _ts_of(parse_field(t), entities)


def _ts_of(ft: FieldType, entities: dict[str, str]) -> str:
    if ft.kind == "list":
        inner = _ts_of(ft.items[0], entities)
        out = f"({inner})[]" if "|" in inner else f"{inner}[]"
    elif ft.kind == "enum":
        out = " | ".join(_ts(v) for v in ft.values) or "string"
    elif ft.kind == "union":
        out = " | ".join(dict.fromkeys(_ts_of(o, entities) for o in ft.items))
    elif ft.kind == "ref":
        out = entities.get(_ident(ft.name), "unknown")
    else:
        out = _TS_SCALAR.get(ft.kind, "unknown")
    return f"{out} | null" if ft.nullable and out != "unknown" else out


def _body_type(r: ContractResponse, names: dict[str, str], entities: dict[str, str]) -> str:
    if r.returns:
        name = names.get(r.returns.removesuffix("[]"), "unknown")
        return f"{name}[]" if r.returns.endswith("[]") else name
    if r.body:
        fields = "; ".join(
            f"{_prop(f.name)}{'' if f.required else '?'}: {ts_type(f.type, entities)}"
            for f in r.body
        )
        return f"{{ {fields} }}"
    return "unknown"


# ── 테스트 파일의 vitest import 보정 ──────────────────────────────────────

_VITEST_NAMES = (
    "describe",
    "it",
    "test",
    "expect",
    "beforeEach",
    "afterEach",
    "beforeAll",
    "afterAll",
    "vi",
)
_VITEST_IMPORT = re.compile(r"^import\s*\{([^}]*)\}\s*from\s*['\"]vitest['\"];?[ \t]*$", re.M)
_USE = {n: re.compile(rf"(?<![\w.$]){n}\s*[.(]") for n in _VITEST_NAMES}
_TS_STRING_OR_COMMENT = re.compile(
    r"//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`", re.S
)


def ensure_vitest_imports(files: list[GeneratedFile]) -> tuple[list[GeneratedFile], list[str]]:
    """테스트 파일(*.test.ts·*.test.tsx)이 쓰는 vitest 이름(describe·it·expect·beforeEach …) 중 import 하지 않은 것을 더한다.

    2026-11-09 상류 평가(React 3건): 숨은 테스트가 모두 `beforeEach` 를 vitest 에서 import 하지 않아 첫 컴파일 0/3 — 생성기에 다시 묻지 않고
    코드가 고친다 (같은 이름을 파일이 따로 정의하면 건드리지 않는다)."""
    out: list[GeneratedFile] = []
    notes: list[str] = []
    for f in files:
        if not re.search(r"\.test\.tsx?$", f.path):
            out.append(f)
            continue
        code = _TS_STRING_OR_COMMENT.sub(" ", f.content)
        m = _VITEST_IMPORT.search(f.content)
        have = {x.strip().split(" as ")[-1].strip() for x in m.group(1).split(",")} if m else set()
        own = set(re.findall(r"\b(?:const|let|function|import)\s+\{?\s*([A-Za-z_$][\w$]*)", code))
        missing = [
            n for n in _VITEST_NAMES if n not in have and n not in own and _USE[n].search(code)
        ]
        if not missing:
            out.append(f)
            continue
        if m is not None:
            names = [x.strip() for x in m.group(1).split(",") if x.strip()] + missing
            content = (
                f.content[: m.start()]
                + f"import {{ {', '.join(names)} }} from 'vitest';"
                + f.content[m.end() :]
            )
        else:
            content = f"import {{ {', '.join(missing)} }} from 'vitest';\n" + f.content
        out.append(GeneratedFile(path=f.path, content=content))
        notes.append(f"{f.path}: vitest import 에 {', '.join(missing)} 를 더함")
    return out, notes


# ── 숨은·공개 테스트의 하네스·testing-library import 보정 · 응답 자리 보정 (React v12) ──────────────────────────

# 하네스(grader.ts)가 내보내는 값과 testing-library 의 화면 도구 — 쓰는 꼴(이름 뒤 '.' 또는 '(')로 찾는다.
# screen 은 import 가 없어도 브라우저 전역(window.screen)으로 읽혀 'Cannot find name' 이 아니라 TS2339 가 난다 — 오류 문구가 아니라
# 코드에서 쓰는지로 판단한다 (2026-11-12 #102 검토: 컴파일 기록 Cannot find name 'api' 12번 · within 1번).
HARNESS_EXPORTS: dict[str, str] = {
    "api": ".",
    "network": ".",
    "params": ".",
    "renderApp": "(",
    "byTestId": "(",
    "textOf": "(",
    "textsByRole": "(",
    "flush": "(",
    "openRoute": "(",
    "reason": "(",
    "tag": "(",
}
# @testing-library/react(템플릿이 고정한 16.3)와 그것이 다시 내보내는 @testing-library/dom(10.4)의 값 내보내기 → 쓰는 꼴('.' 속성, '(' 호출).
# 빠진 import 를 코드가 채운다 (ensure_test_imports). #177: screen·waitFor·within 셋만 채워 하네스가 쓰는 act·cleanup·render·fireEvent 는
# 빠진 채 컴파일 오류로 갔다. 이름별 쿼리 함수(getByText 등 — 화면 밖 단독 호출)는 넣지 않는다 (screen.… 으로 쓰고, 단독 이름은 짐작 쿼리다)
TESTING_LIBRARY: dict[str, str] = {
    "act": "(",
    "buildQueries": "(",
    "cleanup": "(",
    "configure": "(",
    "createEvent": ".(",
    "fireEvent": ".(",
    "getConfig": "(",
    "getDefaultNormalizer": "(",
    "getNodeText": "(",
    "getQueriesForElement": "(",
    "getRoles": "(",
    "isInaccessible": "(",
    "logDOM": "(",
    "logRoles": "(",
    "prettyDOM": "(",
    "queries": ".",
    "queryHelpers": ".",
    "render": "(",
    "renderHook": "(",
    "screen": ".",
    "waitFor": "(",
    "waitForElementToBeRemoved": "(",
    "within": "(",
}
TL_MODULE = "@testing-library/react"
_IMPORT_STMT = re.compile(
    r"^import\s+(?:type\s+)?(?P<what>[\s\S]*?)\s+from\s*['\"](?P<mod>[^'\"]+)['\"]\s*;?[ \t]*$",
    re.M,
)
_REPLY_KEYS = frozenset({"status", "json", "networkError"})


def _imported_names(text: str) -> set[str]:
    names: set[str] = set()
    for m in _IMPORT_STMT.finditer(text):
        what = m.group("what")
        braces = re.search(r"\{([^}]*)\}", what)
        if braces:
            for part in braces.group(1).split(","):
                part = part.strip().removeprefix("type ").strip()
                if part:
                    names.add(part.split(" as ")[-1].strip())
        rest = re.sub(r"\{[^}]*\}", "", what)
        names |= set(re.findall(r"(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)", rest.replace(",", " "))) - {
            "as",
            "type",
        }
    return names


def _add_import(text: str, names: list[str], module: str, same: Callable[[str], bool]) -> str:
    """names 를 module 의 import 에 더한다 — 같은 모듈(same) 의 중괄호 import 가 있으면 거기에, 없으면 마지막 import 뒤에 새 줄."""
    for m in _IMPORT_STMT.finditer(text):
        braces = re.search(r"\{([^}]*)\}", m.group("what"))
        if same(m.group("mod")) and braces and not m.group(0).lstrip().startswith("import type"):
            have = [x.strip() for x in braces.group(1).split(",") if x.strip()]
            start = m.start("what") + braces.start(1)
            end = m.start("what") + braces.end(1)
            return text[:start] + " " + ", ".join([*have, *names]) + " " + text[end:]
    line = f"import {{ {', '.join(names)} }} from '{module}';\n"
    stmts = list(_IMPORT_STMT.finditer(text))
    last = stmts[-1] if stmts else None
    if last is None:
        return line + text
    at = last.end()
    nl = text.find("\n", at)
    at = len(text) if nl < 0 else nl + 1
    return text[:at] + line + text[at:]


def _uses(code: str, name: str, how: str) -> bool:
    """이름을 그 꼴로 쓰나 — how 는 뒤에 오는 글자들 ('.' 속성, '(' 호출, '.(' 둘 다)."""
    return re.search(rf"(?<![\w.$]){re.escape(name)}\s*[{re.escape(how)}]", code) is not None


def _defines(code: str, name: str) -> bool:
    return (
        re.search(rf"\b(?:const|let|var|function|class)\s+{re.escape(name)}\b", code) is not None
        or re.search(rf"\b(?:const|let|var)\s*\{{[^}}]*\b{re.escape(name)}\b[^}}]*\}}\s*=", code)
        is not None
    )


def _args(text: str, open_at: int) -> list[tuple[int, int]] | None:
    """text[open_at] 이 '(' 인 호출의 인자 구간들 (괄호·문자열 안의 쉼표는 건너뜀). 닫는 ')' 를 못 찾으면 None."""
    spans: list[tuple[int, int]] = []
    depth, i, start = 0, open_at, open_at + 1
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            j = i + 1
            while j < len(text) and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                spans.append((start, i))
                return spans
        elif ch == "," and depth == 1:
            spans.append((start, i))
            start = i + 1
        i += 1
    return None


def _object_keys(literal: str) -> list[str] | None:
    """객체 리터럴의 맨 바깥 칸 이름들 — 펼침(...)·계산된 이름이 있으면 None (모양을 모른다)."""
    inner = literal.strip()[1:-1]
    parts = _args("(" + inner + ")", 0)
    if parts is None:
        return None
    keys: list[str] = []
    for a, b in parts:
        part = ("(" + inner + ")")[a:b].strip()
        if not part:
            continue
        if part.startswith("...") or part.startswith("["):
            return None
        head = part.split(":", 1)[0].strip() if ":" in part else part
        keys.append(head.strip("'\""))
    return keys


def wrap_bare_replies(content: str) -> tuple[str, int]:
    """`api.once(메서드, 경로, 응답)`·`api.always(…)` 의 응답 자리에 응답 칸(status·json·networkError) 없이 본문을 바로 넣었으면
    `{ json: 본문 }` 으로 감싼다. 펼침·계산된 이름·빈 객체·변수는 건드리지 않는다 (모양을 모르면 tsc·재요청에 맡긴다)."""
    edits: list[tuple[int, int, str]] = []
    for m in re.finditer(r"\bapi\.(?:once|always)\s*(?:<[^>()]*>)?\s*\(", content):
        spans = _args(content, m.end() - 1)
        if spans is None or len(spans) < 3:
            continue
        a, b = spans[2]
        arg = content[a:b]
        lit = arg.strip()
        if not (lit.startswith("{") and lit.endswith("}")):
            continue
        keys = _object_keys(lit)
        if not keys or _REPLY_KEYS & set(keys):
            continue
        lead = arg[: len(arg) - len(arg.lstrip())]
        tail = arg[len(arg.rstrip()) :]
        edits.append((a, b, f"{lead}{{ json: {lit} }}{tail}"))
    for a, b, new in reversed(edits):
        content = content[:a] + new + content[b:]
    return content, len(edits)


def ensure_test_imports(
    files: list[GeneratedFile], harness_module: str
) -> tuple[list[GeneratedFile], list[str]]:
    """숨은·공개 테스트(*.test.ts·tsx)의 빠진 import 를 코드가 채우고, 응답 자리에 바로 넣은 본문을 감싼다 — LLM 재요청 없이.
    vitest 이름 (ensure_vitest_imports) · 하네스 이름(harness_module 에서, 파일 위치 기준 상대 경로) · screen·waitFor·within
    (testing-library) · `api.once/always` 응답 자리 (wrap_bare_replies). 파일이 같은 이름을 정의하거나 다른 곳에서 가져오면 건드리지 않는다."""
    files, notes = ensure_vitest_imports(files)
    out: list[GeneratedFile] = []
    for f in files:
        if not re.search(r"\.test\.tsx?$", f.path):
            out.append(f)
            continue
        text = f.content
        code = _TS_STRING_OR_COMMENT.sub(" ", text)
        have = _imported_names(text)
        rel = posixpath.relpath(harness_module, posixpath.dirname(f.path))
        rel = rel if rel.startswith(".") else f"./{rel}"
        added: list[str] = []
        for group, module, same in (
            (HARNESS_EXPORTS, rel, lambda mod: mod.rstrip("/").endswith("harness/grader")),
            (TESTING_LIBRARY, TL_MODULE, lambda mod: mod == TL_MODULE),
        ):
            missing = [
                n
                for n, how in group.items()
                if n not in have and not _defines(code, n) and _uses(code, n, how)
            ]
            if missing:
                text = _add_import(text, missing, module, same)
                added += missing
        # 하네스 api 일 때만 (파일이 api 를 직접 정의했으면 그 객체의 once 는 하네스와 무관)
        text, wrapped = wrap_bare_replies(text) if not _defines(code, "api") else (text, 0)
        if added:
            notes.append(f"{f.path}: import 에 {', '.join(added)} 를 더함")
        if wrapped:
            notes.append(
                f"{f.path}: api.once·always 응답 자리에 바로 넣은 본문 {wrapped}곳을 {{ json: … }} 으로 감쌈"
            )
        out.append(GeneratedFile(path=f.path, content=text) if text != f.content else f)
    return out, notes
