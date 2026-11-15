"""공개 테스트 변별 관문 (#164) — 규칙을 어긴 변형이 공개 테스트에서 떨어지는가 (starter/variants.py · build._variant_gate)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from task_spec.llm import Completion, LLMClient
from task_spec.nodes.starter.build import COMPILE_TEST_ATTEMPTS, VARIANT_GATE, build_and_repair
from task_spec.nodes.starter.generate import variant_input
from task_spec.schemas import CompileError, GeneratedFile, GenVariantOutput, VerifyReport
from task_spec.starter.static_check import Overlay
from task_spec.starter.variants import (
    MAX_CHANGED_LINES,
    VariantResult,
    VariantSpec,
    VariantTarget,
    app_files,
    build_variants,
    changed_lines,
    judge,
    needs_boundary,
    pts_by_test,
    repair_note,
    requirement_acs,
    targets,
)
from tests.starter.test_generate import D1, PKG, SERVICE, STARTER, _files, _inputs
from tests.starter.test_repair import (
    HIDDEN as HIDDEN_ANSWER,
)
from tests.starter.test_repair import (
    NAIVE_OK,
    PROMPTS,
    RoutedBackend,
    _no_sleep,
    _one_case,
    _run,
    _t,
)
from tests.starter.test_repair import (
    PUBLIC as PUBLIC_ANSWER,
)
from tests.starter.test_repair import (
    SVC as SVC_ANSWER,
)

SVC_PATH = f"{PKG}/order/OrderService.java"
BROKEN = "package com.example.service.order;\n\nclass OrderService { boolean sorted = false; }\n"
PUBLIC_CLASS = "OrderHistoryPublicTest"


def _variants(*items: tuple[str, str, list[tuple[str, str]]]) -> Completion:
    return Completion(
        json.dumps(
            {
                "variants": [
                    {"op": op, "note": note, "files": [{"path": p, "content": c} for p, c in fs]}
                    for op, note, fs in items
                ]
            },
            ensure_ascii=False,
        )
    )


class VariantBackend(RoutedBackend):
    MARKERS = {**RoutedBackend.MARKERS, "gen_variant": "**공개 요구 하나를 어긴 구현**"}


def _llm(**over: list[Completion]) -> tuple[LLMClient, VariantBackend]:
    routes = {
        "gen_public": [PUBLIC_ANSWER],
        "gen_starter": [_files(*STARTER)],
        "gen_hidden": [HIDDEN_ANSWER],
        "gen_naive": [SVC_ANSWER],
        "gen_accepted": [SVC_ANSWER],
        "gen_variant": [_variants(("invert", "오래된 순으로 돌려줌", [(SVC_PATH, BROKEN)]))],
    } | over
    backend = VariantBackend(**routes)
    return LLMClient({"opencode": backend, "openai": backend, "gemini": backend}, sleep=_no_sleep), backend  # type: ignore[dict-item]


class VariantVerifier:
    """검증 회차 보고서 + 변형 실행 결과를 차례로. 변형 실행 경로로 참조 구현(var- 아닌 라벨)이 오면 — 더한 테스트 확인 — ref_results 대로."""

    def __init__(self, report: VerifyReport | list[VerifyReport], *variant_results: str) -> None:
        self.reports = report if isinstance(report, list) else [report]
        self.variant_results = list(variant_results)
        self.variant_calls: list[list[str]] = []
        self.ref_results: dict[str, str] = {}  # 더한 공개 테스트 클래스 이름 → 참조 구현에서의 결과
        self.ref_calls = 0

    async def verify(
        self,
        starter: Overlay,
        hidden: Overlay,
        refs: Sequence[Overlay],
        public: Overlay | None = None,
    ) -> VerifyReport:
        return self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]

    async def verify_variants(
        self, starter: Overlay, variants: Sequence[Overlay], public: Overlay
    ) -> VerifyReport:
        classes = sorted(
            {
                f.path.rsplit("/", 1)[-1].removesuffix(".java")
                for f in public.files
                if "publictests" in f.path
            }
            - {"PublicCase", "PersistenceCheckTest"}
        )
        if variants and not variants[0].label.startswith("var-"):
            self.ref_calls += 1
            return VerifyReport(
                runs=[
                    _run(
                        v.label,
                        v.kind,
                        *(_t("public", c, self.ref_results.get(c, "PASS")) for c in classes),
                    )
                    for v in variants
                ]
            )
        self.variant_calls.append([v.label for v in variants])
        result = (
            self.variant_results.pop(0)
            if len(self.variant_results) > 1
            else self.variant_results[0]
        )
        return VerifyReport(
            runs=[
                _run(v.label, "ref_variant", _t("public", PUBLIC_CLASS, result)) for v in variants
            ]
        )


ADDED_PATH = "src/test/java/com/example/service/publictests/OrderHistoryEdgeTest.java"
ADDED = (
    "package com.example.service.publictests;\n\n"
    "import org.junit.jupiter.api.DisplayName;\nimport org.junit.jupiter.api.Test;\n\n"
    '@DisplayName("pt1 경계 — 규칙을 어긴 구현이 떨어진다")\n'
    "class OrderHistoryEdgeTest extends PublicCase {\n    @Test\n    void edge() throws Exception {\n    }\n}\n"
)


def _addition(touch_old: bool = True) -> Completion:
    from tests.starter.test_repair import PUBLIC_TEST

    pairs = [(ADDED_PATH, ADDED)]
    if touch_old:  # 기존 파일을 고친 답 — 버려야 한다
        pairs.append(
            (
                "src/test/java/com/example/service/publictests/OrderHistoryPublicTest.java",
                PUBLIC_TEST.replace("void pt1()", "void pt1Changed()"),
            )
        )
    return _files(*pairs)


D1_PUBLIC_OK = _run("d1", "ref_accepted", _t("hidden", "hc1"), _t("public", PUBLIC_CLASS))
NAIVE_PUBLIC_OK = NAIVE_OK.model_copy(
    update={
        "tests": [*NAIVE_OK.tests, *_run("x", "ref_naive", _t("public", PUBLIC_CLASS)).tests[1:]]
    }
)
REPORT = _one_case(NAIVE_PUBLIC_OK, D1_PUBLIC_OK)


async def _build(verifier: Any, llm: LLMClient) -> Any:
    return await build_and_repair(
        _inputs(), (D1,), llm=llm, prompts=PROMPTS, ontology_version="onto-test", verifier=verifier
    )


def _base() -> Overlay:
    return Overlay(
        kind="ref_accepted", files=(GeneratedFile(path=SVC_PATH, content=SERVICE),), label="d1"
    )


def test_targets_are_requirements_linked_to_tested_criteria() -> None:
    """변형 대상은 공개 요구(r*) — 수용 기준은 예시 한 점이라 경계를 담지 않는다 (2026-11-13 쿠폰 알파). 이어진 기준은 계약 요소의 refs 로."""
    b = _inputs()
    tested = {t.ac_id for t in b.contract.public_tests}
    got = targets(b.task, b.contract)
    assert [t.rule_id for t in got] == [r.req_id for r in b.task.requirements]
    links = requirement_acs(b.contract)
    for t in got:
        assert set(t.acs) == links.get(t.rule_id, set()) & tested
        assert t.text.startswith(next(r.text for r in b.task.requirements if r.req_id == t.rule_id))


def test_boundary_words_mark_rules_that_need_a_boundary_variant() -> None:
    assert needs_boundary("남은 수량이 요청 수량보다 적으면 차감을 거절한다")
    assert needs_boundary("금액은 1 이상 100000 이하") and needs_boundary("남은 좌석이 0이면 거절")
    assert not needs_boundary("특정 시점 이후의 주문만 돌려준다")  # 포함 여부를 정하지 않음
    assert not needs_boundary("회원별 충전 기록을 최신순으로 돌려준다")


def test_variant_input_hides_public_checks() -> None:
    """변형을 만드는 쪽은 공개 테스트·확인 방법을 보지 않는다 — 테스트를 쓴 쪽의 맹점을 모르는 독립 검사."""
    b = _inputs()
    impl = app_files(Overlay(kind="starter", files=()), _base())
    inp = variant_input(b.task, b.contract, b.template, impl, targets(b.task, b.contract)[0])
    for t in b.contract.public_tests:
        assert t.checks not in inp.contract and t.checks not in inp.impl_files
    assert SVC_PATH in inp.impl_files and "omit" in inp.operators and "boundary" in inp.operators


def test_build_variants_keeps_one_place_changes_only() -> None:
    impl = {SVC_PATH: SERVICE, "src/main/resources/data.sql": "INSERT 1;\n"}
    big = SERVICE + "".join(f"// {i}\n" for i in range(MAX_CHANGED_LINES + 1))
    out = GenVariantOutput.model_validate(
        {
            "variants": [
                {
                    "op": "invert",
                    "note": "뒤집음",
                    "files": [{"path": SVC_PATH, "content": BROKEN}],
                },
                {"op": "omit", "note": "같음", "files": [{"path": SVC_PATH, "content": SERVICE}]},
                {"op": "boundary", "note": "큼", "files": [{"path": SVC_PATH, "content": big}]},
            ]
        }
    )
    kept, dropped = build_variants(VariantTarget("p1", "기준", ("p1",)), out, _base(), impl)
    assert [s.label for _, s in kept] == ["var-p1-invert"]
    overlay, spec = kept[0]
    assert overlay.kind == "ref_variant" and spec.changed == 1
    assert {f.path: f.content for f in overlay.files} == {SVC_PATH: BROKEN}
    assert any("같음" in d for d in dropped) and any("바꾼 줄" in d for d in dropped)
    # 정답 구현에 없는 파일·시드 파일은 바꾸지 못한다
    bad = GenVariantOutput.model_validate(
        {
            "variants": [
                {"op": "omit", "note": "새 파일", "files": [{"path": "X.java", "content": "x"}]},
                {
                    "op": "omit",
                    "note": "시드",
                    "files": [{"path": "src/main/resources/data.sql", "content": "INSERT 2;\n"}],
                },
            ]
        }
    )
    kept, dropped = build_variants(VariantTarget("p1", "기준", ("p1",)), bad, _base(), impl)
    assert not kept and len(dropped) == 2
    assert changed_lines("a\nb\n", "a\nc\n") == 1


def test_judge_sorts_caught_survived_invalid() -> None:
    specs = [
        VariantSpec("var-p1-omit", "p1", ("p1",), "omit", "정렬 없음", (SVC_PATH,), 1),
        VariantSpec("var-p1-invert", "p1", ("p1",), "invert", "오래된 순", (SVC_PATH,), 1),
        VariantSpec("var-p2-boundary", "p2", ("p2",), "boundary", "같을 때 거절", (SVC_PATH,), 1),
    ]
    report = VerifyReport(
        runs=[
            _run("var-p1-omit", "ref_variant", _t("public", PUBLIC_CLASS, "ASSERT")),
            _run("var-p1-invert", "ref_variant", _t("public", PUBLIC_CLASS)),
            _run("var-p2-boundary", "ref_variant", setup="compile_error"),
        ]
    )
    got = {r.label: r.verdict for r in judge(report, specs)}
    assert got == {
        "var-p1-omit": "caught",
        "var-p1-invert": "survived",
        "var-p2-boundary": "invalid",
    }


def test_repair_note_names_the_rule_not_the_code() -> None:
    alive = [VariantResult("var-r2-boundary", "r2", "boundary", "같을 때도 거절", "survived")]
    note = repair_note(alive, {"p2": ["pt2"]}, {"r2": ("p2",)})
    assert "r2(pt2)" in note and "같을 때도 거절" in note and "경계 값과 바로 바깥 값" in note
    assert "class " not in note  # 변형 코드는 주지 않는다


async def test_gate_passes_when_public_tests_catch_the_variant() -> None:
    llm, backend = _llm()
    verifier = VariantVerifier(REPORT, "ASSERT")
    out = await _build(verifier, llm)
    assert out.status == "review" and not out.repairs and backend.calls.count("gen_public") == 1
    labels = [f"var-{t.rule_id}-invert" for t in targets(_inputs().task, _inputs().contract)]
    assert [(r.label, r.verdict) for r in out.variants] == [(x, "caught") for x in labels]
    assert verifier.variant_calls == [labels]
    variant_prompt = next(u for n, u in backend.users if n == "gen_variant")
    assert all(t.checks not in variant_prompt for t in _inputs().contract.public_tests)


async def test_gate_adds_a_test_class_for_a_survivor() -> None:
    """살아남은 변형 → 기존 공개 테스트는 그대로 두고 새 테스트 클래스만 더한다 [2026-11-13 사용자]. 기존 파일을 고친 답은 버리고,
    참조 구현이 더한 클래스를 통과하면 남겨 같은 변형을 다시 돌린다 (변형은 다시 만들지 않음, 검증 회차도 쓰지 않음)."""
    from tests.starter.test_repair import PUBLIC_TEST

    llm, backend = _llm(gen_public=[PUBLIC_ANSWER, _addition()])
    verifier = VariantVerifier(REPORT, "PASS", "ASSERT")
    out = await _build(verifier, llm)
    assert out.status == "review" and out.rounds == 0
    assert backend.calls.count("gen_public") == 2 and backend.calls.count("gen_variant") == len(
        targets(_inputs().task, _inputs().contract)
    )
    public = {f.path: f.content for f in out.overlays["public"].files}
    assert public[ADDED_PATH] == ADDED
    assert (
        "void pt1()"
        in public["src/test/java/com/example/service/publictests/OrderHistoryPublicTest.java"]
    )
    assert PUBLIC_TEST in public.values()  # 기존 파일은 그대로
    assert any("고친 답은 버림" in a for a in out.adjustments)
    assert [(r.gate, r.target, r.result) for r in out.repairs] == [
        (VARIANT_GATE, "gen_public", "regenerated")
    ]
    repair_prompt = [u for n, u in backend.users if n == "gen_public"][-1]
    assert "오래된 순으로 돌려줌" in repair_prompt and "새 테스트 클래스" in repair_prompt
    labels = [f"var-{t.rule_id}-invert" for t in targets(_inputs().task, _inputs().contract)]
    assert verifier.variant_calls == [labels, labels] and verifier.ref_calls == 1
    assert {r.verdict for r in out.variants} == {"caught"}
    assert not any("가르지 못함" in f for f in out.review_flags)


class AddedCompiler:
    """더한 클래스에 BROKEN 표시가 있으면 컴파일 오류 — 처음 채점 코드 컴파일은 통과 (변형 관문 수리만 본다)."""

    def __init__(self) -> None:
        self.calls = 0

    async def compile_tests(self, hidden: Overlay, public: Overlay | None) -> list[CompileError]:
        self.calls += 1
        bad = [f for f in (public.files if public else ()) if "BROKEN" in f.content]
        return [
            CompileError(path=f.path, line=9, message="cannot find symbol", detail="symbol: BROKEN")
            for f in bad
        ]

    async def compile_overlays(self, overlays: Sequence[Overlay]) -> list[CompileError]:
        return []


async def test_an_added_class_that_does_not_compile_is_remade_with_the_errors() -> None:
    """2026-11-13 재시도 중복 알파: 경계 변형을 가를 테스트가 `cannot find symbol` 하나로 버려져 살아남음 — 컴파일 오류를 붙여
    다시 만든다 (채점 코드 컴파일 수리와 같은 횟수). 다시 만든 클래스가 컴파일되면 그것으로 변형을 다시 돌린다."""
    broken = _files(
        (ADDED_PATH, ADDED.replace("void edge()", "void edge() { BROKEN(); }\n    void unused()"))
    )
    llm, backend = _llm(gen_public=[PUBLIC_ANSWER, broken, _addition(touch_old=False)])
    verifier = VariantVerifier(REPORT, "PASS", "ASSERT")
    out = await build_and_repair(
        _inputs(),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=verifier,
        compiler=AddedCompiler(),
    )
    assert backend.calls.count("gen_public") == 3
    retry = [u for n, u in backend.users if n == "gen_public"][-1]
    assert "cannot find symbol" in retry and "더하려던 이유" in retry and "BROKEN" in retry
    assert {f.path: f.content for f in out.overlays["public"].files}[ADDED_PATH] == ADDED
    assert {r.verdict for r in out.variants} == {"caught"}
    assert not any("컴파일되지 않아 버림" in f for f in out.review_flags)


async def test_an_added_class_that_never_compiles_is_dropped() -> None:
    broken = _files(
        (ADDED_PATH, ADDED.replace("void edge()", "void edge() { BROKEN(); }\n    void unused()"))
    )
    llm, backend = _llm(gen_public=[PUBLIC_ANSWER, broken])
    out = await build_and_repair(
        _inputs(),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=VariantVerifier(REPORT, "PASS", "ASSERT"),
        compiler=AddedCompiler(),
    )
    assert (
        backend.calls.count("gen_public") == 1 + 1 + COMPILE_TEST_ATTEMPTS
    )  # 처음 + 수리 + 재요청
    assert any("컴파일되지 않아 버림" in f for f in out.review_flags)
    assert ADDED_PATH not in {f.path for f in out.overlays["public"].files}  # 통과한 빌드는 그대로


async def test_gate_flags_a_survivor_after_adding_tests() -> None:
    llm, backend = _llm(gen_public=[PUBLIC_ANSWER, _addition(touch_old=False)])
    verifier = VariantVerifier(REPORT, "PASS")
    out = await _build(verifier, llm)
    assert out.status == "review" and backend.calls.count("gen_public") == 2  # 한 번만
    assert any(
        "테스트를 더한 뒤에도" in f and "오래된 순으로 돌려줌" in f for f in out.review_flags
    )


async def test_gate_drops_an_added_class_the_references_fail() -> None:
    """더한 클래스가 참조 구현에서 떨어지면 그 클래스만 버린다 — 이미 통과한 공개 테스트·빌드는 그대로."""
    llm, _ = _llm(gen_public=[PUBLIC_ANSWER, _addition(touch_old=False)])
    verifier = VariantVerifier(REPORT, "PASS")
    verifier.ref_results = {"OrderHistoryEdgeTest": "ASSERT"}
    out = await _build(verifier, llm)
    assert out.status == "review"
    assert ADDED_PATH not in {f.path for f in out.overlays["public"].files}
    assert any(
        "참조 구현이 떨어지는 것은 버림" in f and "OrderHistoryEdgeTest" in f
        for f in out.review_flags
    )
    assert any("더한 테스트를 쓸 수 없음" in f for f in out.review_flags)
    assert [r.result for r in out.repairs] == ["failed"]


async def test_gate_flags_when_nothing_new_is_added() -> None:
    from tests.starter.test_repair import PUBLIC_TEST

    same = _files(
        ("src/test/java/com/example/service/publictests/OrderHistoryPublicTest.java", PUBLIC_TEST)
    )
    llm, _ = _llm(gen_public=[PUBLIC_ANSWER, same])
    out = await _build(VariantVerifier(REPORT, "PASS"), llm)
    assert out.status == "review" and any(
        "추가할 테스트를 만들지 못함" in f for f in out.review_flags
    )


async def test_gate_flags_when_no_variant_is_usable() -> None:
    llm, _ = _llm(gen_variant=[_variants()])
    out = await _build(VariantVerifier(REPORT, "ASSERT"), llm)
    assert out.status == "review" and any("쓸 수 있는 변형이 없음" in f for f in out.review_flags)


def _contract_with_rows() -> Any:
    c = _inputs().contract.model_dump()
    c["seed"] = [
        {
            "entity_id": "m1",
            "rows": 20,
            "groups": [{"first": 1, "last": 20, "values": {"status": "READY"}}],
        }
    ]
    c["public_tests"][0]["rows"] = [{"entity_id": "m1", "values": {"status": "READY"}}]
    from task_spec.schemas import TaskContract

    return TaskContract.model_validate(c)


def test_public_input_names_the_rows_each_test_uses() -> None:
    from task_spec.nodes.starter.generate import public_test_line

    contract = _contract_with_rows()
    line = public_test_line(contract, contract.public_tests[0])
    assert line.endswith("쓸 행: PUBLIC_ORDER_READY (status=READY)")
    assert "쓸 행" not in public_test_line(contract, contract.public_tests[1])


def test_static_check_wants_the_declared_rows() -> None:
    from task_spec.starter.static_check import _public_row_uses

    contract = _contract_with_rows()
    pt = contract.public_tests[0].test_id
    body = f'@DisplayName("{pt} 무엇")\nclass A extends PublicCase {{ long id = PUBLIC_ORDER_READY[0]; }}\n'
    ok = Overlay(kind="public_test", files=(GeneratedFile(path="A.java", content=body),))
    assert _public_row_uses(ok, contract) == []
    other = body.replace("PUBLIC_ORDER_READY[0]", "orderIdOf(6L)")
    bad = Overlay(kind="public_test", files=(GeneratedFile(path="A.java", content=other),))
    problems = _public_row_uses(bad, contract)
    assert len(problems) == 1 and "PUBLIC_ORDER_READY" in problems[0].message
    longer = body.replace("PUBLIC_ORDER_READY[0]", "PUBLIC_ORDER_READY_2[0]")
    near = Overlay(kind="public_test", files=(GeneratedFile(path="A.java", content=longer),))
    assert len(_public_row_uses(near, contract)) == 1  # 이름이 앞부분만 같은 다른 상수는 아님


def test_judge_counts_only_the_targeted_criteria_tests() -> None:
    """리뷰 2026-11-13: 다른 기준의 테스트(또는 pt 가 없는 저장 확인)만 떨어지면 그 기준을 가른 근거가 아니다 — 무효."""
    public = Overlay(
        kind="public_test",
        files=(
            GeneratedFile(
                path="t/Pt1Test.java", content='@DisplayName("pt1 성공")\nclass Pt1Test {}'
            ),
            GeneratedFile(
                path="t/Pt2Test.java", content='@DisplayName("pt2 최신순")\nclass Pt2Test {}'
            ),
            GeneratedFile(
                path="t/PersistenceCheckTest.java", content="class PersistenceCheckTest {}"
            ),
        ),
    )
    pts = pts_by_test(public)
    assert pts["Pt2Test"] == {"pt2"} and "PersistenceCheckTest" not in pts
    pt_ac = {"pt1": "p1", "pt2": "p2"}
    specs = [
        VariantSpec("var-p2-omit", "p2", ("p2",), "omit", "정렬 없음", (SVC_PATH,), 1),
        VariantSpec("var-p2-invert", "p2", ("p2",), "invert", "오래된 순", (SVC_PATH,), 1),
        VariantSpec("var-p1-omit", "p1", ("p1",), "omit", "저장 안 함", (SVC_PATH,), 1),
    ]
    report = VerifyReport(
        runs=[
            _run(
                "var-p2-omit",
                "ref_variant",
                _t("public", "Pt1Test", "ASSERT"),
                _t("public", "Pt2Test"),
            ),
            _run("var-p2-invert", "ref_variant", _t("public", "Pt2Test", "ASSERT")),
            _run("var-p1-omit", "ref_variant", _t("public", "PersistenceCheckTest", "ASSERT")),
        ]
    )
    got = {r.label: (r.verdict, r.detail[:20]) for r in judge(report, specs, pts, pt_ac)}
    assert got["var-p2-invert"][0] == "caught"
    assert got["var-p2-omit"][0] == "invalid" and got["var-p2-omit"][1].startswith("다른 기준")
    assert got["var-p1-omit"][0] == "invalid"
    # pt 대응을 모르면(결과 이름으로 못 찾는 스택) 어느 공개 테스트가 떨어져도 잡힘
    assert {r.verdict for r in judge(report, specs)} == {"caught"}


async def test_gate_flags_criteria_without_a_usable_variant() -> None:
    llm, _ = _llm()
    verifier = VariantVerifier(REPORT, "ASSERT")
    verifier.variant_results = []

    async def broken(
        starter: Overlay, variants: Sequence[Overlay], public: Overlay
    ) -> VerifyReport:
        return VerifyReport(
            runs=[_run(v.label, "ref_variant", setup="compile_error") for v in variants]
        )

    verifier.verify_variants = broken  # type: ignore[method-assign]
    out = await _build(verifier, llm)
    assert out.status == "review" and {r.verdict for r in out.variants} == {"invalid"}
    for t in targets(_inputs().task, _inputs().contract):
        assert any(f"요구 {t.rule_id} 는 쓸 수 있는 변형이 없어" in f for f in out.review_flags)


async def test_gate_flags_a_verifier_without_variant_runs() -> None:
    llm, backend = _llm()

    class Plain:
        async def verify(self, *a: Any, **k: Any) -> VerifyReport:
            return REPORT

    out = await _build(Plain(), llm)
    assert out.status == "review" and "gen_variant" not in backend.calls
    assert any("verify_variants" in f for f in out.review_flags)


def test_row_constants_must_exist_in_the_helper() -> None:
    """리뷰 2026-11-13: 공개 몫 id 가 빈 그룹은 도우미에 상수가 없다 — 계획을 주면 있는 이름만."""
    from task_spec.starter.hidden_helpers import public_rows, row_use_consts

    contract = _contract_with_rows()
    plan = _inputs().plan
    use = contract.public_tests[0].rows[0]
    names = row_use_consts(contract, use, plan)
    assert set(names) <= set(public_rows(contract, plan))
    raw = plan.model_dump()
    raw["cases"] = [
        raw["cases"][0]
        | {
            "data": [
                {"entity_id": "m1", "rows": 10, "initial": {"status": "READY"}, "burst": False}
            ]
        }
    ]
    full = type(plan).model_validate(raw)
    assert row_use_consts(contract, use, full) == []  # 11~20 을 숨은 케이스가 다 씀 → 공개 몫 없음
    assert row_use_consts(contract, use) == ["PUBLIC_ORDER_READY"]


async def test_rules_with_a_comparison_get_a_boundary_variant() -> None:
    """비교 규칙이 있는 요구에 경계 변형이 없으면 그 요구만 한 번 더 부르고, 그래도 없으면 missing_boundary (2026-11-13 쿠폰 알파)."""
    from task_spec.nodes.starter.generate import gen_variants

    b = _inputs()
    starter = Overlay(kind="starter", files=())
    rule = VariantTarget("r2", "남은 수량이 요청 수량보다 적으면 거절한다", ("p1",))
    fixed = SERVICE.replace("{}", "{ boolean strict = true; }")
    omit = _variants(("omit", "검사 없음", [(SVC_PATH, BROKEN)]))
    edge = _variants(("boundary", "같을 때도 거절", [(SVC_PATH, fixed)]))
    llm, backend = _llm(gen_variant=[omit, edge])
    out = await gen_variants(
        b.task,
        b.contract,
        b.template,
        starter,
        _base(),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="t",
        only=[rule],
    )
    assert backend.calls.count("gen_variant") == 2 and not out.missing_boundary
    assert sorted(s.op for _, s in out.variants) == ["boundary", "omit"]
    assert "boundary" in [u for n, u in backend.users if n == "gen_variant"][-1]
    llm, backend = _llm(gen_variant=[omit])
    out = await gen_variants(
        b.task,
        b.contract,
        b.template,
        starter,
        _base(),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="t",
        only=[rule],
    )
    assert out.missing_boundary == ["r2"]
    # 비교가 없는 요구는 다시 부르지 않는다
    llm, backend = _llm(gen_variant=[omit])
    plain = VariantTarget("r3", "회원별 기록을 최신순으로 돌려준다", ("p1",))
    await gen_variants(
        b.task,
        b.contract,
        b.template,
        starter,
        _base(),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="t",
        only=[plain],
    )
    assert backend.calls.count("gen_variant") == 1


def test_failed_line_points_at_the_wrong_assertion(tmp_path: Any) -> None:
    """2026-11-13 N+1 알파: AssertJ 메시지만으로는 어느 단언이 틀렸는지 몰라 수리가 같은 단언을 되풀이했다 — 줄과 코드를 붙인다."""
    from task_spec.starter.verify.gradle import failed_line

    src = tmp_path / "src" / "test" / "java" / "Pt3Test.java"
    src.parent.mkdir(parents=True)
    src.write_text(
        "class Pt3Test {\n  void t() {\n    assertThat(found).isTrue();\n  }\n}\n", "utf-8"
    )
    trace = (
        "org.opentest4j.AssertionFailedError: Expecting value to be true but was false\n"
        "\tat org.assertj.Foo.bar(Foo.java:10)\n\tat com.example.Pt3Test.t(Pt3Test.java:3)\n"
    )
    assert failed_line(trace, "Pt3Test") == "Pt3Test.java:3"
    assert (
        failed_line(trace, "Pt3Test", tmp_path / "src" / "test")
        == "Pt3Test.java:3 `assertThat(found).isTrue();`"
    )
    assert failed_line(trace, "OtherTest") == ""


def test_all_fail_repair_note_carries_the_failed_line() -> None:
    from task_spec.starter.repair import _public_refs

    bad = _t("public", PUBLIC_CLASS, "ASSERT", None, "Expecting value to be true but was false")
    bad["at"] = "Pt3Test.java:27 `assertThat(foundIncluded).isTrue();`"
    runs = [
        _run("naive-all", "ref_naive", bad),
        _run("d1", "ref_accepted", bad),
    ]
    actions, _ = _public_refs(runs, {})
    note = next(a.note for a in actions if a.generator == "gen_public")
    assert "실패한 줄: Pt3Test.java:27 `assertThat(foundIncluded).isTrue();`" in note
    assert "실제 데이터에서 성립" in note


def test_public_helper_gives_actual_times_of_public_rows() -> None:
    """같은 시각 그룹·지어낸 날짜 대신 — 공개 몫 행의 실제 시각을 도우미가 준다 (그룹 값이 있으면 그 값, 없으면 seed_sql 기본값)."""
    from task_spec.schemas import TaskContract
    from task_spec.starter.hidden_helpers import time_values
    from task_spec.starter.seed_sql import time_value

    c = _inputs().contract.model_dump()
    ent = c["entities"][0]
    ent["fields"].append({"name": "createdAt", "type": "datetime", "refs": ["r1"]})
    c["seed"] = [
        {
            "entity_id": ent["entity_id"],
            "rows": 20,
            "groups": [
                {"first": 1, "last": 15, "values": {"status": "READY"}},
                {
                    "first": 16,
                    "last": 20,
                    "values": {"status": "DONE", "createdAt": "2026-01-02T00:00:00"},
                },
            ],
        }
    ]
    contract = TaskContract.model_validate(c)
    plan = _inputs().plan
    vals = time_values(contract, plan)
    name = ent["name"][:1].lower() + ent["name"][1:] + "CreatedAtOf"
    assert name in vals
    got = vals[name]
    assert all(v == time_value("datetime", r) for r, v in got.items() if r <= 15)
    assert all(v == "2026-01-02T00:00:00" for r, v in got.items() if r >= 16)
    assert len(set(v for r, v in got.items() if r <= 15)) == len(
        [r for r in got if r <= 15]
    )  # 같은 값 없음


def test_variants_change_one_place_and_split_rules_spread_over_places() -> None:
    """1차 변이 — 연속한 줄 한 덩어리만 바꾼다. 2026-11-13 N+1: '이름 null + 개수 0' 을 한 변형으로 바꿔 이름 확인에 잡혔고,
    '개수만 0' 인 구현이 통과하는 빈틈(itemCount ≥ 0)을 못 봤다. 같은 규칙이 여러 곳이면 곳마다 따로 받는다."""
    from task_spec.starter.variants import changed_hunks

    base_text = "class S {\n  String name = n;\n  int a = 1;\n  int b = 2;\n  long count = c;\n}\n"
    impl = {SVC_PATH: base_text}
    base = Overlay(
        kind="ref_accepted", files=(GeneratedFile(path=SVC_PATH, content=base_text),), label="d1"
    )
    both = base_text.replace("= n;", "= null;").replace("= c;", "= 0;")
    name_only = base_text.replace("= n;", "= null;")
    count_only = base_text.replace("= c;", "= 0;")
    assert changed_hunks(base_text, both) == 2 and changed_hunks(base_text, name_only) == 1
    out = GenVariantOutput.model_validate(
        {
            "variants": [
                {
                    "op": "omit",
                    "note": "이름·개수 모두 없음",
                    "files": [{"path": SVC_PATH, "content": both}],
                },
                {
                    "op": "omit",
                    "note": "이름 없음",
                    "files": [{"path": SVC_PATH, "content": name_only}],
                },
                {
                    "op": "omit",
                    "note": "개수 늘 0",
                    "files": [{"path": SVC_PATH, "content": count_only}],
                },
            ]
        }
    )
    kept, dropped = build_variants(VariantTarget("r2", "이름과 개수", ("p2",)), out, base, impl)
    assert [s.note for _, s in kept] == ["이름 없음", "개수 늘 0"]
    assert [s.label for _, s in kept] == ["var-r2-omit", "var-r2-omit2"]
    assert len(dropped) == 1 and "2군데" in dropped[0]


def test_renaming_one_name_at_declaration_and_call_is_one_place() -> None:
    """2026-11-15 손 시범 retry-ae1005: 정렬이 Spring Data 파생 메서드 이름에 있어 Asc 로 바꾸려면 선언·호출 두 곳을 같이 고쳐야 했다 —
    이름 하나만 바꾼 것은 한 곳으로 센다. 서로 다른 두 곳을 고친 것(낱말 쌍 둘)은 그대로 버린다."""
    from task_spec.starter.variants import renamed

    repo = "interface R {\n  List<C> findByMemberIdOrderByCreatedAtDesc(Long m);\n}\n"
    svc = "class S {\n  int x = 1;\n  List<C> list(Long m) { return r.findByMemberIdOrderByCreatedAtDesc(m); }\n}\n"
    impl = {"R.java": repo, "S.java": svc}
    asc = "OrderByCreatedAtAsc"
    flip = GenVariantOutput.model_validate(
        {
            "variants": [
                {
                    "op": "invert",
                    "note": "오름차순",
                    "files": [
                        {"path": "R.java", "content": repo.replace("OrderByCreatedAtDesc", asc)},
                        {"path": "S.java", "content": svc.replace("OrderByCreatedAtDesc", asc)},
                    ],
                },
                {  # 이름 바꾸기 + 다른 곳(x = 1 → 2) — 두 곳
                    "op": "omit",
                    "note": "두 곳",
                    "files": [
                        {"path": "R.java", "content": repo.replace("OrderByCreatedAtDesc", asc)},
                        {
                            "path": "S.java",
                            "content": svc.replace("OrderByCreatedAtDesc", asc).replace(
                                "x = 1", "x = 2"
                            ),
                        },
                    ],
                },
            ]
        }
    )
    kept, dropped = build_variants(VariantTarget("r2", "최신순", ("p2",)), flip, _base(), impl)
    assert [s.label for _, s in kept] == ["var-r2-invert"]
    assert any("바꾼 곳" in d for d in dropped)
    assert renamed(repo, repo.replace("Desc", "Asc")) == {
        ("findByMemberIdOrderByCreatedAtDesc", "findByMemberIdOrderByCreatedAtAsc")
    }
    assert renamed("a = b + 1;\n", "a = b - 1;\n") is None  # 낱말이 아닌 글자가 바뀜
