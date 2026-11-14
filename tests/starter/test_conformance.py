"""계약 적합성 스모크 — 계약 모양 예시 응답·화면별 검사 생성, 수리 게이트 ⓪ 의 탓 정하기. 가상 데이터만."""

from __future__ import annotations

import json
import re

import pytest

from task_spec.schemas import TargetRun, TaskContract, VerifyReport
from task_spec.starter.conformance import (
    CONFORM_CONFIG,
    CONFORM_TEST,
    _Examples,
    render_conformance,
    type_kind,
)
from task_spec.starter.repair import assign
from task_spec.starter.templates import load_template
from tests.starter.test_frontend_template import FE, _contract
from tests.starter.test_repair import GOOD_ACC, GOOD_NAIVE, MISSED, OVERLAYS, PLAN2, _run, _t


@pytest.mark.parametrize(
    ("typ", "kind"),
    [
        ("string", "string"),
        ("VARCHAR", "string"),
        ("bigint", "integer"),
        ("Long", "integer"),
        ("timestamp", "datetime"),
        ("LocalDateTime", "datetime"),
        ("BigDecimal", "number"),
        ("enum(READY|SHIPPED)", "enum"),
        ("m2[]", "list"),
        ("array<Notification>", "list"),
        ("m1", "ref"),
    ],
)
def test_free_form_contract_types_are_grouped_by_meaning(typ: str, kind: str) -> None:
    """저장 계약의 필드 타입은 생성기마다 다르게 쓴다 (2026-11-10 집계) — 예시 값을 만들 때만 뜻으로 묶는다."""
    assert type_kind(typ) == kind


def _list_contract() -> TaskContract:
    """응답이 엔티티 배열인 계약 — 2026-11-10 fefix e99cbba0 와 같은 모양."""
    d = _contract().model_dump(mode="json")
    d["entities"][0]["fields"] = [
        {"name": "id", "type": "integer", "refs": ["r1"]},
        {"name": "name", "type": "string", "refs": ["r1"]},
        {"name": "status", "type": "enum(ON|OFF)", "refs": ["r1"]},
    ]
    d["endpoints"][0]["responses"] = [
        {"status": 200, "description": "추천", "returns": "m1[]"},
        {
            "status": 500,
            "description": "오류",
            "body": [{"name": "message", "type": "string", "refs": ["r1"]}],
        },
    ]
    return TaskContract.model_validate(d)


def test_example_response_has_the_contract_shape() -> None:
    c = _list_contract()
    ex = _Examples(c)
    body = ex.response(c.endpoints[0].responses[0])
    assert isinstance(body, list) and body[0]["id"] == 1 and body[0]["status"] == "ON"


def test_smoke_routes_every_endpoint_with_its_success_example_and_types_inputs() -> None:
    files = {f.path: f.content for f in render_conformance(_list_contract(), FE.manifest)}
    assert set(files) == {CONFORM_TEST, CONFORM_CONFIG}
    test = files[CONFORM_TEST]
    m = re.search(r"const ROUTES: .*? = (\[.*\]);", test)
    assert m is not None
    routes = json.loads(m.group(1))
    assert routes == [
        ["GET", "/api/products/suggest", 200, [{"id": 1, "name": "가상 name", "status": "ON"}]]
    ]
    assert (
        'renderApp("/search")' in test and '["search-input"]' in test
    )  # combobox 검색 입력란도 글을 넣는다
    assert CONFORM_TEST.startswith("__conform__/")  # tsconfig(src)·지원자 설정(src/**)·채점 설정 밖
    assert "__conform__/**/*.test.{ts,tsx}" in files[CONFORM_CONFIG]
    assert "from '../src/__grading__/harness/grader'" in test
    # #177: 앱 오류는 모양(글 네 꼴)이 아니라 종류로 — Error 객체·'…Error' 글·처리되지 않은 Promise 거부 (실제 실행 확인 2026-11-14:
    # 비동기 throw·'x is not iterable' 거부·잡아서 console.error 로 남긴 Error 는 실패, 'Warning: …' 개발 경고만은 통과)
    assert "a instanceof Error" in test and "APP_ERROR = /^(?:Uncaught )?[A-Za-z]*Error\\b" in test
    assert (
        "addEventListener('unhandledrejection'" in test and "proc?.on('unhandledRejection'" in test
    )
    assert "is not a function" not in test


def test_not_applied_to_java_templates() -> None:
    java = load_template("backend/java-spring-boot3-jpa")
    assert render_conformance(_list_contract(), java.manifest) == []


def _conform(
    name: str = "s1 /search — 계약 모양 응답에 화면이 깨지지 않는다", ok: bool = True
) -> dict[str, object]:
    return _t(
        "conform",
        name,
        "PASS" if ok else "HARNESS_ERR",
        None,
        "" if ok else "앱 오류: Cannot read properties of undefined",
    )


def _assign(*runs: TargetRun, starter_ok: bool = True):  # type: ignore[no-untyped-def]
    starter = _run("starter", "starter", _conform(ok=starter_ok))
    return assign(VerifyReport(runs=[starter, *runs]), PLAN2, OVERLAYS, MISSED, {})


def test_gate0_starter_broken_by_contract_shape_goes_to_starter() -> None:
    plan = _assign(_run("d1", "ref_accepted", *GOOD_ACC, _conform()), starter_ok=False)
    assert [(a.generator, a.gate) for a in plan.actions] == [("gen_starter", "⓪")]
    assert "계약의 모양" in plan.actions[0].note and "Cannot read" in plan.actions[0].note


def test_gate0_all_implementations_broken_the_same_way_is_shared_code() -> None:
    plan = _assign(
        _run("naive-all", "ref_naive", *GOOD_NAIVE, _conform(ok=False)),
        _run("d1", "ref_accepted", *GOOD_ACC, _conform(ok=False)),
    )
    assert [(a.generator, a.gate) for a in plan.actions] == [("gen_starter", "⓪")]
    assert "공유 코드" in plan.actions[0].reason


def test_gate0_some_implementations_broken_regenerates_them_only() -> None:
    """깨진 구현만 다시 — 이번 회차 다른 게이트는 보지 않는다 (남은 구현끼리 비교하면 '모두 실패'를 잘못 읽는다)."""
    broken = _run(
        "d1", "ref_accepted", _t("hidden", "hc1", "ASSERT", None, "x"), _conform(ok=False)
    )
    plan = _assign(
        _run("naive-all", "ref_naive", *GOOD_NAIVE, _conform()),
        broken,
        _run("d2", "ref_accepted", *GOOD_ACC, _conform()),
    )
    assert [(a.generator, a.label, a.gate) for a in plan.actions] == [("gen_accepted", "d1", "⓪")]
    # 남은 구현 하나가 공개 테스트에서 떨어져도 이번 회차에는 공개 테스트를 의심하지 않는다 (2026-11-10 e99cbba0 재검증)
    lone = _run(
        "d2",
        "ref_accepted",
        *GOOD_ACC,
        _t("public", "pt1", "HARNESS_ERR", None, "요청이 오지 않음"),
        _conform(),
    )
    plan = _assign(_run("naive-all", "ref_naive", *GOOD_NAIVE, _conform(ok=False)), lone)
    assert [a.generator for a in plan.actions] == ["gen_naive"]


def test_gate0_silent_when_everything_conforms() -> None:
    plan = _assign(
        _run("naive-all", "ref_naive", *GOOD_NAIVE, _conform()),
        _run("d1", "ref_accepted", *GOOD_ACC, _conform()),
    )
    assert plan.passed


# ── A1 앱 쪽 계약 타입 ─────────────────────────────────────────────


def test_app_types_have_the_same_names_and_shapes_as_the_test_helper() -> None:
    from task_spec.starter.ts_helpers import APP_TYPES, render_app_types

    f = render_app_types(_list_contract(), FE.manifest)
    assert f is not None and f.path == APP_TYPES
    assert "export interface Product {" in f.content
    assert "export type GetApiProductsSuggestOk = Product[];" in f.content  # 배열이면 배열 그대로
    java = load_template("backend/java-spring-boot3-jpa")
    assert render_app_types(_list_contract(), java.manifest) is None  # Java·Python 은 켜지 않는다


def _starter_api(content: str):  # type: ignore[no-untyped-def]
    from task_spec.schemas import GeneratedFile
    from task_spec.starter.static_check import Overlay

    return Overlay(
        kind="starter", files=(GeneratedFile(path="src/api/products.ts", content=content),)
    )


def test_reading_responses_without_contract_types_is_reasked() -> None:
    from task_spec.starter.static_check import _contract_types_usage

    bad = (
        "export interface Product { name: string }\n"
        "export async function suggest(q: string) {\n"
        "  const res = await fetch(`/api/products/suggest?q=${q}`);\n"
        "  const data: { items: Product[] } = await res.json();\n"
        "  return data.items;\n}\n"
    )
    msg = _contract_types_usage(_starter_api(bad), FE, _list_contract())[0].message
    assert "다시 정의함(Product)" in msg and "직접 적음" in msg and "import 하지 않음" in msg
    good = (
        "import type { GetApiProductsSuggestOk } from './contract-types';\n"
        "export async function suggest(q: string): Promise<GetApiProductsSuggestOk> {\n"
        "  const res = await fetch(`/api/products/suggest?q=${q}`);\n"
        "  const data: GetApiProductsSuggestOk = await res.json();\n"
        "  return data;\n}\n"
    )
    assert _contract_types_usage(_starter_api(good), FE, _list_contract()) == []


async def test_generators_cannot_overwrite_the_app_types_file() -> None:
    from task_spec.nodes.starter.generate import _app_overlay
    from task_spec.schemas import GeneratedFile
    from task_spec.starter.ts_helpers import APP_TYPES
    from tests.starter.test_frontend_template import _fe_inputs

    b = _fe_inputs()
    fake = GeneratedFile(path=APP_TYPES, content="export type X = 1;")
    page = GeneratedFile(path="src/pages/Search.tsx", content="export default 1;")
    starter = _app_overlay(b, "starter")([fake, page])
    types = [f for f in starter.files if f.path == APP_TYPES]
    assert len(types) == 1 and "export type X" not in types[0].content  # 코드 판으로 바뀜
    ref = _app_overlay(b, "ref_accepted", "d1")([fake, page])
    assert [f.path for f in ref.files] == [
        "src/pages/Search.tsx"
    ]  # 구현은 쓰지 못함 — 시작 코드 판이 그대로


# ── A3 선택 값 (계약 options) ─────────────────────────────────────


def _choice_contract(options: list[str]) -> TaskContract:
    d = _list_contract().model_dump(mode="json")
    d["screens"][0]["elements"].append(
        {
            "test_id": "category-select",
            "role": "combobox",
            "description": "점검 항목 선택",
            "action": "고르면 화면 값만 바뀜 — 요청은 저장 버튼에서",
            "options": options,
            "refs": ["r1"],
        }
    )
    return TaskContract.model_validate(d)


def test_choice_elements_need_values_without_placeholders() -> None:
    from task_spec.starter.contract import _ui_options, choice_element

    assert "options" in "\n".join(_ui_options(_choice_contract([])))
    assert "안내 문구" in "\n".join(_ui_options(_choice_contract(["선택하세요", "센서"])))
    assert _ui_options(_choice_contract(["캘리브레이션", "센서"])) == []
    # 검색 자동완성 입력란(combobox)은 선택형이 아니다
    assert not choice_element("combobox", "search-input", "검색어")


def test_tests_must_choose_from_contract_values() -> None:
    from task_spec.schemas import GeneratedFile
    from task_spec.starter.static_check import option_guesses

    c = _choice_contract(["캘리브레이션", "센서"])

    def f(body: str) -> list[GeneratedFile]:
        return [GeneratedFile(path="src/__grading__/cases/hc1.test.tsx", content=body)]

    assert option_guesses(f("await user.selectOptions(sel, sel.options[0]);"), c, "숨은 테스트")
    assert option_guesses(f("await user.selectOptions(sel, '액추에이터');"), c, "숨은 테스트")
    assert option_guesses(f("await user.selectOptions(sel, '센서');"), c, "숨은 테스트") == []
    assert (
        option_guesses(
            f("await user.selectOptions(sel, OPTIONS.CATEGORY_SELECT[0]);"), c, "숨은 테스트"
        )
        == []
    )


def test_starter_must_render_contract_values() -> None:
    from task_spec.schemas import GeneratedFile
    from task_spec.starter.static_check import Overlay, _starter_options

    c = _choice_contract(["캘리브레이션", "센서"])

    def o(body: str) -> Overlay:
        return Overlay(
            kind="starter", files=(GeneratedFile(path="src/pages/Form.tsx", content=body),)
        )

    assert _starter_options(o('<option value="">선택</option><option>센서</option>'), c)
    literal = '<option value="">선택하세요</option><option value="캘리브레이션">캘리브레이션</option><option value="센서">센서</option>'
    assert _starter_options(o(literal), c) == []
    via_const = "import { OPTIONS } from '../api/contract-types';\nOPTIONS.CATEGORY_SELECT.map((v) => <option key={v}>{v}</option>)"
    assert _starter_options(o(via_const), c) == []


def test_dev_mock_server_reading_request_bodies_is_not_flagged() -> None:
    """2026-11-10 검증 알파 cf-fe-token: 개발용 가짜 서버가 요청 본문을 `await request.json() as {…}` 로 읽은 것을 응답 모양으로 잘못 잡아
    시작 코드 생성이 멈췄다."""
    from task_spec.schemas import GeneratedFile
    from task_spec.starter.static_check import Overlay, _contract_types_usage

    mock = "http.post('/api/x', async ({ request }) => { const body = (await request.json()) as { id: number }; return HttpResponse.json(body); })"
    o = Overlay(kind="starter", files=(GeneratedFile(path="src/mocks/handlers.ts", content=mock),))
    assert _contract_types_usage(o, FE, _list_contract()) == []
    # #177: 가짜 서버는 경로 글자·변수 이름이 아니라 msw 를 쓰는 코드로 안다 — 폴더 밖 핸들러·변수 r 도 면제
    msw = "import { http, HttpResponse } from 'msw';\n"
    for path, body in (
        ("src/api/x.ts", mock),
        ("src/dev/handlers.ts", mock.replace("request", "r")),
    ):
        o = Overlay(kind="starter", files=(GeneratedFile(path=path, content=msw + body),))
        assert _contract_types_usage(o, FE, _list_contract()) == [], path
    # msw 를 쓰지 않는 앱 코드가 응답을 `as { … }` 로 읽으면 변수 이름(request·req)과 상관없이 잡는다
    app = "import type { A } from './contract-types';\nconst req = await fetch('/api/x');\nconst v = (await req.json()) as { id: number };\n"
    o = Overlay(kind="starter", files=(GeneratedFile(path="src/api/y.ts", content=app),))
    assert _contract_types_usage(o, FE, _list_contract())
