"""TypeScript 계약 도우미 — 프론트엔드 템플릿의 숨은·공개 테스트가 import 하는 `contract.ts` 를 계약에서 코드가 쓴다 (LLM 없음).

Java 템플릿의 ContractCase·PublicCase(starter/hidden_helpers.py)와 같은 자리: 테스트가 test id·화면 경로·API 경로를 손으로 쓰지 않게
상수와 함수로 준다. 없는 이름을 쓰면 tsc 컴파일 루프가 잡는다 (build.compile_and_fix). 2026-11-09 React 템플릿 파이프라인 연결.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from task_spec.schemas.build import ContractResponse, GeneratedFile, TaskContract
from task_spec.schemas.template import TemplateManifest
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
    contract: TaskContract, manifest: TemplateManifest, audience: Literal["hidden", "public"]
) -> GeneratedFile | None:
    rule = manifest.hidden_helper if audience == "hidden" else manifest.public_helper
    if rule is None:
        return None
    harness = "../harness/grader" if audience == "hidden" else "../__grading__/harness/grader"
    who = "숨은 케이스" if audience == "hidden" else "공개 테스트"
    lines = [
        f"// 이 과제의 공개 계약에서 빌드 파이프라인이 만든 도우미 — 손으로 고치지 않는다. {who}가 import 한다.",
        "// test id·화면 경로·API 경로를 직접 쓰지 말고 아래 상수·함수를 쓴다 (없는 이름은 tsc 가 잡는다).",
        f"import {{ api, type Call, type CallHandle }} from {_ts(harness)};",
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
        f"  {k}: {_ts(v)}," + (f" // 조작: {actions[k]}" if k in actions else "")
        for k, v in tids.items()
    ]
    lines += ["} as const;", ""]
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
    lines += [
        "/** 이 과제의 계약 API 를 모두 가로챈다 — 케이스 파일의 beforeEach 에서 (가로챈 요청은 응답할 때까지 붙잡힌다) */",
        "export function routeContract(): void {",
        *[f"  api.route({_ts(e.method or '')}, {_ts(msw_path(e.path))});" for e in endpoints],
        "}",
    ]
    names = {m.entity_id: _ident(m.name) for m in contract.entities}
    by_name = {_ident(m.name): _ident(m.name) for m in contract.entities}
    lines += ["", "/** 계약 데이터 모양 (entities) — 응답 본문의 타입 */"]
    for m in contract.entities:
        lines.append(f"export interface {names[m.entity_id]} {{")
        lines += [
            f"  {_prop(f.name)}{'' if f.required else '?'}: {ts_type(f.type, by_name)};"
            for f in m.fields
        ]
        lines.append("}")
    for e in endpoints:
        q = ", ".join(f.name for f in e.query)
        base = method_name(e)[:1].upper() + method_name(e)[1:]
        ok = next((r for r in e.responses if 200 <= r.status < 300), None)
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
    return GeneratedFile(path=rule.path, content="\n".join(lines) + "\n")


_SCALAR = {
    **dict.fromkeys(
        ("string", "text", "uuid", "date", "datetime", "date-time", "time", "email", "url"),
        "string",
    ),
    **dict.fromkeys(("integer", "int", "long", "number", "float", "double", "decimal"), "number"),
    **dict.fromkeys(("boolean", "bool"), "boolean"),
    **dict.fromkeys(("array", "list"), "unknown[]"),
    **dict.fromkeys(("object", "map", "json"), "Record<string, unknown>"),
}


def _ident(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_$]", "", name)
    return out if out and not out[0].isdigit() else f"T{out}"


def _prop(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else _ts(name)


def ts_type(t: str, entities: dict[str, str]) -> str:
    """계약 필드 형식 → TypeScript 타입. 모르는 형식은 unknown (막지 않는다)."""
    t = t.strip()
    if t.endswith("[]"):
        inner = ts_type(t[:-2], entities)
        return f"({inner})[]" if "|" in inner else f"{inner}[]"
    m = re.fullmatch(r"(?:array|list)\s*<\s*(.+?)\s*>", t, re.I)
    if m:
        return ts_type(m.group(1) + "[]", entities)
    m = re.fullmatch(r"enum\s*\((.+)\)", t, re.I)
    if m:
        values = [v.strip() for v in re.split(r"[|,]", m.group(1)) if v.strip()]
        return " | ".join(_ts(v) for v in values) or "string"
    if _ident(t) in entities:
        return entities[_ident(t)]
    return _SCALAR.get(t.lower(), "unknown")


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
