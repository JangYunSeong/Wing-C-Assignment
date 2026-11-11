"""코드 뼈대 (React) — 계약이 이미 공개한 구조(API 호출 함수·화면 컴포넌트·라우트)를 코드가 먼저 쓴다 (docs/design/code-skeleton.md).

생성기(시작 코드·흔한 구현·허용 구현)는 이 뼈대 위에서 몸통만 채운다 — 이름·시그니처·라우트는 유지한다 (static_check._skeleton_kept).
이름은 하네스 도우미와 같은 규칙 (method_name: POST /api/inspection-notes → postApiInspectionNotes) — 테스트·앱·README 가 한 규칙.
몸통에는 함정의 장치를 넣지 않는다 — API 호출 함수의 기본 몸통은 fetch 한 번 (재시도·캐시·갱신·중복 제거·취소 없음, 취소·헤더는 init 으로).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from task_spec.schemas.build import Endpoint, GeneratedFile, Screen, TaskContract
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.hidden_helpers import method_name
from task_spec.starter.ts_helpers import APP_TYPES, _ident, _ts, ts_type

ENDPOINTS = "src/api/endpoints.ts"
APP = "src/App.tsx"
PAGES = "src/pages"

_VAR = re.compile(r"\{(\w+)\}|:(\w+)")


@dataclass(frozen=True)
class Fn:
    """뼈대의 API 호출 함수 하나 — 이름과 매개변수(이름 목록)를 정적 검사가 대조한다."""

    name: str
    params: tuple[str, ...]


def _base(e: Endpoint) -> str:
    n = method_name(e)
    return n[:1].upper() + n[1:]


def _path_vars(path: str) -> list[str]:
    return list(dict.fromkeys(a or b for a, b in _VAR.findall(path)))


def endpoint_fns(contract: TaskContract) -> list[Fn]:
    out: list[Fn] = []
    for e in contract.endpoints:
        if not e.method:
            continue
        params = [*_path_vars(e.path)]
        if e.request:
            params.append("body")
        if e.query:
            params.append("query")
        params.append("init")
        out.append(Fn(method_name(e), tuple(params)))
    return out


def page_name(sc: Screen) -> str:
    """화면 → 컴포넌트 이름 — 경로의 변수 아닌 마디를 PascalCase 로 + Page ('/inspection/notes/new' → InspectionNotesNewPage)."""
    segs = [s for s in sc.route.strip("/").split("/") if s and not _VAR.fullmatch(s)]
    words = [w for s in segs for w in re.split(r"[^A-Za-z0-9]+", s) if w]
    name = "".join(w[:1].upper() + w[1:] for w in words) or "Home"
    return _ident(name) + "Page"


def page_names(contract: TaskContract) -> dict[str, str]:
    """screen_id → 컴포넌트 이름 (겹치면 화면 id 를 붙인다)."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for sc in contract.screens:
        n = page_name(sc)
        if n in used:
            n = n.removesuffix("Page") + sc.screen_id.upper() + "Page"
        used.add(n)
        out[sc.screen_id] = n
    return out


def _route(route: str) -> str:
    """계약 경로 → React Router 경로 ('{id}' → ':id')."""
    return re.sub(r"\{(\w+)\}", r":\1", route)


def render_endpoints(contract: TaskContract) -> GeneratedFile | None:
    eps = [e for e in contract.endpoints if e.method]
    if not eps:
        return None
    names = {m.entity_id: _ident(m.name) for m in contract.entities} | {
        _ident(m.name): _ident(m.name) for m in contract.entities
    }
    imports: list[str] = []
    lines: list[str] = []
    for e in eps:
        base = _base(e)
        imports.append(f"{base}Ok")
        params: list[str] = [f"{v}: string" for v in _path_vars(e.path)]
        if e.request:
            imports.append(f"{base}Body")
            params.append(f"body: {base}Body")
        if e.query:
            q = "; ".join(
                f"{f.name}{'' if f.required else '?'}: {ts_type(f.type, names)}" for f in e.query
            )
            params.append(f"query: {{ {q} }}")
        params.append("init?: RequestInit")
        url = re.sub(
            r"\{(\w+)\}|:(\w+)",
            lambda m: "${encodeURIComponent(" + (m.group(1) or m.group(2)) + ")}",
            e.path,
        )
        qs = (
            "\n  const qs = new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)])).toString();"
            if e.query
            else ""
        )
        target = f"`{url}${{qs ? `?${{qs}}` : ''}}`" if e.query else f"`{url}`"
        body = "\n    body: JSON.stringify(body)," if e.request else ""
        heads = (
            " (요청 헤더: "
            + ", ".join(f"{h.name}{'' if h.required else ' 선택'}" for h in e.headers)
            + " — init.headers 로)"
            if e.headers
            else ""
        )
        lines += [
            "",
            f"/** {e.endpoint_id} {e.method} {e.path} — {e.summary}{heads} */",
            f"export async function {method_name(e)}({', '.join(params)}): Promise<{base}Ok> {{{qs}",
            # Headers 로 받아야 부르는 쪽이 Headers 객체를 넘겨도 값이 남는다 (펼치기 {...headers} 는 Headers 를 비운다)
            "  const headers = new Headers(init?.headers);",
            *(["  headers.set('Content-Type', 'application/json');"] if e.request else []),
            f"  const res = await fetch({target}, {{",
            "    ...init,",
            f"    method: {_ts(e.method or 'GET')},",
            "    headers,",
            *([body.strip("\n")] if body else []),
            "  });",
            "  return readJson<" + f"{base}Ok>(res);",
            "}",
        ]
    head = [
        "// 계약 API 호출 함수 — 빌드 파이프라인이 계약에서 만든 뼈대. 함수 이름·매개변수는 유지한다 (몸통은 고쳐도 된다).",
        "// 기본 몸통은 fetch 한 번 — 2xx 가 아니면 ApiError(status, 본문). 취소(signal)·헤더는 init 으로 부르는 쪽이 정한다.",
        f"import type {{ {', '.join(dict.fromkeys(imports))} }} from './{APP_TYPES.rsplit('/', 1)[1].removesuffix('.ts')}';",
        "",
        "/** 2xx 가 아닌 응답 — status 와 본문(JSON 이면 객체, 아니면 글자·null) */",
        "export class ApiError extends Error {",
        "  constructor(public readonly status: number, public readonly body: unknown) {",
        "    super(`HTTP ${status}`);",
        "    this.name = 'ApiError';",
        "  }",
        "}",
        "",
        "async function readJson<T>(res: Response): Promise<T> {",
        "  const text = await res.text();",
        "  let data: unknown = null;",
        "  if (text) {",
        "    try {",
        "      data = JSON.parse(text);",
        "    } catch {",
        "      data = text;",
        "    }",
        "  }",
        "  if (!res.ok) throw new ApiError(res.status, data);",
        "  return data as T;",
        "}",
    ]
    return GeneratedFile(path=ENDPOINTS, content="\n".join(head + lines) + "\n")


def render_pages(contract: TaskContract) -> list[GeneratedFile]:
    names = page_names(contract)
    out: list[GeneratedFile] = []
    for sc in contract.screens:
        n = names[sc.screen_id]
        out.append(
            GeneratedFile(
                path=f"{PAGES}/{n}.tsx",
                content=(
                    f"/** 화면 {sc.screen_id} `{sc.route}` — {sc.summary} (뼈대 — 컴포넌트 이름은 유지한다) */\n"
                    f"export default function {n}() {{\n"
                    "  return <section />;\n"
                    "}\n"
                ),
            )
        )
    return out


def render_app(contract: TaskContract) -> GeneratedFile | None:
    if not contract.screens:
        return None
    names = page_names(contract)
    imports = [
        f"import {names[sc.screen_id]} from './pages/{names[sc.screen_id]}';"
        for sc in contract.screens
    ]
    routes = [
        f"            <Route path={_ts(_route(sc.route))} element={{<{names[sc.screen_id]} />}} />"
        for sc in contract.screens
    ]
    content = "\n".join(
        [
            "import { useState } from 'react';",
            "import { BrowserRouter, Route, Routes } from 'react-router-dom';",
            "import { QueryClient, QueryClientProvider } from '@tanstack/react-query';",
            *imports,
            "",
            "/** 앱 화면의 뿌리 — 빌드 파이프라인이 계약 화면에서 만든 뼈대. 화면 경로 → 컴포넌트 라우트는 유지한다.",
            " *  QueryClient 는 마운트마다 새로 (테스트가 앱을 새로 그릴 때 캐시가 남지 않게). */",
            "export default function App() {",
            "  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));",
            "  return (",
            "    <QueryClientProvider client={client}>",
            "      <BrowserRouter>",
            '        <div className="page">',
            '          <main className="page-main">',
            "          <Routes>",
            *routes,
            "          </Routes>",
            "          </main>",
            "        </div>",
            "      </BrowserRouter>",
            "    </QueryClientProvider>",
            "  );",
            "}",
            "",
        ]
    )
    return GeneratedFile(path=APP, content=content)


def render_skeleton(contract: TaskContract, manifest: TemplateManifest) -> list[GeneratedFile]:
    """템플릿이 뼈대를 켰을 때(manifest.skeleton)만 — API 호출 함수·화면 컴포넌트·라우트."""
    if not manifest.skeleton:
        return []
    files = [f for f in (render_endpoints(contract), render_app(contract)) if f is not None]
    return files + render_pages(contract)


def routes(contract: TaskContract) -> list[tuple[str, str]]:
    """뼈대 라우트 (React Router 경로, 컴포넌트 이름) — 정적 검사가 App.tsx 에 남았는지 본다."""
    names = page_names(contract)
    return [(_route(sc.route), names[sc.screen_id]) for sc in contract.screens]
