"""코드 뼈대 (React) — 계약에서 API 호출 함수·화면 컴포넌트·라우트를 코드가 먼저 쓰고, 생성기는 이름·시그니처를 유지한다. 가상 데이터만."""

from __future__ import annotations

from task_spec.schemas import GeneratedFile, TaskContract
from task_spec.starter.render import candidate_contract
from task_spec.starter.skeleton import (
    APP,
    ENDPOINTS,
    JAVA_CONTROLLER,
    JAVA_SERVICE,
    endpoint_fns,
    java_fns,
    page_names,
    render_skeleton,
)
from task_spec.starter.static_check import Overlay, _skeleton_kept
from task_spec.starter.templates import load_template
from tests.starter.test_contract_shapes import _contract, _ui_contract
from tests.starter.test_plan_contract import _task

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


# ── Java ───────────────────────────────────────────────────────────

J2 = load_template("backend/java-spring-boot3-jpa", 2)
J1 = load_template("backend/java-spring-boot3-jpa", 1)
CJ = TaskContract.model_validate(_contract())


def _jproblems(files: list[GeneratedFile]) -> list[str]:
    return [p.message for p in _skeleton_kept(Overlay(kind="starter", files=tuple(files)), J2, CJ)]


def test_java_skeleton_maps_every_endpoint_through_one_controller() -> None:
    assert render_skeleton(CJ, J1.manifest) == []
    files = {f.path: f.content for f in render_skeleton(CJ, J2.manifest)}
    ctl, svc = files[JAVA_CONTROLLER], files[JAVA_SERVICE]
    for fn in java_fns(CJ):
        assert f"service.{fn.name}({', '.join(fn.params)})" in ctl
        assert f" {fn.name}(" in svc
    for e in CJ.endpoints:
        assert f'{e.method.capitalize()}Mapping("{e.path}")' in ctl  # type: ignore[union-attr]
    assert "UnsupportedOperationException" in svc
    assert _jproblems(list(render_skeleton(CJ, J2.manifest))) == []


def test_skeleton_comments_carry_no_summary_for_an_analysis_spec() -> None:
    """문제 분석 판(요청 글이 있는 명세) — 시작 코드 주석은 모양만. 요약에 규칙이 들어간다(2026-11-17 알파 #006 컨트롤러 주석
    '허용되지 않은 전이는 400으로 거절한다'). 예전 판은 요약 그대로, 이름·매핑은 두 판이 같다 (analysis-stage.md §5.2)."""
    old, asked = _task(), _task().model_copy(update={"request": "가상 요청 글"})
    for contract, manifest, path in (
        (CJ, J2.manifest, JAVA_CONTROLLER),
        (C, V10.manifest, ENDPOINTS),
    ):
        full = {
            f.path: f.content for f in render_skeleton(candidate_contract(old, contract), manifest)
        }
        thin = {
            f.path: f.content
            for f in render_skeleton(candidate_contract(asked, contract), manifest)
        }
        for e in contract.endpoints:
            assert f"{e.method} {e.path} — {e.summary}" in full[path]
            assert (
                e.summary not in thin[path] and f"{e.endpoint_id} {e.method} {e.path}" in thin[path]
            )
        assert thin[path].count("Mapping(") == full[path].count("Mapping(")
    assert _jproblems(list(render_skeleton(candidate_contract(asked, CJ), J2.manifest))) == []


def test_java_skeleton_changes_and_remapping_are_caught() -> None:
    skel = render_skeleton(CJ, J2.manifest)
    svc = next(f for f in skel if f.path == JAVA_SERVICE)
    fn = java_fns(CJ)[0]
    renamed = svc.content.replace(f" {fn.name}(", " handle(")
    assert any(
        f"{fn.name} 가 없음" in p
        for p in _jproblems([GeneratedFile(path=JAVA_SERVICE, content=renamed)])
    )
    e = next(e for e in CJ.endpoints if e.method)
    other = GeneratedFile(
        path="src/main/java/com/example/service/orders/OrderController.java",
        content=f'@RestController\npublic class OrderController {{ @GetMapping("{e.path}") public void x() {{}} }}',
    )
    assert any("다시 매핑함" in p for p in _jproblems([other]))
    # 몸통을 채우고 의존성을 더하는 것은 괜찮다
    filled = svc.content.replace(
        "throw new UnsupportedOperationException(", "return ResponseEntity.ok("
    ).replace(
        "public class ContractService {",
        "public class ContractService {\n    private final Object repo = null;",
    )
    assert _jproblems([GeneratedFile(path=JAVA_SERVICE, content=filled)]) == []
