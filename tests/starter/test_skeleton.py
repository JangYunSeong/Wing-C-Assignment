"""코드 뼈대 (React) — 계약에서 API 호출 함수·화면 컴포넌트·라우트를 코드가 먼저 쓰고, 생성기는 이름·시그니처를 유지한다. 가상 데이터만."""

from __future__ import annotations

from task_spec.schemas import GeneratedFile, TaskContract
from task_spec.starter.skeleton import (
    APP,
    ENDPOINTS,
    endpoint_fns,
    page_names,
    render_skeleton,
)
from task_spec.starter.static_check import Overlay, _skeleton_kept
from task_spec.starter.templates import load_template
from tests.starter.test_contract_shapes import _ui_contract

V10 = load_template("frontend/react-vite-ts", 10)
V8 = load_template("frontend/react-vite-ts", 8)
C = TaskContract.model_validate(_ui_contract())


def test_skeleton_only_when_the_template_turns_it_on() -> None:
    assert render_skeleton(C, V8.manifest) == []
    paths = [f.path for f in render_skeleton(C, V10.manifest)]
    assert ENDPOINTS in paths and APP in paths
    assert all(f"src/pages/{n}.tsx" in paths for n in page_names(C).values())


def test_endpoint_functions_follow_the_harness_naming() -> None:
    """하네스 도우미(waitForPatchOrdersIdStatus)와 같은 규칙 — 테스트·앱·README 가 한 이름."""
    fns = {f.name: f.params for f in endpoint_fns(C)}
    assert fns["patchOrdersIdStatus"] == ("id", "body", "init")
    assert fns["getOrdersIdHistory"] == ("id", "init")
    text = next(f.content for f in render_skeleton(C, V10.manifest) if f.path == ENDPOINTS)
    assert (
        "export async function patchOrdersIdStatus(id: string, body: PatchOrdersIdStatusBody, init?: RequestInit)"
        in text
    )
    assert "new Headers(init?.headers)" in text and "throw new ApiError" in text
    for word in ("retry", "cache", "queue", "dedupe", "abort"):  # 함정의 장치는 몸통에 없다
        assert word not in text.lower()


def test_app_routes_screens_to_page_components() -> None:
    app = next(f.content for f in render_skeleton(C, V10.manifest) if f.path == APP)
    for sc in C.screens:
        assert f"<{page_names(C)[sc.screen_id]} />" in app
    assert "new QueryClient" in app and "BrowserRouter" in app


def _problems(files: list[GeneratedFile]) -> list[str]:
    return [p.message for p in _skeleton_kept(Overlay(kind="starter", files=tuple(files)), V10, C)]


def test_kept_skeleton_passes_and_renames_are_caught() -> None:
    skel = render_skeleton(C, V10.manifest)
    assert _problems(skel) == []
    ep = next(f for f in skel if f.path == ENDPOINTS)
    renamed = ep.content.replace("patchOrdersIdStatus(", "updateStatus(")
    assert any(
        "patchOrdersIdStatus 가 없음" in p
        for p in _problems([GeneratedFile(path=ENDPOINTS, content=renamed)])
    )
    reparam = ep.content.replace(
        "(id: string, body: PatchOrdersIdStatusBody",
        "(orderId: number, body: PatchOrdersIdStatusBody",
    )
    assert any(
        "매개변수가 뼈대와 다름: (orderId, body, init)" in p
        for p in _problems([GeneratedFile(path=ENDPOINTS, content=reparam)])
    )
    app = next(f for f in skel if f.path == APP)
    gone = app.content.replace(f"<{page_names(C)['s1']} />", "<Other />")
    assert any("뼈대 라우트" in p for p in _problems([GeneratedFile(path=APP, content=gone)]))
    page = next(f for f in skel if f.path.startswith("src/pages/"))
    assert any(
        "export default 하지 않음" in p
        for p in _problems([GeneratedFile(path=page.path, content="export function X() {}")])
    )
    # 도우미를 더하거나 몸통을 바꾸는 것은 괜찮다
    body = (
        ep.content.replace("const res = await fetch(", "const res = await myFetch(")
        + "\nexport function extra() {}\n"
    )
    assert _problems([GeneratedFile(path=ENDPOINTS, content=body)]) == []
