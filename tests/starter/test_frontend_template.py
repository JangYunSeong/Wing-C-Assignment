"""프론트엔드 React 템플릿을 파이프라인에 연결 (2026-11-09) — 템플릿 선택·조절값 JSON·Java 전용 산출물 제외·정적 검사. 가상 데이터만."""

from __future__ import annotations

import json
from typing import Any

from task_spec.schemas import GeneratedFile, HiddenPlan, TaskContract
from task_spec.starter.hidden_helpers import render_persistence_check
from task_spec.starter.render import render_params
from task_spec.starter.static_check import Overlay, static_check
from task_spec.starter.templates import is_java, load_template, select_template
from tests.starter.test_contract import _spec

FE = load_template("frontend/react-vite-ts")
CASES = "src/__grading__/cases"


def _field(name: str, typ: str = "string") -> dict[str, Any]:
    return {"name": name, "type": typ, "refs": ["r1"]}


def _contract() -> TaskContract:
    return TaskContract.model_validate(
        {
            "interface_kind": "ui",
            "endpoints": [
                {
                    "endpoint_id": "e1",
                    "method": "GET",
                    "path": "/api/products/suggest",
                    "summary": "검색 추천",
                    "query": [_field("q")],
                    "responses": [
                        {"status": 200, "description": "추천", "body": [_field("items", "array")]}
                    ],
                    "refs": ["r1", "p1"],
                }
            ],
            "entities": [{"entity_id": "m1", "name": "Product", "fields": [_field("name")]}],
            "screens": [
                {
                    "screen_id": "s1",
                    "route": "/search",
                    "summary": "상품 검색",
                    "elements": [
                        {
                            "test_id": "search-input",
                            "role": "combobox",
                            "description": "검색어",
                            "refs": ["r1"],
                        }
                    ],
                    "refs": ["r1", "p1"],
                }
            ],
            "public_tests": [
                {"test_id": "pt1", "ac_id": "p1", "checks": "검색어를 치면 추천이 보인다"}
            ],
            "covers": {"r1": ["e1", "m1", "s1"], "r2": ["e1"]},
        }
    )


def _plan() -> HiddenPlan:
    return HiddenPlan.model_validate(
        {
            "harness": {},
            "cases": [
                {
                    "case_id": "hc1",
                    "ac_id": "h1",
                    "trap_id": "t1",
                    "scenario": "먼저 보낸 검색의 응답을 나중에",
                    "expect": "최신 입력의 추천이 남는다",
                    "intended_reason": "stale_response_shown",
                }
            ],
        }
    )


def _f(path: str, content: str) -> GeneratedFile:
    return GeneratedFile(path=path, content=content)


HC1 = (
    "import { describe, expect, it } from 'vitest';\n"
    "import { api, reason, renderApp } from '../harness/grader';\n"
    "describe('hc1 늦은 응답', () => {\n"
    "  it('최신 입력', async () => {\n"
    "    renderApp();\n"
    "    expect(1, reason('stale_response_shown', '목록이 바뀜')).toBe(1);\n"
    "  });\n"
    "});\n"
)


def _run(hidden: str = HC1, starter_extra: str = "") -> list[str]:
    starter = Overlay(
        kind="starter",
        files=(
            _f("src/App.tsx", "export default function App() { return <div />; }\n"),
            _f("src/api/products.ts", starter_extra or "export const x = 1;\n"),
        ),
    )
    hid = Overlay(
        kind="hidden",
        files=(_f(f"{CASES}/hc1.test.tsx", hidden), render_params(_plan(), FE.manifest)),
    )
    res = static_check(
        [starter, hid], template=FE, plan=_plan(), spec=_spec(), contract=_contract(), seed=None
    )
    return [p.message for p in res.problems]


def test_template_is_selected_by_job_family() -> None:
    assert select_template("sw.frontend").manifest.ref == "frontend/react-vite-ts@v1"  # type: ignore[union-attr]
    assert select_template("sw.backend").manifest.ref == "backend/java-spring-boot3-jpa@v1"  # type: ignore[union-attr]
    assert select_template("infra.dba") is None
    assert not is_java(FE.manifest) and is_java(select_template("sw.backend").manifest)  # type: ignore[union-attr]


def test_params_file_is_json_for_the_frontend_template() -> None:
    f = render_params(_plan(), FE.manifest)
    assert f.path == "src/__grading__/grader-params.json" and json.loads(f.content) == {}


def test_java_only_persistence_check_is_not_written_into_a_typescript_project() -> None:
    assert render_persistence_check(_contract(), FE.manifest) is None


def test_typescript_overlays_pass_and_rules_still_bite() -> None:
    assert _run() == []
    # 지원자 구현 내부 import (공개 계약인 화면으로만 판정)
    inner = HC1.replace(
        "import { api, reason", "import { search } from '../../api/products';\nimport { api, reason"
    )
    assert any("지원자 구현 내부" in m for m in _run(inner))
    # 템플릿에 없는 라이브러리
    assert any(
        "템플릿에 없는 라이브러리" in m for m in _run(starter_extra="import axios from 'axios';\n")
    )
    # 목록에 없는 실패 사유 · 케이스 id 빠짐
    assert any(
        "not_a_reason" in m for m in _run(HC1.replace("stale_response_shown", "not_a_reason"))
    )
    assert any("hc1" in m for m in _run(HC1.replace("hc1 늦은 응답", "늦은 응답")))


def test_contract_helper_is_written_for_hidden_and_public_typescript_tests() -> None:
    from task_spec.starter.hidden_helpers import render_contract_case
    from task_spec.starter.ts_helpers import msw_path

    assert msw_path("/api/items/{itemId}/notes") == "/api/items/:itemId/notes"
    assert msw_path("/api/items/:id") == "/api/items/:id"
    hidden = render_contract_case(_contract(), _plan(), FE.manifest, audience="hidden")
    public = render_contract_case(_contract(), _plan(), FE.manifest, audience="public")
    assert hidden is not None and public is not None
    assert hidden.path == f"{CASES}/contract.ts" and public.path == "src/__public__/contract.ts"
    assert 'from "../harness/grader"' in hidden.content
    assert 'from "../__grading__/harness/grader"' in public.content
    for f in (hidden, public):
        assert 'SEARCH_INPUT: "search-input"' in f.content
        assert 'S1: "/search"' in f.content
        assert (
            'GET_API_PRODUCTS_SUGGEST: { method: "GET", path: "/api/products/suggest" }'
            in f.content
        )
        assert 'api.route("GET", "/api/products/suggest");' in f.content
        assert "export function waitForGetApiProductsSuggest(" in f.content
        assert "(쿼리: q)" in f.content
