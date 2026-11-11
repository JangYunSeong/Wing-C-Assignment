"""계약 적합성 스모크 — 시작 코드·참조 구현이 공개 계약대로 응답을 받아도 깨지지 않는지, 계약에서 코드가 쓴 짧은 검사 (LLM 없음).

생성된 조각(시작 코드·숨은·공개 테스트·참조 구현)은 계약을 각자 읽는다. 2026-11-10 프론트 알파(fefix e99cbba0): 계약은 추천 응답을
배열로 정했는데 시작 코드의 api 모듈이 `{ items }` 로 읽어, 계약 모양 응답을 받는 순간 화면이 내려가(TypeError) 구현 4개가 모두
같은 공개 테스트에서 떨어졌다 — 수리는 공개 테스트를 의심해 다시 만들고 예산을 다 썼다. 이 검사가 테스트·구현보다 먼저 시작 코드를
계약대로 두드려, 깨지면 탓이 시작 코드(공유 코드)로 정해진다 (repair 게이트 ⓪).

지금은 TypeScript(React) 템플릿만: 화면마다 계약의 모든 API 를 계약 모양 예시로 바로 응답하게 두고(api.always), 화면을 열고 입력란에
글자를 넣은 뒤, 앱 오류(잡히지 않은 오류·React 렌더 오류)가 없고 화면이 남아 있는지 본다. 공개 계약만 쓴다 — 숨은 요구·계획 없음.
파일은 프로젝트 루트 `__conform__/` — tsconfig(include src)·지원자 vitest 설정(src/**)·채점 설정 어디에도 들지 않아, 스모크 자체의 결함이
시작 코드의 컴파일·기존 테스트 실패로 잡히지 않는다. 검증 때만 전용 설정으로 돌고 채점용·응시용 이미지에 넣지 않는다.
"""

from __future__ import annotations

import json
import re
from typing import Any

from task_spec.schemas.build import (
    ContractField,
    ContractResponse,
    Endpoint,
    GeneratedFile,
    TaskContract,
)
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.templates import is_java, is_python
from task_spec.starter.ts_helpers import msw_path

CONFORM_DIR = "__conform__"
CONFORM_TEST = f"{CONFORM_DIR}/conform.test.tsx"
CONFORM_CONFIG = f"{CONFORM_DIR}/vitest.conform.config.ts"
TYPED = "가상1"  # 입력란에 넣는 글자 — 글자·숫자 입력란 모두에 무언가 들어가게

# 계약 필드 타입은 생성기가 자유롭게 쓴다 (2026-11-10 저장 계약 집계: string·String·VARCHAR, long·bigint·Integer,
# datetime·timestamp·LocalDateTime·DateTime …) — 예시 값을 만들 때만 뜻으로 묶는다.
_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "datetime",
        ("datetime", "timestamp", "localdatetime", "instant", "offsetdatetime", "zoneddatetime"),
    ),
    ("date", ("date", "localdate")),
    ("integer", ("integer", "int", "long", "bigint", "short", "smallint", "int32", "int64")),
    ("number", ("number", "decimal", "bigdecimal", "double", "float", "numeric", "real")),
    ("boolean", ("boolean", "bool")),
    ("string", ("string", "varchar", "text", "char", "uuid", "email", "url")),
)
_LIST = re.compile(
    r"^(?:array<\s*(?P<a>[\w.]+)\s*>|list<\s*(?P<l>[\w.]+)\s*>|(?P<b>[\w.]+)\s*\[\])$", re.I
)
_ENUM = re.compile(r"^enum\((?P<v>[^)]*)\)$", re.I)


def type_kind(t: str) -> str:
    """계약 필드 타입 글 → string·integer·number·boolean·datetime·date·enum·list·ref·unknown."""
    s = t.strip()
    if _ENUM.match(s):
        return "enum"
    if _LIST.match(s) or s.lower() in ("array", "object[]"):
        return "list"
    base = s.split("(")[0].strip().lower()
    for kind, names in _KINDS:
        if base in names:
            return kind
    return "ref" if re.fullmatch(r"m\d{1,2}|[A-Z]\w*", s) else "unknown"


class _Examples:
    def __init__(self, contract: TaskContract) -> None:
        self.by_key = {e.entity_id: e for e in contract.entities} | {
            e.name: e for e in contract.entities
        }
        self._depth = 0

    def entity(self, key: str) -> dict[str, Any]:
        e = self.by_key.get(key)
        if e is None or self._depth > 2:  # 서로 가리키는 엔티티 — 깊이를 막는다
            return {}
        self._depth += 1
        try:
            return {f.name: self.value(f) for f in e.fields}
        finally:
            self._depth -= 1

    def value(self, f: ContractField) -> Any:
        t = f.type.strip()
        kind = type_kind(t)
        if kind == "enum":
            m = _ENUM.match(t)
            vals = [v.strip() for v in re.split(r"[|,]", m.group("v"))] if m else []
            return next((v for v in vals if v), "A")
        if kind == "list":
            m = _LIST.match(t)
            inner = m and (m.group("a") or m.group("l") or m.group("b"))
            if inner and inner in self.by_key:
                return [self.entity(inner)]
            if inner and type_kind(inner) not in ("ref", "unknown"):
                return [self.value(f.model_copy(update={"type": inner}))]
            return []
        if kind == "ref":
            return self.entity(t)
        return {
            "string": f"가상 {f.name}",
            "integer": 1,
            "number": 1.5,
            "boolean": False,
            "datetime": "2026-01-01T09:00:00Z",
            "date": "2026-01-01",
        }.get(kind, f"가상 {f.name}")

    def response(self, r: ContractResponse) -> Any:
        if r.returns:
            key = r.returns.removesuffix("[]")
            body = self.entity(key)
            return [body] if r.returns.endswith("[]") else body
        if r.body:
            return {f.name: self.value(f) for f in r.body}
        return None


def ok_response(e: Endpoint) -> ContractResponse | None:
    return next((r for r in e.responses if 200 <= r.status < 300), None)


def _route(route: str) -> str:
    """화면 경로의 변수(`:id`·`{id}`)는 1 로 채운다."""
    return re.sub(r":[A-Za-z]\w*|\{[A-Za-z]\w*\}", "1", route)


_TYPEABLE = (
    "input",
    "textbox",
    "searchbox",
    "combobox",
)  # combobox 는 글 입력란일 때만 (실행 때 태그로 거른다)


def applies(manifest: TemplateManifest, contract: TaskContract) -> bool:
    """TypeScript 템플릿의 ui 계약만 (Java·Python 은 2단계)."""
    return (
        not is_java(manifest)
        and not is_python(manifest)
        and manifest.hidden_layout is not None
        and bool(contract.screens)
    )


def render_conformance(contract: TaskContract, manifest: TemplateManifest) -> list[GeneratedFile]:
    """스모크 테스트 파일과 그것만 도는 vitest 설정. 해당 없으면 빈 목록."""
    if not applies(manifest, contract):
        return []
    hl = manifest.hidden_layout
    assert hl is not None
    ex = _Examples(contract)
    routes = []
    for e in contract.endpoints:
        if e.method is None:
            continue
        ok = ok_response(e)
        if ok is None:
            continue
        body = ex.response(ok)
        routes.append([e.method, msw_path(e.path), ok.status, body])
    lines = [
        "// 계약 적합성 스모크 — 빌드 파이프라인이 공개 계약에서 쓴다 (starter/conformance.py, LLM 없음). 손으로 고치지 않는다.",
        "// 계약 모양 그대로의 응답을 받아도 앱이 깨지지 않는지만 본다. 검증 때만 돌고 이미지에는 넣지 않는다.",
        "import { screen } from '@testing-library/react';",
        "import { afterEach, beforeEach, expect, it } from 'vitest';",
        f"import {{ api, flush, renderApp, type Method }} from '../{hl.harness}/grader';",
        "",
        "// [메서드, 경로, 상태, 계약 모양 예시 본문]",
        f"const ROUTES: [Method, string, number, unknown][] = {json.dumps(routes, ensure_ascii=False)};",
        "const errors: string[] = [];",
        "const onError = (e: ErrorEvent) => {",
        "  errors.push(String(e.error instanceof Error ? e.error.message : e.message).slice(0, 200));",
        "};",
        "let restore = () => {};",
        "beforeEach(() => {",
        "  errors.length = 0;",
        "  window.addEventListener('error', onError);",
        "  const original = console.error;",
        "  console.error = (...args: unknown[]) => {",
        "    const text = args.map((a) => (a instanceof Error ? a.message : String(a))).join(' ');",
        "    if (/The above error occurred|Uncaught|is not a function|Cannot read propert/.test(text)) errors.push(text.slice(0, 200));",
        "    original(...args);",
        "  };",
        "  restore = () => {",
        "    console.error = original;",
        "    window.removeEventListener('error', onError);",
        "  };",
        "  for (const [method, path, status, json] of ROUTES) api.always(method, path, { status, json });",
        "});",
        "afterEach(() => restore());",
        "",
        "const settle = async () => {",
        "  await flush();",
        "  await new Promise((r) => setTimeout(r, 400)); // 입력 디바운스·늦은 렌더",
        "  await flush();",
        "};",
        "",
    ]
    for sc in contract.screens:
        typeable = [el.test_id for el in sc.elements if el.role in _TYPEABLE]
        lines += [
            f"it({json.dumps(f'{sc.screen_id} {sc.route} — 계약 모양 응답에 화면이 깨지지 않는다', ensure_ascii=False)}, async () => {{",
            f"  const {{ user, container }} = renderApp({json.dumps(_route(sc.route))});",
            "  await settle();",
            f"  for (const id of {json.dumps(typeable)}) {{",
            "    const el = screen.queryByTestId(id);",
            "    if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) continue;",
            "    if (el instanceof HTMLInputElement && ['checkbox', 'radio', 'file', 'hidden'].includes(el.type)) continue;",
            "    if (el.disabled || el.readOnly) continue;",
            f"    await user.type(el, {json.dumps(TYPED, ensure_ascii=False)});",
            "    await settle();",
            "  }",
            "  expect(errors, `앱 오류: ${errors.join(' | ')}`).toEqual([]);",
            "  expect(container.childElementCount, '화면이 사라짐 — 렌더 오류로 앱이 내려감').toBeGreaterThan(0);",
            "});",
            "",
        ]
    config = f"""// 계약 적합성 스모크 전용 vitest 설정 — 빌드 파이프라인이 쓴다 (starter/conformance.py). 채점 설정과 같게, include 만 스모크.
import path from 'node:path';
import {{ fileURLToPath }} from 'node:url';
import react from '@vitejs/plugin-react';
import {{ loadConfigFromFile, type UserConfig }} from 'vite';
import {{ defineConfig }} from 'vitest/config';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');

export default defineConfig(async (env) => {{
  const loaded = await loadConfigFromFile(env, undefined, root).catch(() => null);
  const base: UserConfig = loaded?.config ?? {{}};
  const {{ test: _ignored, ...rest }} = base as UserConfig & {{ test?: unknown }};
  return {{
    ...rest,
    root,
    plugins: rest.plugins ?? [react()],
    test: {{
      environment: 'jsdom',
      include: ['{CONFORM_DIR}/**/*.test.{{ts,tsx}}'],
      setupFiles: ['{hl.harness}/grader-setup.ts'],
      testTimeout: 30_000,
      hookTimeout: 30_000,
    }},
  }};
}});
"""
    return [
        GeneratedFile(path=CONFORM_TEST, content="\n".join(lines)),
        GeneratedFile(path=CONFORM_CONFIG, content=config),
    ]
