"""TypeScript 계약 도우미 — 프론트엔드 템플릿의 숨은·공개 테스트가 import 하는 `contract.ts` 를 계약에서 코드가 쓴다 (LLM 없음).

Java 템플릿의 ContractCase·PublicCase(starter/hidden_helpers.py)와 같은 자리: 테스트가 test id·화면 경로·API 경로를 손으로 쓰지 않게
상수와 함수로 준다. 없는 이름을 쓰면 tsc 컴파일 루프가 잡는다 (build.compile_and_fix). 2026-11-09 React 템플릿 파이프라인 연결.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from task_spec.schemas.build import GeneratedFile, TaskContract
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.hidden_helpers import method_name
from task_spec.starter.tables import spring_snake

_SEG = re.compile(r"\{([A-Za-z][A-Za-z0-9]*)\}")


def msw_path(path: str) -> str:
    """계약 경로 → MSW 꼴: '/api/items/{id}' → '/api/items/:id' (':id' 는 그대로)."""
    return _SEG.sub(r":\1", path)


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
    for sc in contract.screens:
        for el in sc.elements:
            tids.setdefault(_const(el.test_id), el.test_id)
    lines.append(
        "/** 화면 요소 test id (계약 screens) — screen.getByTestId(TID.X) 또는 byTestId(TID.X) */"
    )
    lines.append("export const TID = {")
    lines += [f"  {k}: {_ts(v)}," for k, v in tids.items()]
    lines += ["} as const;", ""]
    lines.append("/** 화면 경로 (계약 screens) */")
    lines.append("export const SCREEN = {")
    lines += [
        f"  {_const(sc.screen_id)}: {_ts(sc.route)}, // {sc.summary}" for sc in contract.screens
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
    for e in endpoints:
        q = ", ".join(f.name for f in e.query)
        lines += [
            "",
            f"/** {e.endpoint_id} {e.method} {e.path} — {e.summary}"
            + (f" (쿼리: {q})" if q else "")
            + " */",
            f"export function waitFor{method_name(e)[:1].upper()}{method_name(e)[1:]}(match?: (call: Call) => boolean): Promise<CallHandle> {{",
            f"  return api.waitForCall({_ts(e.method or '')}, {_ts(msw_path(e.path))}, match);",
            "}",
        ]
    return GeneratedFile(path=rule.path, content="\n".join(lines) + "\n")
