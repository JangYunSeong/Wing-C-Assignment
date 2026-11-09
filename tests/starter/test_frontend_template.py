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
        # 계약의 응답 모양이 타입으로 — 모양이 다른 respond 는 tsc 가 잡는다 (2026-11-09 알파: 숨은 테스트가 예시의 { items } 로 응답)
        assert "export interface Product {\n  name: string;\n}" in f.content
        assert "export type GetApiProductsSuggestOk = { items: unknown[] };" in f.content
        assert "respond(json: GetApiProductsSuggestOk, status?: number)" in f.content
        assert "Promise<GetApiProductsSuggestCall>" in f.content


# ── 3단계: 템플릿이 고른 생성기 프롬프트 · 입력의 도우미 블록 · 수리 문구 (2026-11-09) ──


def _fe_inputs() -> Any:
    from task_spec.nodes.starter.generate import BuildInputs
    from tests.starter.test_contract import BACKEND_SEED
    from tests.starter.test_static_check import _approved

    return BuildInputs(
        task=_approved(), contract=_contract(), plan=_plan(), template=FE, seed=BACKEND_SEED
    )


def test_frontend_template_routes_every_generator_to_its_react_prompt() -> None:
    from pathlib import Path

    from task_spec.nodes.starter.generate import (
        accepted_input,
        fit_input,
        hidden_input,
        naive_input,
        public_input,
        starter_input,
    )
    from task_spec.prompting import PromptRegistry, assemble
    from task_spec.schemas import AcceptedDesign
    from tests.starter.test_contract import BACKEND_SEED

    reg = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
    b = _fe_inputs()
    starter = Overlay(kind="starter", files=(_f("src/App.tsx", "export default 1;\n"),))
    d1 = AcceptedDesign(
        design_id="d1", summary="응답을 입력과 대조", conditions="-", trap_ids=["t1"]
    )
    inputs = {
        "gen_starter": starter_input(b.task, b.contract, FE),
        "gen_public": public_input(b.task, b.contract, b.plan, FE),
        "gen_hidden": hidden_input(b.task, b.contract, b.plan, FE),
        "gen_naive": naive_input(b.task, b.contract, FE, starter, BACKEND_SEED),
        "gen_accepted": accepted_input(b.task, b.contract, FE, starter, d1),
    }
    assert set(FE.manifest.prompt_tasks) == set(inputs)
    for gen, inp in inputs.items():
        name = FE.manifest.prompt_tasks[gen]
        assert name == f"{gen}_react"
        t = reg.get(name)
        text = "\n".join(
            m.content
            for m in assemble(t, fit_input(inp, t.input_model), ontology_version="v").messages
        )
        assert "ContractCase" not in text and "schema.sql" not in text, gen
    # 숨은·공개 테스트 입력 끝에 이 과제의 contract.ts 가 ts 코드 블록으로
    for inp in (inputs["gen_hidden"], inputs["gen_public"]):
        api = inp.harness_api  # type: ignore[attr-defined]
        assert "## contract.ts — 이 과제의" in api and "```ts\n" in api and "```java" not in api
        assert "waitForGetApiProductsSuggest" in api


async def test_gen_hidden_calls_the_react_prompt_for_the_frontend_template() -> None:
    import json as _json

    from task_spec.llm import Completion, FakeBackend
    from task_spec.nodes.starter.generate import gen_hidden
    from tests.spec.test_feature import _client
    from tests.starter.test_generate import PROMPTS

    fake = FakeBackend().push(
        Completion(_json.dumps({"files": [{"path": f"{CASES}/hc1.test.tsx", "content": HC1}]}))
    )
    res = await gen_hidden(_fe_inputs(), llm=_client(fake), prompts=PROMPTS, ontology_version="o")
    assert res.overlay is not None and res.problems == []
    assert res.provenance[0].prompt.startswith("gen_hidden_react@")
    paths = {f.path for f in res.overlay.files}
    assert {f"{CASES}/contract.ts", "src/__grading__/grader-params.json"} <= paths


def test_repair_notes_use_template_terms() -> None:
    from task_spec.schemas.template import RepairTerms

    java = load_template("backend/java-spring-boot3-jpa").manifest.repair_terms
    assert java == RepairTerms() and java.reason_call == "Reasons.as"  # Java 문구 그대로
    fe = FE.manifest.repair_terms
    assert fe.reason_call.startswith("reason(") and fe.public_helper == "contract.ts"
    assert "ContractCase" not in fe.feature_first


def test_missing_vitest_imports_are_added_by_code() -> None:
    """2026-11-09 상류 평가: 숨은 테스트 3/3 이 beforeEach 를 import 하지 않아 첫 컴파일 실패."""
    from task_spec.starter.ts_helpers import ensure_vitest_imports

    no_before = (
        "import { describe, expect, it } from 'vitest';\n"
        "import { routeContract } from './contract';\n"
        "describe('hc1 x', () => {\n  beforeEach(() => routeContract());\n"
        "  it('y', () => { expect(1).toBe(1); });\n});\n"
    )
    none = "describe('pt1 a', () => { it('b', () => { expect('beforeEach(').toBe('x'); }); });\n"
    own = "const it = (s: string) => s;\nimport { describe } from 'vitest';\ndescribe('x', () => { it('y'); });\n"
    files, notes = ensure_vitest_imports(
        [
            _f(f"{CASES}/hc1.test.tsx", no_before),
            _f("src/__public__/pt1.test.tsx", none),
            _f("src/x.test.ts", own),
            _f("src/App.tsx", "export const beforeEach = 1;\n"),
        ]
    )
    got = {f.path: f.content for f in files}
    assert got[f"{CASES}/hc1.test.tsx"].startswith(
        "import { describe, expect, it, beforeEach } from 'vitest';\n"
    )
    assert got["src/__public__/pt1.test.tsx"].startswith(
        "import { describe, it, expect } from 'vitest';\n"
    )  # 문자열 안의 'beforeEach(' 는 쓰임이 아니다
    assert got["src/x.test.ts"] == own and got["src/App.tsx"] == "export const beforeEach = 1;\n"
    assert len(notes) == 2


def test_material_reasons_narrow_the_plan_input_and_check() -> None:
    """2026-11-09 상류 평가: 계약 계획이 숨은 요구(응답 경쟁)와 무관한 duplicate_request 케이스를 만들어 올바른 구현도 떨어짐."""
    from pathlib import Path

    from task_spec.nodes.starter.plan_contract import plan_input
    from task_spec.spec.incidents import feature_incident, load_incidents
    from task_spec.starter.contract import _reasons
    from tests.starter.test_static_check import _approved

    inc = feature_incident(
        load_incidents(Path(__file__).resolve().parents[2] / "incidents" / "public_v1"),
        "sw.frontend",
    )
    assert inc is not None and inc.feature is not None
    seed = inc.feature
    text = plan_input(_approved(), FE, seed=seed).failure_reasons
    assert "stale_response_shown" in text and "duplicate_request" not in text
    off = _plan().model_copy(
        update={
            "cases": [_plan().cases[0].model_copy(update={"intended_reason": "duplicate_request"})]
        }
    )
    assert _reasons(_plan(), FE.manifest, seed) == []
    assert any("duplicate_request" in p for p in _reasons(off, FE.manifest, seed))
    assert _reasons(off, FE.manifest) == []  # 재료 선언이 없으면 템플릿 목록 전체


def test_public_test_importing_its_contract_helper_is_not_a_leak() -> None:
    """2026-11-09 React 알파: 숨은 도우미 contract.ts 의 이름이 누설 낱말이 되어 공개 테스트의 './contract' import 를 막았다."""
    from task_spec.starter.hidden_helpers import render_contract_case
    from tests.starter.test_contract import _spec

    hidden_helper = render_contract_case(_contract(), _plan(), FE.manifest, audience="hidden")
    public_helper = render_contract_case(_contract(), _plan(), FE.manifest, audience="public")
    assert hidden_helper is not None and public_helper is not None
    pt1 = (
        "import { describe, expect, it } from 'vitest';\n"
        "import { renderApp } from '../__grading__/harness/grader';\n"
        "import { SCREEN } from './contract';\n"
        "describe('pt1 추천이 보인다', () => { it('a', () => { renderApp(SCREEN.S1); expect(1).toBe(1); }); });\n"
    )
    hid = Overlay(
        kind="hidden",
        files=(
            _f(f"{CASES}/hc1.test.tsx", HC1),
            hidden_helper,
            render_params(_plan(), FE.manifest),
        ),
    )
    pub = Overlay(kind="public_test", files=(_f("src/__public__/pt1.test.tsx", pt1), public_helper))
    res = static_check(
        [hid, pub], template=FE, plan=_plan(), spec=_spec(), contract=_contract(), seed=None
    )
    assert not [p.message for p in res.problems if "드러내는 말" in p.message]


def test_contract_field_types_map_to_typescript() -> None:
    from task_spec.starter.ts_helpers import ts_type

    ents = {"Product": "Product"}
    assert ts_type("string", ents) == "string" and ts_type("integer", ents) == "number"
    assert (
        ts_type("Product[]", ents) == "Product[]" and ts_type("list<Product>", ents) == "Product[]"
    )
    assert ts_type("enum(READY|SHIPPED)", ents) == '"READY" | "SHIPPED"'
    assert ts_type("enum(A|B)[]", ents) == '("A" | "B")[]'
    assert ts_type("weird-type", ents) == "unknown" and ts_type("datetime", ents) == "string"


def test_implementation_may_not_import_test_helpers() -> None:
    """2026-11-09 React 알파: 시작 코드·참조 구현이 공개 도우미의 응답 타입을 import — 공개 테스트가 없는 실행에서 컴파일 실패."""
    bad = (
        "import type { Product } from '../__public__/contract';\nexport const x: Product[] = [];\n"
    )
    worse = "import { api } from '../__grading__/harness/grader';\nexport const y = api;\n"
    ok = "import { load } from './client';\nexport const z = load;\n"
    msgs = _run(starter_extra=bad) + _run(starter_extra=worse)
    assert sum("채점·공개 테스트 폴더" in m for m in msgs) == 2
    assert not [m for m in _run(starter_extra=ok) if "채점·공개 테스트 폴더" in m]


def test_guessed_accessible_names_are_reasked() -> None:
    """2026-11-09 React 알파: 숨은 테스트가 계약에 없는 이름(/상품 검색/)으로 입력란을 찾아 모든 구현에서 예외."""
    guess = HC1.replace(
        "    renderApp();\n",
        "    renderApp();\n    await screen.findByRole('textbox', { name: /상품 검색/ });\n",
    ).replace(
        "import { describe", "import { screen } from '@testing-library/react';\nimport { describe"
    )
    assert any("접근 가능한 이름" in m for m in _run(guess))
    by_id = guess.replace(
        "findByRole('textbox', { name: /상품 검색/ })", "findByTestId('search-input')"
    )
    assert not any("접근 가능한 이름" in m for m in _run(by_id))
