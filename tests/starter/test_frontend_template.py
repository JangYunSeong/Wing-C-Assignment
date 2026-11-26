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
            # 뼈대 라우트를 남긴 App (v10~ 은 코드 뼈대 — static_check._skeleton_kept)
            _f(
                "src/App.tsx",
                'export default function App() { return <Route path="/search" element={<SearchPage />} />; }\n',
            ),
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
    assert select_template("sw.frontend").manifest.ref == "frontend/react-vite-ts@v15"  # type: ignore[union-attr]
    assert select_template("sw.backend").manifest.ref == "backend/java-spring-boot3-jpa@v6"  # type: ignore[union-attr]
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
        # 요청 기록도 계약 타입 — match 조건에서 call.body·call.params 를 좁히지 않고 읽는다 (2026-11-09 알파: body 가 unknown 이라 세 번 연속 컴파일 실패)
        assert (
            "export interface GetApiProductsSuggestRequest extends Omit<Call, 'body' | 'params'>"
            in f.content
        )
        assert "match?: (call: GetApiProductsSuggestRequest) => boolean" in f.content
        assert "  params: Record<string, string>;" in f.content and "  body: unknown;" in f.content


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
    # Java 문구 그대로 + 서버 쪽 예외 꼴은 매니페스트 (#176)
    assert (
        java.model_copy(update={"server_side": ""}) == RepairTerms()
        and java.reason_call == "Reasons.as"
    )
    assert "ServletException" in java.server_side
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
    from task_spec.spec.incidents import incident_by_id, load_incidents
    from task_spec.starter.contract import _reasons
    from tests.starter.test_static_check import _approved

    inc = incident_by_id(
        load_incidents(Path(__file__).resolve().parents[2] / "incidents" / "public_v1"),
        "pub:sw.frontend:stale-search-response",
    )
    assert inc is not None and inc.feature is not None
    seed = inc.feature
    from task_spec.nodes.starter.plan_contract import for_material

    # 계약 입력은 재료로 좁힌 템플릿을 받는다 — 사유를 입력에서 다시 거르지 않는다 (#179)
    text = plan_input(_approved(), for_material(FE, seed), seed=seed).failure_reasons
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
    assert any("계약 test id 밖으로 요소를 찾음" in m and "ByRole" in m for m in _run(guess))
    by_id = guess.replace(
        "findByRole('textbox', { name: /상품 검색/ })", "findByTestId('search-input')"
    )
    assert not any("test id 밖으로" in m for m in _run(by_id))


def test_element_lookups_outside_test_ids_are_one_rule() -> None:
    """#177: ByRole 이름·querySelector 를 사례마다 따로 잡아 글·레이블·자리표시자 쿼리와 closest()·getElementById 는 지나갔다 —
    test id 밖 요소 찾기 하나로. 문서가 권하는 `queryByRole('alert')` 는 둔다."""
    from task_spec.starter.static_check import element_guesses

    def hits(line: str) -> list[str]:
        return element_guesses(
            [_f("src/__grading__/cases/hc1.test.tsx", line)], _contract(), "숨은 테스트"
        )

    for guess in (
        "screen.getByText('결제 완료');",
        "within(list).getByText(item.name);",
        "await screen.findByLabelText('검색어');",
        "screen.getByPlaceholderText('검색');",
        "screen.getAllByRole('option');",
        "await screen.findByRole('textbox');",
        "input.closest('form');",
        "document.getElementById('q');",
        "form.querySelector(sel);",
    ):
        assert hits(guess), guess
    for fine in (
        "expect(screen.queryByRole('alert'), reason('x', 'y')).toBe(null);",
        "await screen.findByTestId(TID.SEARCH_INPUT);",
        "byTestId(item, TID.NAME);",
        "within(item).getByTestId(TID.NAME);",
        "expect(textsByRole('option', TID.LIST)).toEqual(['가상']);",
    ):
        assert not hits(fine), fine
    # 화면 계약이 없으면 보지 않는다
    assert element_guesses([_f("x.test.tsx", "screen.getByText('a');")], None, "숨은 테스트") == []


def test_head_meta_is_read_with_the_harness_not_selectors() -> None:
    """ct-9433f54e7d06-b1: 계약이 og:image·og:description meta 를 확인하는데 head 를 읽을 도구가 없어 공개·숨은 테스트가
    querySelector 로 찾다 막혔다 — v15 하네스 도구(document.title·headMeta)는 짐작이 아니고, 선택자로 찾으면 그 도구를 알린다."""
    from task_spec.starter.static_check import element_guesses

    def hits(line: str) -> list[str]:
        return element_guesses(
            [_f("src/__public__/pt3.test.tsx", line)], _contract(), "공개 테스트"
        )

    [meta] = hits("document.querySelector('meta[property=\"og:image\"]')?.getAttribute('content');")
    assert "headMeta(" in meta and "document.title" in meta
    [other] = hits("form.querySelector(sel);")  # head 와 무관한 선택자에는 붙이지 않는다
    assert "headMeta(" not in other
    for fine in (
        "await waitFor(() => expect(headMeta('og:description')).toBe('가상 소개'));",
        "expect(document.title).toContain('가상');",
    ):
        assert not hits(fine), fine


def test_harness_export_names_match_the_current_react_harness() -> None:
    """빠진 import 를 코드가 채우는 하네스 이름 표(HARNESS_EXPORTS)가 지금 템플릿 grader.ts 의 값 내보내기와 같다 — 하네스에 도구를
    더하고 표를 빠뜨리면 import 없이 쓴 케이스가 컴파일 재요청이 된다. 새 도구는 생성기가 받는 두 문서에도 있다."""
    import re

    from task_spec.starter.ts_helpers import HARNESS_EXPORTS

    template = select_template("sw.frontend")
    assert template is not None
    grader = (template.hidden / "harness" / "grader.ts").read_text("utf-8")
    names = {
        n.strip()
        for group in re.findall(r"export \{([^}]*)\} from", grader)
        for n in group.split(",")
        if n.strip() and not n.strip().startswith("type ")
    }
    assert names == set(HARNESS_EXPORTS)
    for doc in ("HARNESS.md", "PUBLIC.md"):
        assert "headMeta(" in (template.hidden / doc).read_text("utf-8"), doc


def test_frontend_contract_needs_are_checked_without_row_seeds() -> None:
    """2026-11-09 팩트 체크: 필요 요소 검사가 행 시드 템플릿에서만 돌아 프론트 재료(바꾸는 API·결과 화면)의 선언이 무시됐다."""
    from task_spec.schemas.incident import ContractNeed
    from task_spec.starter.contract import _needs
    from tests.starter.test_contract import BACKEND_SEED

    seed = BACKEND_SEED.model_copy(
        update={
            "contract_needs": [
                ContractNeed(kind="mutation_endpoint", note="주문을 만드는 API"),
                ContractNeed(kind="result_screen", note="주문 번호"),
            ]
        }
    )
    got = _needs(_contract(), _plan(), seed)  # GET 만 있는 계약
    assert any("바꾸는 API" in m for m in got) and not any("화면 요소" in m for m in got)
    post = _contract().model_copy(
        update={"endpoints": [_contract().endpoints[0].model_copy(update={"method": "POST"})]}
    )
    assert _needs(post, _plan(), seed) == []


def test_screens_with_route_variables_become_typed_functions() -> None:
    """2026-11-09 React 알파(중복 제출): 공개 테스트가 '/products/:productId' 를 변수 없이 열었다 — 함수로 두어 tsc 가 잡게."""
    from task_spec.starter.ts_helpers import screen_route

    assert screen_route("/cart") == '"/cart"'
    assert screen_route("/products/:productId") == (
        "(p: { productId: string }) => `/products/${encodeURIComponent(p.productId)}`"
    )
    assert screen_route("/a/{x}/b/:y").startswith("(p: { x: string; y: string }) =>")


def test_type_escapes_in_typescript_tests_are_reasked() -> None:
    """2026-11-09 React 알파: 'Call 에 json 이 없다'를 as unknown as 로 우회해 모든 구현에서 요청을 못 찾았다."""
    escaped = HC1.replace(
        "    renderApp();\n", "    renderApp();\n    const c = {} as unknown as { json: number };\n"
    )
    assert any("타입 검사를 우회" in m for m in _run(escaped))
    assert not any("타입 검사를 우회" in m for m in _run())


def test_ui_element_action_reaches_contract_text_and_helper() -> None:
    """조작 방법(action)은 계약 글의 '조작' 칸과 contract.ts 주석으로 — 테스트·구현이 같은 조작(요청 시점 포함)을 하게."""
    from task_spec.starter.render import contract_markdown
    from task_spec.starter.ts_helpers import render_ts_contract

    c = _contract()
    el = c.screens[0].elements[0].model_copy(update={"action": "입력할 때마다 추천 요청"})
    c = c.model_copy(update={"screens": [c.screens[0].model_copy(update={"elements": [el]})]})
    md = contract_markdown(c)
    assert "| test id | 역할 | 설명 | 조작 |" in md and "| 입력할 때마다 추천 요청 |" in md
    ts = render_ts_contract(c, FE.manifest, "hidden")
    assert (
        ts is not None
        and 'SEARCH_INPUT: "search-input" as TestId, // 조작: 입력할 때마다 추천 요청' in ts.content
    )


def test_hidden_terms_in_background_and_on_load_are_reasked() -> None:
    """#232: background('화면 조작 없이 나가는 요청')·화면 on_load('화면에 들어오면')는 README 에 그대로 나가는데 계약 누설 검사
    (contract_places)가 보지 않았다 — ui 계약은 background 가 필수라 프론트 과제는 늘 이 빈틈을 지났다."""
    from task_spec.schemas import PublicIncident
    from task_spec.starter.contract import check_contract
    from tests.starter.test_contract import ROOT

    raw = (ROOT / "incidents" / "public_v1" / "sw.frontend.offline-replay.json").read_text("utf-8")
    seed = PublicIncident.model_validate_json(raw).feature

    def leaks(c: TaskContract) -> list[str]:
        res = check_contract(c, _plan(), _spec(), FE.manifest, seed)
        return [p for p in res.problems if "숨은 요구·운영 조건을 드러내는 말이 공개 계약" in p]

    c = _contract().model_copy(update={"background": "브라우저 online 이벤트가 오면 메모를 보냄"})
    assert not leaks(c)
    bg = c.model_copy(update={"background": "다시 연결된 직후 대기열의 메모를 순서대로 보냄"})
    got = leaks(bg)
    assert len(got) == 1 and "다시 연결된 직후" in got[0] and "background" in got[0]
    screen = c.screens[0].model_copy(update={"on_load": "저장소의 메모 중 실패한 것만 다시 보냄"})
    got = leaks(c.model_copy(update={"screens": [screen]}))
    assert len(got) == 1 and "실패한 것만" in got[0] and "화면 /search 들어올 때" in got[0]


def test_contract_helper_tells_harness_success_status_and_test_ids() -> None:
    """2026-11-11 프론트 알파 fe8: 상태를 주지 않은 respond(본문)이 200 이라 계약이 201 인 생성 API 에서 `res.status === 201` 로
    확인하는 허용 구현만 떨어졌고, textsByRole('listitem', TID.X) 가 컴파일되지 않았다 — v8 은 contract.ts 가 하네스에 계약을 알린다."""
    from task_spec.starter.ts_helpers import render_ts_contract

    base = _contract().model_dump()
    post = dict(base["endpoints"][0]) | {
        "endpoint_id": "e2",
        "method": "POST",
        "path": "/api/memos/{memoId}/copies",
        "query": [],
        "request": [_field("content")],
        "responses": [
            {"status": 201, "description": "만듦", "body": [_field("id")]},
            {"status": 409, "description": "충돌", "body": [_field("code")]},
        ],
    }
    c = TaskContract.model_validate(base | {"endpoints": [*base["endpoints"], post]})
    for audience in ("hidden", "public"):
        ts = render_ts_contract(c, FE.manifest, audience)
        assert ts is not None
        assert "type CallHandle, type TestId }" in ts.content
        assert 'SEARCH_INPUT: "search-input" as TestId,' in ts.content
        assert '    "GET /api/products/suggest": 200,' in ts.content
        assert '    "POST /api/memos/:memoId/copies": 201,' in ts.content
        assert "  testIds: Object.values(TID)," in ts.content
    # 하네스에 api.contract 가 없는 판(v7)은 예전 그대로
    old = render_ts_contract(c, load_template("frontend/react-vite-ts", 7).manifest, "hidden")
    assert (
        old is not None and "api.contract" not in old.content and "type TestId" not in old.content
    )
    assert 'SEARCH_INPUT: "search-input",' in old.content


def test_list_screen_without_a_data_source_is_sent_back() -> None:
    """2026-11-10 프론트 알파 fe-v3-opt: 장바구니 화면에 목록을 읽는 API·저장소가 없어 테스트는 renderApp(경로, 초기 데이터) 를
    지어내고(TS2554) 구현은 샘플 항목을 코드에 박았다 — 계약 단계에서 처음 항목의 출처를 적게 한다."""
    from task_spec.starter.contract import _ui_data_source

    base = _contract().model_dump()
    base["screens"][0]["elements"].append(
        {"test_id": "cart-item", "role": "item", "description": "항목", "refs": ["r1"]}
    )
    patch = dict(base["endpoints"][0]) | {"method": "PATCH", "path": "/api/cart-items/{cartItemId}"}
    no_source = TaskContract.model_validate(base | {"endpoints": [patch]})
    problems = _ui_data_source(no_source)
    assert len(problems) == 1 and "/search" in problems[0] and "GET" in problems[0]
    # 그 화면이 부르는 읽기 API(GET)가 있으면 통과 — 같은 공개 요구를 refs 로 적었거나 화면 글이 그 경로를 말함(메서드 없어도)
    assert (
        _ui_data_source(TaskContract.model_validate(base)) == []
    )  # GET e1 과 화면이 r1 을 함께 적음
    other = dict(base["endpoints"][0]) | {"refs": ["r2"]}
    apart = base | {"endpoints": [other]}
    assert len(_ui_data_source(TaskContract.model_validate(apart))) == 1
    named = apart | {
        "screens": [base["screens"][0] | {"on_load": "/api/products/suggest?size=20 요청으로 목록"}]
    }
    assert _ui_data_source(TaskContract.model_validate(named)) == []
    # #180: 계약 어딘가에 POST 가 있다고 넘기지 않는다 — 목록과 무관한 결제 POST 만 있으면 출처가 없다
    pay = dict(base["endpoints"][0]) | {"method": "POST", "path": "/api/payments"}
    paid = base | {"endpoints": [patch, pay]}
    assert len(_ui_data_source(TaskContract.model_validate(paid))) == 1
    # 같은 refs 를 적은 브라우저 입력 환경(처음 내용)도 출처
    browser = paid | {
        "fixtures": [
            {"fixture_id": "f1", "kind": "browser", "summary": "처음 항목 2개", "refs": ["r1"]}
        ]
    }
    assert _ui_data_source(TaskContract.model_validate(browser)) == []
    stored = base | {"endpoints": [patch]}
    stored["screens"] = [
        base["screens"][0]
        | {
            "storage": [
                {"name": "cart", "kind": "localStorage", "shape": "항목 배열", "refs": ["r1"]}
            ]
        }
    ]
    assert _ui_data_source(TaskContract.model_validate(stored)) == []


def test_existing_test_must_wait_for_list_items_not_just_the_list() -> None:
    """2026-11-10 v3 알파 r2 사실 확인: 시작 코드의 기존 테스트가 목록 틀을 기다린 뒤 항목을 getBy 로 바로 찾아, 템플릿의
    TanStack Query 로 짠 허용 설계(항목이 한 박자 늦게 그려짐)가 기존 테스트에서만 떨어졌다."""
    from task_spec.starter.static_check import _code_pitfalls

    bad = (
        "it('목록', async () => {\n"
        "  render(<CartPage />);\n"
        "  const list = await screen.findByTestId('cart-list');\n"
        "  // 첫 항목\n"
        "  const item = within(list).getByTestId('cart-item');\n"
        "});\n"
    )
    good = bad.replace("within(list).getByTestId", "await within(list).findByTestId")
    found = [
        p
        for p in _code_pitfalls(
            Overlay(kind="starter", files=(_f("src/cart/CartPage.test.tsx", bad),)), FE
        )
        if "within("
        in p.message  # v5: 라우터 없이 화면을 그리는 꼴도 함께 걸린다 (그쪽은 따로 시험)
    ]
    assert (
        len(found) == 1
        and "findByTestId" in found[0].message
        and "CartPage.test.tsx:3" in found[0].message
    )
    assert not [
        p
        for p in _code_pitfalls(
            Overlay(kind="starter", files=(_f("src/cart/CartPage.test.tsx", good),)), FE
        )
        if "within(" in p.message
    ]


def test_ui_contract_401_needs_no_backend_auth_rule() -> None:
    """2026-11-10 프론트 알파 fe7-token: 토큰 만료 401 에 백엔드용 권한 칸(요청자 행·X-Actor 통과 값)을 요구해 계약 3번이 모두 거절됐다.
    화면 과제는 테스트가 서버 응답을 직접 정하므로 권한 칸·헤더 값 검사를 하지 않는다 — 서버가 판정하는 과제는 그대로."""
    from task_spec.starter.auth_rule import auth_problems
    from task_spec.starter.contract import SpecView, check_contract

    raw = _contract().model_dump()
    raw["endpoints"][0]["responses"].append(
        {"status": 401, "description": "접근 토큰 만료", "body": [_field("code")]}
    )
    ui = TaskContract.model_validate(raw)
    assert auth_problems(ui, seed_rows=False)  # 칸 검사 자체는 401 을 본다
    spec = SpecView(
        req_ids=("r1", "r2"),
        public_ac_ids=("p1",),
        hidden_acs={"h1": "t1"},
        trap_ids=("t1",),
        visible_texts=(),
    )
    msgs = check_contract(ui, _plan(), spec, FE.manifest).problems
    assert not [m for m in msgs if "auth" in m or "권한 거절" in m], msgs


def test_role_aliases_and_rejected_value_in_retry_message() -> None:
    """2026-11-10 fe7-dup: 목록 밖 역할을 3번 반복 — 흔한 이름은 맞는 역할로 받고, 그 밖은 재요청에 받은 값을 보여 준다."""
    import pytest
    from pydantic import ValidationError

    from task_spec.llm.client import _validation_summary
    from task_spec.schemas.build import UiElement

    el = {"test_id": "qty", "description": "수량", "refs": ["r1"]}
    assert UiElement.model_validate(el | {"role": "spinbutton"}).role == "input"
    assert UiElement.model_validate(el | {"role": "ListItem"}).role == "item"
    with pytest.raises(ValidationError) as e:
        UiElement.model_validate(el | {"role": "gauge"})
    assert "받은 값 'gauge'" in _validation_summary(e.value)
    with pytest.raises(
        ValidationError
    ) as e:  # 자유 글은 받은 값을 보이지 않는다 (원문이 섞일 수 있음)
        UiElement.model_validate(el | {"role": "수량 입력 상자"})
    assert "받은 값" not in _validation_summary(e.value)


def test_existing_test_rendering_a_screen_without_router_is_flagged() -> None:
    """2026-11-10 알파 fe7b-user: 시작 코드의 기존 테스트가 화면을 라우터 없이 그려, 구현이 useNavigate 를 쓰자 두 구현 모두 깼다."""
    from task_spec.starter.static_check import _code_pitfalls

    bad = "it('x', () => {\n  render(<LoginPage />);\n});\n"
    good = "it('x', () => {\n  render(<MemoryRouter initialEntries={['/login']}><LoginPage /></MemoryRouter>);\n});\n"
    found = _code_pitfalls(
        Overlay(kind="starter", files=(_f("src/pages/LoginPage.test.tsx", bad),)), FE
    )
    assert any("MemoryRouter" in p.message for p in found)
    assert not [
        p
        for p in _code_pitfalls(
            Overlay(kind="starter", files=(_f("src/pages/LoginPage.test.tsx", good),)), FE
        )
        if "MemoryRouter" in p.message
    ]


def test_all_refs_breaking_an_existing_test_outside_the_router_blame_the_starter_test() -> None:
    """같은 알파: 두 구현이 모두 기존 테스트를 'useNavigate() may be used only in the context of a <Router>' 로 깼는데 '계약 모호'로
    짚고 다시 만들지 않았다 — 시작 코드 테스트를 다시 만든다."""
    from task_spec.schemas import TargetRun
    from task_spec.starter.repair import _starter_test_context

    msg = "Error: useNavigate() may be used only in the context of a <Router> component."
    starter = TargetRun.model_validate(
        {
            "target": "starter",
            "kind": "starter",
            "setup": "ok",
            "tests": [{"group": "existing", "name": "pages/LoginPage.test.tsx", "result": "PASS"}],
        }
    )

    def ref(t: str, kind: str, m: str) -> TargetRun:
        return TargetRun.model_validate(
            {
                "target": t,
                "kind": kind,
                "setup": "ok",
                "tests": [
                    {
                        "group": "existing",
                        "name": "pages/LoginPage.test.tsx",
                        "result": "HARNESS_ERR",
                        "message": m,
                    }
                ],
            }
        )

    terms = FE.manifest.repair_terms
    acts = _starter_test_context(
        [ref("naive-all", "ref_naive", msg), ref("d1", "ref_accepted", msg)], starter, terms
    )
    assert [(a.generator, a.gate) for a in acts] == [
        ("gen_starter", "②")
    ] and "MemoryRouter" in acts[0].note
    other = "AssertionError: expected 1 to be 2"
    assert (
        _starter_test_context(
            [ref("naive-all", "ref_naive", other), ref("d1", "ref_accepted", other)], starter, terms
        )
        == []
    )
    assert (
        _starter_test_context(
            [ref("naive-all", "ref_naive", msg), ref("d1", "ref_accepted", other)], starter, terms
        )
        == []
    )


def test_textsbyrole_scope_overload_is_in_the_harness() -> None:
    src = (
        load_template("frontend/react-vite-ts", 5).hidden / "harness" / "grader-ui.tsx"
    ).read_text("utf-8")
    assert "export function textsByRole(scope: HTMLElement, role: string): string[];" in src
    # v8: 범위를 계약 test id(TestId)로도 — 글자 test id 두 개는 받지 않는다 (2026-11-11 알파 fe8-token)
    src = (FE.hidden / "harness" / "grader-ui.tsx").read_text("utf-8")
    assert (
        "export function textsByRole(role: string, scope: HTMLElement | TestId): string[];" in src
    )
    assert (
        "export function textsByRole(scope: HTMLElement | TestId, role: string): string[];" in src
    )


def test_test_checks_array_contains_vitest_imports_and_selector_guesses() -> None:
    """2026-11-10 알파 fe7b·fe7c·fe7d: 항목 글 배열에 toContain('글자') · waitFor 를 vitest 에서 import · querySelector 로 입력란 짐작."""
    from task_spec.starter.static_check import (
        array_substring_contains,
        element_guesses,
        misplaced_test_imports,
    )

    bad = (
        "import { describe, it, expect, waitFor } from 'vitest';\n"
        "const names = textsByRole('listitem');\n"
        "expect(names, reason('session_lost', '이름')).toContain('입출금');\n"
        "const input = form.querySelector('input[name=\"userId\"]');\n"
    )
    good = (
        "import { describe, it, expect } from 'vitest';\n"
        "import { waitFor } from '@testing-library/react';\n"
        "const names = textsByRole('listitem');\n"
        "expect(names.some((t) => t.includes('입출금')), reason('session_lost', '이름')).toBe(true);\n"
        "expect(names[0]).toContain('입출금');\n"
        "expect(names).toEqual(['입출금']);\n"
    )
    f_bad, f_good = (
        _f("src/__grading__/cases/hc1.test.tsx", bad),
        _f("src/__grading__/cases/hc1.test.tsx", good),
    )
    assert len(array_substring_contains([f_bad], "숨은 테스트")) == 1
    assert array_substring_contains([f_good], "숨은 테스트") == []
    assert "waitFor" in misplaced_test_imports([f_bad], "숨은 테스트")[0]
    assert misplaced_test_imports([f_good], "숨은 테스트") == []
    assert "querySelector" in element_guesses([f_bad], _contract(), "숨은 테스트")[0]
    assert element_guesses([f_good], _contract(), "숨은 테스트") == []
    # 화면 도구 이름은 testing-library 내보내기 표 하나에서 — act·cleanup 도 vitest 에서 가져오면 잡는다 (#177)
    act = "import { describe, it, act, cleanup } from 'vitest';\n"
    assert "act, cleanup" in misplaced_test_imports([_f("a.test.tsx", act)], "숨은 테스트")[0]


def test_form_without_its_fields_is_sent_back() -> None:
    """2026-11-10: 저장된 화면 계약에서 폼이 있는 화면 대부분이 폼 요소 하나만 적어 테스트가 입력란을 짐작했다."""
    from task_spec.starter.contract import _ui_form_fields

    base = _contract().model_dump()
    base["screens"][0]["elements"] = [
        {"test_id": "login-form", "role": "form", "description": "로그인", "refs": ["r1"]}
    ]
    problems = _ui_form_fields(TaskContract.model_validate(base))
    assert len(problems) == 1 and "입력란" in problems[0] and "제출 버튼" in problems[0]
    base["screens"][0]["elements"] += [
        {
            "test_id": "login-id",
            "role": "textbox",
            "description": "아이디",
            "action": "입력하면 값만 바뀜 — 요청은 버튼에서",
            "refs": ["r1"],
        },
        {
            "test_id": "login-submit",
            "role": "button",
            "description": "로그인",
            "action": "클릭하면 POST /api/auth/login",
            "refs": ["r1"],
        },
    ]
    assert _ui_form_fields(TaskContract.model_validate(base)) == []


def test_each_form_on_a_screen_needs_its_own_fields_and_button() -> None:
    """#248: 화면 전체의 요소 역할을 한데 모아, 완전한 폼(login-form) 옆의 입력란·버튼 없는 폼(profile-form)을 놓쳤다 —
    fit_forms 와 같은 소속(폼이 여럿이면 test id 접두)으로 폼마다 본다."""
    from task_spec.starter.contract import _ui_form_fields, fit_forms

    def write(eid: str, path: str, field: str) -> dict[str, Any]:
        return {
            "endpoint_id": eid,
            "method": "POST",
            "path": path,
            "summary": "저장",
            "request": [_field(field)],
            "responses": [{"status": 200, "description": "저장", "body": [_field("id")]}],
            "refs": ["r1"],
        }

    def el(test_id: str, role: str, action: str) -> dict[str, Any]:
        return {
            "test_id": test_id,
            "role": role,
            "description": test_id,
            "action": action,
            "refs": ["r1"],
        }

    login = [
        el("login-form", "form", "제출하면 POST /api/auth/login"),
        el("login-form-username", "textbox", "값을 입력한다"),
        el("login-form-submit", "button", "클릭하면 POST /api/auth/login"),
    ]
    profile = el("profile-form", "form", "제출하면 프로필을 저장한다")
    base = _contract().model_dump()
    base["endpoints"] += [
        write("e2", "/api/auth/login", "username"),
        write("e3", "/api/profile", "nickname"),
    ]
    base["screens"][0] |= {"route": "/account", "elements": [*login, profile]}
    c = TaskContract.model_validate(base)
    # 조작에 메서드·경로가 없고 본문을 받는 변경 API 가 둘 — fit_forms 가 고치지 못해 검사가 물어야 한다
    assert fit_forms(c) == (c, [])
    [msg] = _ui_form_fields(c)
    assert msg.startswith("화면 /account 의 폼(profile-form)에 입력란·제출 버튼 요소가 없음 — ")
    assert "login-form" not in msg
    # 완전한 폼만 빼면 예전과 같은 문장 (폼이 하나면 화면 전체가 그 폼의 것)
    alone = base | {"screens": [base["screens"][0] | {"elements": [profile]}]}
    assert _ui_form_fields(TaskContract.model_validate(alone)) == [msg]
    # 빠진 것이 다른 폼은 따로 — 버튼만 없는 폼에 입력란이 없다고 하지 않는다
    mixed = base | {"screens": [base["screens"][0] | {"elements": [*login[:2], profile]}]}
    got = _ui_form_fields(TaskContract.model_validate(mixed))
    assert len(got) == 2 and got[1] == msg
    assert got[0].startswith(
        "화면 /account 의 폼(login-form)에 제출 버튼 요소가 없음 — login-form 는 POST /api/auth/login 를 보낸다"
    )
    # 둘째 폼에도 제 입력란·버튼(접두 profile-)이 있으면 문제 없음
    base["screens"][0]["elements"] += [
        el("profile-nickname", "textbox", "값을 입력한다"),
        el("profile-save", "button", "클릭하면 POST /api/profile"),
    ]
    assert _ui_form_fields(TaskContract.model_validate(base)) == []


def _login_contract(
    action: str | None, request: list[dict[str, Any]], extra: list[dict[str, Any]] = ()
) -> TaskContract:
    base = _contract().model_dump()
    login = dict(base["endpoints"][0]) | {
        "endpoint_id": "e2",
        "method": "POST",
        "path": "/api/auth/login",
        "query": [],
        "request": request,
        "responses": [{"status": 200, "description": "로그인", "body": [_field("token")]}],
    }
    base["endpoints"] = [*base["endpoints"], login]
    base["screens"] = [
        *base["screens"],
        {
            "screen_id": "s2",
            "route": "/login",
            "summary": "로그인",
            "elements": [
                {
                    "test_id": "login-form",
                    "role": "form",
                    "description": "로그인 폼(아이디, 비밀번호 입력 및 로그인 버튼)",
                    "action": action,
                    "refs": ["r1"],
                },
                *extra,
            ],
            "refs": ["r1"],
        },
    ]
    return TaskContract.model_validate(base)


def test_form_fields_are_filled_from_the_request_it_sends() -> None:
    """2026-11-11 알파 fe8-user: 로그인 폼(조작 'POST /api/auth/login', 요청 칸 username·password)에 입력란도 버튼도 없어 계약 답 3개가
    모두 막혔다 — 폼이 적은 제출 요청의 칸에서 코드가 입력란·제출 버튼을 채운다."""
    from task_spec.starter.contract import _ui_form_fields, fit_forms

    action = "로그인 버튼 클릭 시 POST /api/auth/login 요청, 성공하면 /orders 로 이동"
    c = _login_contract(
        action,
        [
            _field("username"),
            _field("password"),
            _field("requestId"),
            _field("loggedAt", "datetime"),
        ],
    )
    assert _ui_form_fields(c) != []
    fitted, notes = fit_forms(c)
    els = {el.test_id: el for el in fitted.screens[1].elements}
    assert [el.test_id for el in fitted.screens[1].elements] == [
        "login-form",
        "login-form-username",
        "login-form-password",
        "login-form-submit",
    ]  # 앱이 만드는 요청 번호·시각은 입력란이 아니다
    assert (
        els["login-form-username"].role == "textbox" and els["login-form-password"].role == "input"
    )
    assert els["login-form-submit"].role == "button" and els["login-form-submit"].action == action
    assert all(el.refs == ["r1"] and el.action for el in fitted.screens[1].elements)
    assert len(notes) == 2 and _ui_form_fields(fitted) == []
    assert fitted.screens[0] == c.screens[0]  # 폼 없는 화면은 그대로
    # 다시 돌려도 같다 (결정적·멱등)
    assert fit_forms(fitted) == (fitted, [])


def test_form_without_a_request_body_becomes_a_button_and_unknown_requests_are_asked_back() -> None:
    from task_spec.starter.contract import _ui_form_fields, fit_forms

    # 본문 없는 요청을 보내는 폼 — 입력란이 필요 없다: 버튼이 없으면 같은 test id 의 버튼으로
    c = _login_contract("클릭하면 POST /api/auth/login", [])
    fitted, notes = fit_forms(c)
    assert [(el.test_id, el.role) for el in fitted.screens[1].elements] == [
        ("login-form", "button")
    ]
    assert notes and _ui_form_fields(fitted) == []
    # 버튼이 따로 있으면 폼은 묶음(region)으로 — test id 는 그대로 (다른 곳이 가리킬 수 있다)
    btn = {
        "test_id": "login-go",
        "role": "button",
        "description": "로그인",
        "action": "클릭하면 POST /api/auth/login",
        "refs": ["r1"],
    }
    fitted, _ = fit_forms(_login_contract("클릭하면 POST /api/auth/login", [], [btn]))
    assert [(el.test_id, el.role) for el in fitted.screens[1].elements] == [
        ("login-form", "region"),
        ("login-go", "button"),
    ]
    # 다른 요청을 적은 폼·요청 칸이 모두 앱이 채우는 칸인 폼은 그대로 — 검사가 그 요청과 칸을 짚어 다시 묻는다
    c = _login_contract("제출하면 POST /api/auth/refresh", [_field("username")])
    assert fit_forms(c) == (c, [])
    assert "폼 요소의 조작(action)에 제출할 때 나가는 요청을" in _ui_form_fields(c)[0]
    c = _login_contract("클릭하면 POST /api/auth/login", [_field("refreshToken")])
    assert fit_forms(c) == (c, [])
    assert (
        "login-form 는 POST /api/auth/login 를 보낸다 — 요청 칸 refreshToken"
        in _ui_form_fields(c)[0]
    )


def test_form_without_a_named_request_uses_the_only_write_api() -> None:
    """2026-11-10 fe7c-user: 조작이 '제출하면 로그인 요청을 보내고…' 로 메서드·경로가 없었다 — 본문을 받는 변경 API 가 하나뿐이면 그것.
    경로 변수(화면·API)는 앱이 채우므로 입력란이 아니다."""
    from task_spec.starter.contract import fit_forms

    c = _login_contract("입력 후 제출하면 로그인 요청", [_field("userId"), _field("password")])
    fitted, _ = fit_forms(c)
    assert [el.test_id for el in fitted.screens[1].elements] == [
        "login-form",
        "login-form-user-id",
        "login-form-password",
        "login-form-submit",
    ]
    base = c.model_dump()
    base["endpoints"].append(
        base["endpoints"][1] | {"endpoint_id": "e3", "path": "/api/auth/other"}
    )
    two = TaskContract.model_validate(base)
    assert fit_forms(two) == (two, [])  # 변경 API 가 둘이면 짐작하지 않는다
    base = c.model_dump()
    base["screens"][1]["route"] = "/sites/:siteId/login"
    base["endpoints"][1]["request"] = [_field("siteId"), _field("password")]
    fitted, _ = fit_forms(TaskContract.model_validate(base))
    assert [el.test_id for el in fitted.screens[1].elements][1:] == [
        "login-form-password",
        "login-form-submit",
    ]


def test_form_fitting_edges_particles_several_forms_cap_and_names() -> None:
    """리뷰 2026-11-11: 경로 뒤 한글 조사 · 한 화면의 폼 둘 · 요소 30개 상한 · 영문이 없는 칸 이름."""
    from task_spec.starter.contract import fit_forms

    # 'POST /api/auth/login으로' — 조사를 경로로 먹지 않는다
    c = _login_contract("제출하면 POST /api/auth/login으로 요청.", [_field("username")])
    fitted, _ = fit_forms(c)
    assert [el.test_id for el in fitted.screens[1].elements] == [
        "login-form",
        "login-form-username",
        "login-form-submit",
    ]
    # 폼 둘 — 첫 폼을 채워도 둘째 폼도 채운다 (폼마다 그 폼의 요소로 본다)
    base = c.model_dump()
    base["endpoints"].append(
        base["endpoints"][1]
        | {"endpoint_id": "e3", "path": "/api/auth/signup", "request": [_field("email")]}
    )
    base["screens"][1]["elements"].append(
        {
            "test_id": "signup-form",
            "role": "form",
            "description": "가입",
            "action": "제출하면 POST /api/auth/signup",
            "refs": ["r1"],
        }
    )
    fitted, _ = fit_forms(TaskContract.model_validate(base))
    ids = {el.test_id for el in fitted.screens[1].elements}
    assert {
        "login-form-username",
        "login-form-submit",
        "signup-form-email",
        "signup-form-submit",
    } <= ids
    # 요소 30개 상한 — 넘기지 않는다 (입력란이 있고 버튼만 없는 화면)
    full = c.model_dump()
    full["screens"][1]["elements"] += [
        {
            "test_id": f"login-note-{i}",
            "role": "textbox",
            "description": "칸",
            "action": "입력",
            "refs": ["r1"],
        }
        for i in range(29)
    ]
    capped, _ = fit_forms(TaskContract.model_validate(full))
    assert len(capped.screens[1].elements) == 30
    # 영문이 없는 칸 이름은 입력란으로 만들지 않는다 (test id 가 '-' 로 끝나지 않게)
    odd = _login_contract("제출하면 POST /api/auth/login", [_field("username")])
    req = odd.endpoints[1].request[0].model_copy(update={"name": "아이디"})
    login = odd.endpoints[1].model_copy(update={"request": [req]})
    odd = odd.model_copy(update={"endpoints": [odd.endpoints[0], login]})
    assert fit_forms(odd) == (odd, [])


def test_moving_screens_with_window_location_is_sent_back() -> None:
    """2026-11-11 알파 fe9-token: 허용·흔한 구현이 모두 window.location.assign('/login') 으로 옮기려다 jsdom 에서 주소가 안 바뀜."""
    from task_spec.starter.static_check import _code_pitfalls

    for bad in (
        "window.location.assign('/login');",
        "location.replace('/login')",
        "window.location.href = '/login';",
    ):
        files = (_f("src/api/auth.ts", bad),)
        found = _code_pitfalls(Overlay(kind="ref_accepted", files=files), FE)
        assert any("라우터로" in p.message for p in found), bad
    ok = "if (window.location.href === '/x') navigate('/login');"
    files = (_f("src/api/auth.ts", ok),)
    assert not _code_pitfalls(Overlay(kind="ref_accepted", files=files), FE)


def test_v6_harness_types_bodies_and_server_state() -> None:
    src = (FE.hidden / "harness" / "grader-api.ts").read_text("utf-8")
    assert "serverState<T = Body>(path: string): T[]" in src
    assert "once<B = Body>(" in src and "calls<B = Body>(" in src


def test_contract_helper_tells_harness_numeric_id_lists_v12() -> None:
    """2026-11-12 #102 검토: 하네스 목록 id 가 늘 글자라 숫자 id 계약에서 `===` 비교가 어긋났다 (저장 화면 계약 엔티티 id 4개 중 1개꼴이 숫자)."""
    from task_spec.starter.ts_helpers import id_kinds, render_ts_contract

    base = _contract().model_dump()
    base["entities"] = [
        {"entity_id": "m1", "name": "Memo", "fields": [_field("id", "integer"), _field("note")]},
        {"entity_id": "m2", "name": "Tag", "fields": [_field("id", "string")]},
    ]
    memo_post = dict(base["endpoints"][0]) | {
        "endpoint_id": "e2",
        "method": "POST",
        "path": "/api/memos",
        "query": [],
        "request": [_field("note")],
        "responses": [{"status": 201, "description": "만듦", "returns": "m1"}],
    }
    memo_get = memo_post | {
        "endpoint_id": "e3",
        "method": "GET",
        "path": "/api/memos/{memoId}",
        "request": [],
        "responses": [{"status": 200, "description": "하나", "returns": "m1"}],
    }
    tag_get = memo_get | {
        "endpoint_id": "e4",
        "path": "/api/tags",
        "responses": [{"status": 200, "description": "목록", "returns": "m2[]"}],
    }
    base["endpoints"] = [*base["endpoints"], memo_post, memo_get, tag_get]
    base["covers"] = {"r1": ["e1", "m1", "s1"], "r2": ["e1"]}
    c = TaskContract.model_validate(base)
    assert id_kinds(c) == {
        "/api/memos": "number"
    }  # 항목 경로는 목록 경로로, 글자 id 는 알리지 않음
    ts = render_ts_contract(c, FE.manifest, "hidden")
    assert (
        ts is not None
        and '    "/api/memos": "number",' in ts.content
        and "idKinds: {" in ts.content
    )
    old = render_ts_contract(c, load_template("frontend/react-vite-ts", 11).manifest, "hidden")
    assert old is not None and "idKinds" not in old.content  # v11 하네스는 idKinds 를 모른다


def test_missing_harness_and_testing_library_imports_are_added_by_code() -> None:
    """2026-11-12 #102 검토: 컴파일 기록 Cannot find name 'api' 12번 — 재요청 없이 코드가 import 를 넣는다.
    screen 은 import 가 없으면 브라우저 전역(window.screen)으로 읽혀 TS2339 — 쓰는지로 판단한다."""
    from task_spec.starter.ts_helpers import ensure_test_imports

    hidden = (
        "import { it } from 'vitest';\nimport {\n  renderApp,\n} from '../harness/grader';\n"
        "it('x', async () => {\n  renderApp();\n  api.route('GET', '/a');\n"
        "  await waitFor(() => screen.getByTestId('t'));\n  const w = window.screen.width;\n});\n"
    )
    public = "import { it } from 'vitest';\nit('p', async () => { await screen.findByTestId('x'); network.goOffline(); });\n"
    mine = (
        "import { it } from 'vitest';\nimport { screen } from '@testing-library/react';\n"
        "const textOf = (x: string) => x;\nit('m', () => { screen.getByText(textOf('a')); });\n"
    )
    files, notes = ensure_test_imports(
        [
            _f(f"{CASES}/hc1.test.tsx", hidden),
            _f("src/__public__/pt1.test.tsx", public),
            _f("src/__public__/pt2.test.tsx", mine),
        ],
        "src/__grading__/harness/grader",
    )
    h, p, m = (f.content for f in files)
    assert "import { renderApp, api } from '../harness/grader';" in h  # 있던 하네스 import 에 더함
    assert "import { screen, waitFor } from '@testing-library/react';" in h
    assert (
        "import { network } from '../__grading__/harness/grader';" in p
    )  # 파일 위치 기준 상대 경로
    assert "import { screen } from '@testing-library/react';" in p
    assert m == mine  # 직접 정의한 이름·이미 가져온 이름은 건드리지 않음
    assert len(notes) == 2
    # #177: screen·waitFor·within 셋만이 아니라 testing-library 내보내기 표 전부 — act·cleanup·fireEvent(속성·호출 둘 다)
    tools = (
        "import { it } from 'vitest';\n"
        "it('t', async () => { await act(async () => {}); fireEvent.click(document.body); cleanup(); });\n"
    )
    [t], _ = ensure_test_imports(
        [_f(f"{CASES}/hc2.test.tsx", tools)], "src/__grading__/harness/grader"
    )
    assert "import { act, cleanup, fireEvent } from '@testing-library/react';" in t.content


def test_bare_body_in_reply_slot_is_wrapped_by_code() -> None:
    """api.once·always 의 응답 자리에 응답 칸(status·json·networkError) 없이 본문을 바로 넣으면 { json: … } 으로 감싼다.
    응답 칸이 있거나·펼침·빈 객체·변수면 모양을 모르니 그대로 (tsc·재요청에 맡김)."""
    from task_spec.starter.ts_helpers import wrap_bare_replies

    src = (
        "api.once('POST', '/m', { id: 1, note: 'a, b', tags: ['x', { y: 1 }] }, (c) => c.body.note === 'B');\n"
        "api.always('GET', '/m', { status: 200, json: [] });\n"
        "api.once('POST', '/m', { ...base });\n"
        "api.once('POST', '/m', {});\n"
        "api.once('POST', '/m', memo);\n"
        "api.always<{ a: number }>('GET', '/n', { a: 1 });\n"
    )
    out, n = wrap_bare_replies(src)
    assert n == 2
    assert "{ json: { id: 1, note: 'a, b', tags: ['x', { y: 1 }] } }, (c) =>" in out
    assert "api.always<{ a: number }>('GET', '/n', { json: { a: 1 } });" in out
    for kept in ("{ status: 200, json: [] }", "{ ...base }", "'/m', {})", "'/m', memo)"):
        assert kept in out


def test_input_without_action_is_filled_only_when_one_submit_target() -> None:
    """2026-11-15 c0df 재빌드: 로그인 화면 입력란 login-email·login-password 에 조작 방법이 없어 계약이 막혔다 — 요청을 보내는 제출 대상이
    하나뿐인 화면의 글자·숫자 입력란만 '제출 전에는 요청 없음'으로 채운다. 검색 입력·제출 대상이 둘인 화면은 그대로 (검사가 묻는다)."""
    from task_spec.starter.contract import _FIELD_ACTION, _ui_actions, fit_field_actions

    def el(
        test_id: str, role: str, action: str | None = None, desc: str = "가상 요소"
    ) -> dict[str, Any]:
        return {
            "test_id": test_id,
            "role": role,
            "description": desc,
            "action": action,
            "refs": ["r1"],
        }

    submit = "클릭하면 POST /api/auth/login 요청, 성공하면 /orders 로 이동"
    c = _login_contract(
        submit,
        [_field("email"), _field("password")],
        [
            el("login-email", "textbox"),
            el("login-password", "input"),
            el("login-submit", "button", submit),  # 폼과 같은 API — 제출 대상 하나
            el("login-keyword", "searchbox"),  # 검색칸 역할은 건드리지 않음
            el("login-memo", "textbox", desc="검색어 입력"),  # 검색으로 보이는 입력은 짐작하지 않음
        ],
    )
    fitted, notes = fit_field_actions(c)
    els = {e.test_id: e for e in fitted.screens[1].elements}
    assert (
        els["login-email"].action == _FIELD_ACTION and els["login-password"].action == _FIELD_ACTION
    )
    assert els["login-keyword"].action is None and els["login-memo"].action is None
    assert len(notes) == 1 and "login-email, login-password" in notes[0]
    assert fitted.screens[0] == c.screens[0]
    assert fit_field_actions(fitted) == (fitted, [])  # 멱등
    assert not any("login-email" in p for p in _ui_actions(fitted))

    # 제출 대상이 둘(다른 API)이면 어느 쪽에서 요청이 나가는지 모른다 — 그대로
    two = _login_contract(
        submit,
        [_field("email")],
        [
            el("login-email", "textbox"),
            el("login-reset", "button", "클릭하면 POST /api/auth/reset 요청"),
        ],
    )
    assert fit_field_actions(two) == (two, [])
