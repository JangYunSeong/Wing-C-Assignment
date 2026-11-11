"""채점 코드 컴파일 루프 (build.compile_and_fix) — 구현 만들기 전에 숨은·공개 테스트를 컴파일하고 오류를 쓴 생성기에 돌려준다.
가상 데이터만 — 컴파일러는 가짜 (실제 Gradle 은 eval/starter/gradle_verify.GradleVerifier.compile_tests)."""

from __future__ import annotations

from task_spec.nodes.starter.build import build_and_repair, compile_note
from task_spec.schemas import CompileError
from task_spec.starter.static_check import Overlay
from tests.starter.test_generate import D1, _inputs
from tests.starter.test_repair import (
    D1_OK,
    HIDDEN,
    NAIVE_OK,
    PROMPTS,
    ScriptedVerifier,
    _llm,
    _one_case,
)
from tests.starter.test_static_check import CASES

CASE = f"{CASES}/OrderStatusBurstTest.java"
NESTED = CompileError(
    path=CASE,
    line=14,
    message="cannot find symbol",
    detail="symbol: class IntUnaryOperator, location: class Burst",
)


class ScriptedCompiler:
    def __init__(self, *rounds: list[CompileError]) -> None:
        self.rounds = list(rounds)
        self.seen: list[tuple[Overlay, Overlay | None]] = []

    async def compile_tests(self, hidden: Overlay, public: Overlay | None) -> list[CompileError]:
        self.seen.append((hidden, public))
        return self.rounds.pop(0) if len(self.rounds) > 1 else self.rounds[0]


async def _build(compiler: ScriptedCompiler, **routes):  # type: ignore[no-untyped-def]
    llm, backend = _llm(**routes)
    out = await build_and_repair(
        _inputs(),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=ScriptedVerifier(_one_case(NAIVE_OK, D1_OK)),
        compiler=compiler,
    )
    return out, backend


async def test_compile_errors_go_back_to_the_generator_that_wrote_the_file() -> None:
    """2026-11-08 상류 평가: `Burst.IntUnaryOperator` — 오류 위치·symbol·그 줄의 코드를 붙여 숨은 테스트만 다시."""
    compiler = ScriptedCompiler([NESTED], [])
    out, backend = await _build(compiler)
    assert out.status == "review"
    assert [(r.gate, r.target, r.result) for r in out.repairs] == [
        ("compile", "gen_hidden", "regenerated")
    ]
    assert len(compiler.seen) == 2 and compiler.seen[0][1] is not None  # 공개 테스트도 함께 컴파일
    hidden_calls = [u for name, u in backend.users if name == "gen_hidden"]
    assert len(hidden_calls) == 2
    assert "class IntUnaryOperator, location: class Burst" in hidden_calls[1]
    assert "코드: var outcomes = Burst.fire(" in hidden_calls[1]
    assert backend.calls.count("gen_public") == 1  # 오류가 없는 공개 테스트는 그대로


async def test_persistent_compile_errors_stop_before_implementations() -> None:
    """재요청 2회 뒤에도 컴파일되지 않으면 구현을 만들지 않는다 — 깨진 테스트 위에 시작 코드·참조 구현을 쓰지 않게."""
    compiler = ScriptedCompiler([NESTED])
    out, backend = await _build(compiler, gen_hidden=[HIDDEN])
    assert out.status == "failed" and len(compiler.seen) == 3
    assert [r.target for r in out.repairs] == ["gen_hidden", "gen_hidden"]
    assert any("채점 코드 컴파일 실패" in f and "IntUnaryOperator" in f for f in out.review_flags)
    assert "gen_starter" not in backend.calls and set(out.overlays) == {"public", "hidden"}


async def test_errors_in_code_written_files_are_not_sent_to_generators() -> None:
    """코드가 쓴 도우미(ContractCase)·조절값 파일의 오류는 생성기를 다시 불러도 못 고친다 — 한 번 컴파일하고 멈춘다."""
    helper = _inputs().template.manifest.hidden_helper
    assert helper is not None
    bad = CompileError(path=helper.path, line=3, message="';' expected")
    compiler = ScriptedCompiler([bad])
    out, backend = await _build(compiler)
    assert out.status == "failed" and len(compiler.seen) == 1 and out.repairs == []
    assert backend.calls.count("gen_hidden") == 1


def test_compile_note_shows_location_detail_and_code_line() -> None:
    from task_spec.schemas import GeneratedFile

    o = Overlay(kind="hidden", files=(GeneratedFile(path=CASE, content="a\nb\n  Burst.X x;\n"),))
    e = CompileError(path=CASE, line=3, message="cannot find symbol", detail="symbol: class X")
    note = compile_note([e], o)
    assert f"- {CASE}:3 cannot find symbol (symbol: class X)" in note
    assert "  코드: Burst.X x;" in note


async def test_no_compiler_keeps_the_old_flow() -> None:
    llm, backend = _llm()
    out = await build_and_repair(
        _inputs(),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=ScriptedVerifier(_one_case(NAIVE_OK, D1_OK)),
    )
    assert out.status == "review" and out.repairs == []


# ── 구현 컴파일 루프 (시작 코드·참조 구현 — build.compile_impl_and_fix) ──

PKG = "src/main/java/com/example/service"
ORDER_JAVA = f"{PKG}/order/Order.java"
SERVICE_JAVA = f"{PKG}/order/OrderService.java"


def _err(
    path: str, msg: str = "cannot find symbol", detail: str = "symbol: class Map"
) -> CompileError:
    return CompileError(path=path, line=1, message=msg, detail=detail)


class ImplCompiler(ScriptedCompiler):
    """채점 코드 컴파일은 늘 통과, 구현 컴파일은 부를 때마다 준비한 오류를 차례로 (다 쓰면 마지막 것)."""

    def __init__(self, *impl_rounds: list[CompileError]) -> None:
        super().__init__([])
        self.impl_rounds = list(impl_rounds)
        self.parts: list[list[str]] = []

    async def compile_overlays(self, overlays):  # type: ignore[no-untyped-def]
        self.parts.append([o.label or o.kind for o in overlays])
        return self.impl_rounds.pop(0) if len(self.impl_rounds) > 1 else self.impl_rounds[0]


async def test_starter_compile_errors_go_back_to_gen_starter_before_refs() -> None:
    """2026-11-08 알파: 시작 코드가 자기 테스트에서 import 를 빠뜨려 첫 검증 회차가 통째로 compile_error — 검증 전에 고친다."""
    compiler = ImplCompiler([_err(ORDER_JAVA)], [], [], [])
    out, backend = await _build(compiler)
    assert out.status == "review"
    assert ("compile", "gen_starter", "regenerated") in [
        (r.gate, r.target, r.result) for r in out.repairs
    ]
    starter_calls = [u for name, u in backend.users if name == "gen_starter"]
    assert len(starter_calls) == 2 and "구현 컴파일 오류" in starter_calls[1]
    assert "symbol: class Map" in starter_calls[1]
    # 시작 코드는 공개 테스트와, 참조 구현은 시작 코드·공개·숨은 테스트와 함께 컴파일
    assert compiler.parts[0] == ["starter", "public_test"]
    assert compiler.parts[2] == ["starter", "naive-all", "public_test", "hidden"]


async def test_ref_compile_errors_go_back_to_that_ref_only() -> None:
    compiler = ImplCompiler([], [_err(SERVICE_JAVA)], [], [])
    out, backend = await _build(compiler)
    assert out.status == "review"
    compile_repairs = [r for r in out.repairs if r.gate == "compile"]
    assert [(r.target, r.result) for r in compile_repairs] == [("gen_naive", "regenerated")]
    assert compile_repairs[0].reason.startswith("naive-all: OrderService.java:1")
    assert backend.calls.count("gen_naive") == 2 and backend.calls.count("gen_accepted") == 1


async def test_persistent_starter_compile_error_stops_before_refs() -> None:
    compiler = ImplCompiler([_err(ORDER_JAVA)])
    out, backend = await _build(compiler)
    assert out.status == "failed" and len(compiler.parts) == 3  # 첫 컴파일 + 재요청 2회
    assert any("시작 코드 컴파일 실패" in f for f in out.review_flags)
    assert "gen_naive" not in backend.calls and "gen_accepted" not in backend.calls


async def test_errors_outside_the_implementation_are_not_sent_back() -> None:
    """다른 산출물(공개 테스트 등) 파일의 오류는 그 구현의 생성기로 못 고친다 — 한 번 컴파일하고 멈춘다."""
    compiler = ImplCompiler([_err(f"{PKG}/publictests/OtherTest.java")])
    out, backend = await _build(compiler)
    assert out.status == "failed" and len(compiler.parts) == 1
    assert backend.calls.count("gen_starter") == 1


PUBLIC_JAVA = "src/test/java/com/example/service/publictests/OrderHistoryPublicTest.java"


async def test_regenerated_public_tests_are_compiled_before_the_next_verify() -> None:
    """2026-11-10 프론트 알파 fe-v3-opt: ⑤ 로 다시 만든 공개 테스트가 컴파일되지 않은 채 검증에 들어가 모든 대상이 compile_error →
    ① 이 시작 코드를 다시 만들었다. 다시 만든 채점 코드도 처음처럼 컴파일하고 오류는 그 파일을 쓴 생성기로."""
    from tests.starter.test_repair import _run, _t

    every_ref_fails_pt1 = _one_case(
        _run(
            "naive-all",
            "ref_naive",
            _t("hidden", "hc1", "ASSERT", "pool_acquire_timeout", "32"),
            _t("public", "pt1", "ASSERT", None, "expected 200 but was 404"),
        ),
        _run(
            "d1",
            "ref_accepted",
            _t("hidden", "hc1"),
            _t("public", "pt1", "ASSERT", None, "expected 200 but was 404"),
        ),
    )
    verifier = ScriptedVerifier(every_ref_fails_pt1, _one_case(NAIVE_OK, D1_OK))
    pub_err = CompileError(
        path=PUBLIC_JAVA, line=9, message="cannot find symbol", detail="symbol: method seed"
    )
    compiler = ScriptedCompiler([], [pub_err], [])  # 처음 · ⑤ 뒤 · 재요청 뒤
    llm, backend = _llm()
    out = await build_and_repair(
        _inputs(),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=verifier,
        compiler=compiler,
    )
    assert out.status == "review"
    assert [(r.gate, r.target, r.result) for r in out.repairs] == [
        ("⑤", "gen_public", "regenerated"),
        ("compile", "gen_public", "regenerated"),
    ]
    assert len(compiler.seen) == 3 and backend.calls.count("gen_public") == 3
    assert backend.calls.count("gen_starter") == 1  # 시작 코드는 탓하지 않는다
    public_calls = [u for name, u in backend.users if name == "gen_public"]
    assert "symbol: method seed" in public_calls[2]


def test_starter_compile_errors_inside_test_files_go_to_the_test_generator() -> None:
    """같은 알파: 모든 오류가 공개 테스트 파일 안인데 '시작 코드 빌드 실패'(①)로 시작 코드를 다시 만들었다."""
    from task_spec.schemas import GeneratedFile, TargetRun, VerifyReport
    from task_spec.starter.repair import assign
    from tests.starter.test_repair import MISSED, OVERLAYS, PLAN2, PUBLIC_TEST

    def report(path: str) -> VerifyReport:
        return VerifyReport(
            runs=[
                TargetRun.model_validate(
                    {
                        "target": "starter",
                        "kind": "starter",
                        "setup": "compile_error",
                        "compile_errors": [
                            {
                                "path": path,
                                "line": 16,
                                "message": "TS2554: Expected 0-1 arguments, but got 2.",
                            }
                        ],
                    }
                )
            ]
        )

    public = Overlay(
        kind="public_test", files=(GeneratedFile(path=PUBLIC_JAVA, content=PUBLIC_TEST),)
    )
    overlays = dict(OVERLAYS) | {"public": public}
    plan = assign(report(PUBLIC_JAVA), PLAN2, overlays, MISSED)
    assert [(a.generator, a.compile) for a in plan.actions] == [("gen_public", True)]
    assert "TS2554" in plan.actions[0].note
    # 시작 코드 파일의 오류는 그대로 시작 코드
    plan = assign(report(ORDER_JAVA), PLAN2, overlays, MISSED)
    assert [(a.generator, a.gate) for a in plan.actions] == [("gen_starter", "①")]


def test_previous_hidden_file_is_kept_only_when_every_accepted_impl_passed() -> None:
    """#102 검토: 이전 파일 유지는 게이트 ③·③' 이고 허용 구현이 모두 통과한 케이스만 — 허용 구현을 떨어뜨린 케이스(④)를 남기면
    다음 회차에 같은 실패가 되풀이된다 (2026-11-11 재검증 오프라인 hc1)."""
    from task_spec.nodes.starter.build import BuildOutcome, _keepable_cases
    from task_spec.schemas import TargetRun, VerifyReport
    from task_spec.starter.repair import RepairAction

    def run(target: str, kind: str, hc1: str, hc2: str) -> TargetRun:
        tests = [
            {"group": "hidden", "name": "hc1", "result": hc1},
            {"group": "hidden", "name": "hc2", "result": hc2},
        ]
        return TargetRun.model_validate({"target": target, "kind": kind, "tests": tests})

    out = BuildOutcome(status="failed")
    out.reports.append(
        VerifyReport(
            runs=[
                run("naive-all", "ref_naive", "PASS", "HARNESS_ERR"),
                run("d1", "ref_accepted", "PASS", "ASSERT"),
                run("d2", "ref_accepted", "PASS", "PASS"),
            ]
        )
    )

    def hidden(gate: str) -> RepairAction:
        return RepairAction("gen_hidden", "", gate, "가상 사유", "가상 수정 요청")

    assert _keepable_cases([hidden("③"), hidden("③'")], out) == {
        "hc1"
    }  # hc2 는 허용 구현 d1 이 실패
    assert _keepable_cases([hidden("③"), hidden("④")], out) == frozenset()
    assert _keepable_cases([hidden("③④")], out) == frozenset()
    assert _keepable_cases([hidden("③"), hidden("setup")], out) == frozenset()
    assert _keepable_cases([hidden("static")], out) == frozenset()
    assert (
        _keepable_cases([RepairAction("gen_naive", "naive-all", "③", "r", "n")], out) == frozenset()
    )
