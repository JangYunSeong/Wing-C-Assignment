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
