"""검증 실패 배정(starter/repair.py)과 빌드 루프(nodes/starter/build.py) — 가짜 verifier·가짜 LLM, 가상 데이터만.

배정 표(spec-to-code §7)의 행마다 가짜 검증 결과를 만들어 어느 생성기를 다시 돌리는지 본다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from task_spec.llm import Completion, LLMClient
from task_spec.nodes.starter.build import build_and_repair
from task_spec.prompting import PromptRegistry
from task_spec.schemas import GeneratedFile, HiddenPlan, TargetRun, VerifyReport
from task_spec.starter.repair import assign
from task_spec.starter.static_check import Overlay
from tests.starter.test_generate import D1, D2, SERVICE, STARTER, _files, _inputs
from tests.starter.test_static_check import CASES, HC1, PKG

PROMPTS = PromptRegistry(__import__("pathlib").Path(__file__).resolve().parents[2] / "prompts")

PLAN2 = HiddenPlan.model_validate(
    {
        "harness": {"pool_size": 8, "concurrency": 32, "bursts": 3},
        "cases": [
            {
                "case_id": "hc1",
                "ac_id": "h1",
                "trap_id": "t1",
                "scenario": "동시 요청",
                "expect": "모두 정상 응답",
                "intended_reason": "pool_acquire_timeout",
            },
            {
                "case_id": "hc2",
                "ac_id": "h2",
                "trap_id": "t1",
                "scenario": "저장 실패 주입",
                "expect": "실패 기록만 남음",
                "intended_reason": "rollback_left_success",
            },
        ],
    }
)
OVERLAYS = {
    "starter": Overlay(
        kind="starter", files=(GeneratedFile(path=f"{PKG}/order/Order.java", content="x"),)
    ),
    "hidden": Overlay(
        kind="hidden", files=(GeneratedFile(path=f"{CASES}/Hc1Test.java", content="x"),)
    ),
    "naive-all": Overlay(
        kind="ref_naive",
        label="naive-all",
        files=(GeneratedFile(path=f"{PKG}/order/OrderService.java", content="x"),),
    ),
    "d1": Overlay(
        kind="ref_accepted",
        label="d1",
        files=(GeneratedFile(path=f"{PKG}/order/OrderService.java", content="x"),),
    ),
}
MISSED = {"naive-all": {"t1"}}


def _t(
    group: str, name: str, result: str = "PASS", reason: str | None = None, msg: str = ""
) -> dict[str, Any]:
    return {"group": group, "name": name, "result": result, "reason": reason, "message": msg}


def _run(target: str, kind: str, *tests: dict[str, Any], **kw: Any) -> TargetRun:
    base = [_t("existing", "OrderControllerTest")]
    return TargetRun.model_validate(
        {"target": target, "kind": kind, "tests": [*base, *tests], **kw}
    )


GOOD_NAIVE = (
    _t("hidden", "hc1", "ASSERT", "pool_acquire_timeout", "기대와 다른 응답 수 32"),
    _t("hidden", "hc2", "ASSERT", "rollback_left_success", "성공 기록이 남음"),
)
GOOD_ACC = (_t("hidden", "hc1"), _t("hidden", "hc2"))


def _report(*runs: TargetRun) -> VerifyReport:
    return VerifyReport(runs=[_run("starter", "starter"), *runs])


def _assign(report: VerifyReport, **counts: int):  # type: ignore[no-untyped-def]
    return assign(
        report, PLAN2, OVERLAYS, MISSED, {k.replace("__", ":"): v for k, v in counts.items()}
    )


def _keys(plan) -> list[str]:  # type: ignore[no-untyped-def]
    return sorted(a.key for a in plan.actions)


def test_all_gates_pass() -> None:
    plan = _assign(
        _report(_run("naive-all", "ref_naive", *GOOD_NAIVE), _run("d1", "ref_accepted", *GOOD_ACC))
    )
    assert plan.passed


def test_starter_problems_come_first() -> None:
    broken = VerifyReport(
        runs=[
            TargetRun.model_validate(
                {
                    "target": "starter",
                    "kind": "starter",
                    "setup": "compile_error",
                    "compile_errors": [
                        {
                            "path": f"{PKG}/order/Order.java",
                            "line": 3,
                            "message": "cannot find symbol",
                        }
                    ],
                }
            ),
            _run("naive-all", "ref_naive", setup="compile_error"),  # 시작 코드가 깨지면 보지 않는다
        ]
    )
    plan = _assign(broken)
    assert _keys(plan) == ["gen_starter"] and plan.actions[0].gate == "①"
    assert "Order.java:3 cannot find symbol" in plan.actions[0].note
    # ② 시작 코드 자체 테스트 실패 (2026-11-07 스모크: 400 기대인데 200)
    report = VerifyReport(
        runs=[
            TargetRun.model_validate(
                {
                    "target": "starter",
                    "kind": "starter",
                    "tests": [
                        _t(
                            "existing",
                            "OrderControllerTest",
                            "ASSERT",
                            msg="Status expected:<400> but was:<200>",
                        )
                    ],
                }
            ),
            _run("d1", "ref_accepted", *GOOD_ACC),
        ]
    )
    plan = _assign(report)
    assert _keys(plan) == ["gen_starter"] and plan.actions[0].gate == "②"
    assert "OrderControllerTest: Status expected:<400> but was:<200>" in plan.actions[0].note


def test_compile_errors_go_to_the_file_owner() -> None:
    def err(path: str) -> dict[str, Any]:
        return {"path": path, "line": 48, "message": "actual has private access in Outcome"}

    run = _run(
        "naive-all",
        "ref_naive",
        setup="compile_error",
        compile_errors=[
            err(f"{CASES}/Hc1Test.java"),
            err(f"{PKG}/order/OrderService.java"),
            err(f"{PKG}/order/Order.java"),
        ],
    )
    plan = _assign(_report(run, _run("d1", "ref_accepted", *GOOD_ACC)))
    assert _keys(plan) == ["gen_hidden", "gen_naive:naive-all"]
    naive = next(a for a in plan.actions if a.generator == "gen_naive")
    assert (
        "OrderService.java:48" in naive.note and "Order.java:48" in naive.note
    )  # 시작 코드 파일 오류도 구현 탓
    hidden = next(a for a in plan.actions if a.generator == "gen_hidden")
    assert "Hc1Test.java:48" in hidden.note and "OrderService" not in hidden.note


def test_boot_error_and_timeout() -> None:
    plan = _assign(
        _report(
            _run("d1", "ref_accepted", setup="boot_error"),
            _run("naive-all", "ref_naive", setup="timeout"),
        )
    )
    assert _keys(plan) == ["gen_accepted:d1", "gen_hidden"]
    assert next(a for a in plan.actions if a.generator == "gen_hidden").gate == "⑨"


def test_ref_breaking_existing_test_is_the_refs_fault() -> None:
    """2026-11-07 스모크: 흔한 구현이 필수 필드를 빠뜨려 기존 테스트가 깨짐 — 누설(⑤ gen_starter)보다 구현을 먼저."""
    run = TargetRun.model_validate(
        {
            "target": "naive-all",
            "kind": "ref_naive",
            "tests": [
                _t(
                    "existing",
                    "OrderControllerTest",
                    "ASSERT",
                    msg="not-null property references a null value",
                ),
                *GOOD_NAIVE,
            ],
        }
    )
    plan = _assign(_report(run, _run("d1", "ref_accepted", *GOOD_ACC)))
    assert _keys(plan) == ["gen_naive:naive-all"] and plan.actions[0].gate == "⑤"
    assert "not-null property" in plan.actions[0].note


def test_broken_or_missing_hidden_case() -> None:
    naive = _run(
        "naive-all",
        "ref_naive",
        _t("hidden", "hc1", "HARNESS_ERR", msg="IllegalStateException: 충분한 id 가 없음"),
    )  # hc2 결과 없음
    plan = _assign(_report(naive, _run("d1", "ref_accepted", *GOOD_ACC)))
    assert _keys(plan) == ["gen_hidden"]
    note = plan.actions[0].note
    assert "hc1 가 단언이 아닌 예외" in note and "hc2 가 실행되지 않았다" in note


@pytest.mark.parametrize(
    ("hc2", "needle"),
    [
        (
            _t("hidden", "hc2", "ASSERT", "record_missing", "실패 기록 없음"),
            "의도한 사유(rollback_left_success) 대신 record_missing",  # 2026-11-07 스모크
        ),
        (_t("hidden", "hc2", "ASSERT", None, "expected 1 but was 0"), "대신 사유 표시 없음"),
    ],
)
def test_gate3_naive_must_fail_for_the_intended_reason(hc2: dict[str, Any], needle: str) -> None:
    naive = _run("naive-all", "ref_naive", GOOD_NAIVE[0], hc2)
    plan = _assign(_report(naive, _run("d1", "ref_accepted", *GOOD_ACC)))
    assert _keys(plan) == ["gen_hidden"] and plan.actions[0].gate == "③"
    assert needle in plan.actions[0].note


def test_naive_passing_a_case_is_regenerated_before_the_hidden_test() -> None:
    """2026-11-07 배송 감사 재실행: 흔한 구현이 REQUIRES_NEW 를 상태 변경 트랜잭션 밖에서 불러 hc1 을 통과 — 숨은 테스트는 맞았다.
    흔한 구현을 먼저 다시 만들고 (그 회차엔 같은 대상의 사유 불일치 배정은 미룸), 이미 다시 만들었으면 숨은 테스트를 의심한다."""
    naive = _run(
        "naive-all",
        "ref_naive",
        _t("hidden", "hc1"),
        _t("hidden", "hc2", "ASSERT", "record_missing", "실패 기록 없음"),
    )
    report = _report(naive, _run("d1", "ref_accepted", *GOOD_ACC))
    first = _assign(report)
    assert _keys(first) == ["gen_naive:naive-all"] and first.actions[0].gate == "③"
    assert "hc1" in first.actions[0].note and "구조 그대로" in first.actions[0].note
    again = _assign(report, **{"gen_naive__naive-all": 1})
    assert _keys(again) == ["gen_hidden"]
    note = again.actions[0].note
    assert (
        "흔한 구현(naive-all)이 숨은 케이스 hc1 를 통과" in note and "hc2 가 흔한 구현에서" in note
    )
    # 컴파일 수리로 다시 만든 것은 세지 않는다 (예산 키가 따로)
    assert _keys(_assign(report, **{"gen_naive__naive-all#compile": 1})) == ["gen_naive:naive-all"]


def test_starter_that_already_passes_every_hidden_case_is_regenerated() -> None:
    """2026-11-07 알파 테스트 (새 공고): 시작 코드가 새 기능까지 구현해 시작 코드 그대로 숨은 케이스를 통과 — 과제가 이미 풀림."""
    refs = (_run("naive-all", "ref_naive", *GOOD_NAIVE), _run("d1", "ref_accepted", *GOOD_ACC))
    solved = _run("starter+hidden", "starter_hidden", *GOOD_ACC)
    plan = _assign(_report(solved, *refs))
    assert _keys(plan) == ["gen_starter"] and plan.actions[0].gate == "②'"
    assert "아무것도 하지 않은" in plan.actions[0].note
    # 정상: 시작 코드 그대로는 숨은 케이스가 실패 — 나머지 게이트도 통과
    bare = _run(
        "starter+hidden",
        "starter_hidden",
        _t("hidden", "hc1", "HARNESS_ERR", msg="500"),
        _t("hidden", "hc2", "ASSERT", "record_missing", "x"),
    )
    assert _assign(_report(bare, *refs)).passed
    # 일부만 통과하면 막지 않는다 (부정 단언만 있는 케이스는 빈 구현으로도 통과할 수 있음)
    partly = _run(
        "starter+hidden",
        "starter_hidden",
        _t("hidden", "hc1"),
        _t("hidden", "hc2", "ASSERT", "record_missing", "x"),
    )
    assert _assign(_report(partly, *refs)).passed
    # 숨은 테스트가 컴파일되지 않으면 여기서 보지 않는다 (참조 구현 쪽에서 배정)
    broken = _run("starter+hidden", "starter_hidden", setup="compile_error")
    assert _assign(_report(broken, *refs)).passed


def test_gate3_prime_kept_requirement_must_pass() -> None:
    plan2 = PLAN2.model_copy(
        update={"cases": [PLAN2.cases[0], PLAN2.cases[1].model_copy(update={"trap_id": "t2"})]}
    )
    miss_t1 = _run(
        "naive-miss-t1",
        "ref_naive",
        GOOD_NAIVE[0],
        _t("hidden", "hc2", "ASSERT", "record_missing", "x"),
    )
    plan = assign(
        _report(miss_t1, _run("d1", "ref_accepted", *GOOD_ACC)),
        plan2,
        OVERLAYS,
        {"naive-miss-t1": {"t1"}},
    )
    assert _keys(plan) == ["gen_hidden"] and plan.actions[0].gate == "③'"


def test_gate4_accepted_designs() -> None:
    fail = _t("hidden", "hc2", "ASSERT", "record_missing", "실패 기록 없음")
    d1, d2 = (
        _run("d1", "ref_accepted", GOOD_ACC[0], fail),
        _run("d2", "ref_accepted", GOOD_ACC[0], fail),
    )
    naive = _run("naive-all", "ref_naive", *GOOD_NAIVE)
    # 둘 이상이 모두 실패 → 숨은 테스트가 너무 엄격
    plan = _assign(_report(naive, d1, d2))
    assert _keys(plan) == ["gen_hidden"] and "모두(d1, d2)" in plan.actions[0].note
    # 일부만 실패 → 그 구현
    plan = _assign(_report(naive, d1, _run("d2", "ref_accepted", *GOOD_ACC)))
    assert _keys(plan) == ["gen_accepted:d1"] and "설계 조건과 스택 조건" in plan.actions[0].note
    # 허용 구현이 하나뿐: 먼저 구현 → 다시 만든 뒤에도 실패면 숨은 테스트 → 둘 다 다시 만들었으면 사람 확인
    assert _keys(_assign(_report(naive, d1))) == ["gen_accepted:d1"]
    # 다시 만든 뒤에도 실패: 흔한 구현이 그 케이스를 의도한 사유로 잡으면 테스트는 제 몫 → 설계를 못 따름 (사람 확인, 2026-11-07 루프)
    plan = _assign(_report(naive, d1), gen_accepted__d1=1)
    assert not plan.actions and any("설계를 못 따름" in u for u in plan.unresolved)
    # 그런 근거가 없으면(흔한 구현 결과 없음) 허용 구현이 하나뿐일 때만 숨은 테스트를 의심
    assert _keys(_assign(_report(d1), gen_accepted__d1=1)) == ["gen_hidden"]
    plan = _assign(_report(d1), gen_accepted__d1=1, gen_hidden=1)
    assert not plan.actions and any("사람 확인" in u for u in plan.unresolved)
    # 허용 구현이 여럿인데 다시 만든 하나가 또 혼자 실패 → 설계 조건 부족
    plan = _assign(_report(naive, d1, _run("d2", "ref_accepted", *GOOD_ACC)), gen_accepted__d1=1)
    assert not plan.actions and plan.unresolved


def test_same_generator_actions_are_merged() -> None:
    naive = _run(
        "naive-all",
        "ref_naive",
        _t("hidden", "hc1", "ASSERT", None, "expected 0 but was 3"),
        _t("hidden", "hc2", "ASSERT", "record_missing", "x"),
    )
    plan = _assign(_report(naive, _run("d1", "ref_accepted", *GOOD_ACC)))
    assert _keys(plan) == ["gen_hidden"]
    note = plan.actions[0].note
    assert "hc1 가 흔한 구현에서" in note and "hc2 가 흔한 구현에서" in note


# ── 빌드 루프 ────────────────────────────────────────────────


class RoutedBackend:
    """프롬프트(생성기)별로 준비한 응답을 차례로 — 병렬 호출 순서와 무관."""

    MARKERS = {
        "gen_starter": "**시작 코드**",
        "gen_hidden": "**숨은 테스트**",
        "gen_naive": "흔한 구현",
        "gen_accepted": "**허용 설계 하나**",
    }

    def __init__(self, **routes: list[Completion]) -> None:
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[str] = []
        self.users: list[tuple[str, str]] = []

    async def complete(self, model: str, messages: list[Any], params: dict[str, Any]) -> Completion:
        system = messages[0].content
        name = next(k for k, m in self.MARKERS.items() if m in system)
        self.calls.append(name)
        self.users.append((name, messages[-1].content))
        queue = self.routes.get(name) or []
        if not queue:
            raise AssertionError(f"{name}: 준비된 응답 없음")
        return queue.pop(0) if len(queue) > 1 else queue[0]


class ScriptedVerifier:
    def __init__(self, *reports: VerifyReport) -> None:
        self.reports = list(reports)
        self.seen: list[list[str]] = []

    async def verify(
        self, starter: Overlay, hidden: Overlay, refs: Sequence[Overlay]
    ) -> VerifyReport:
        self.seen.append([o.label for o in refs])
        return self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]


SVC = _files((f"{PKG}/order/OrderService.java", SERVICE))
HIDDEN = _files((f"{CASES}/OrderStatusBurstTest.java", HC1))


def _llm(**over: list[Completion]) -> tuple[LLMClient, RoutedBackend]:
    routes = {
        "gen_starter": [_files(*STARTER)],
        "gen_hidden": [HIDDEN],
        "gen_naive": [SVC],
        "gen_accepted": [SVC],
    } | over
    backend = RoutedBackend(**routes)
    return LLMClient({"opencode": backend, "openai": backend}, sleep=_no_sleep), backend  # type: ignore[dict-item]


async def _no_sleep(_: float) -> None:
    return None


def _one_case(*runs: TargetRun) -> VerifyReport:
    return VerifyReport(runs=[_run("starter", "starter"), *runs])


NAIVE_OK = _run(
    "naive-all", "ref_naive", _t("hidden", "hc1", "ASSERT", "pool_acquire_timeout", "32")
)
D1_OK = _run("d1", "ref_accepted", _t("hidden", "hc1"))
D1_FAIL = _run(
    "d1", "ref_accepted", _t("hidden", "hc1", "ASSERT", "pool_acquire_timeout", "3 건 시간 초과")
)


async def _build(verifier: ScriptedVerifier, llm: LLMClient, designs=(D1,)):  # type: ignore[no-untyped-def]
    return await build_and_repair(
        _inputs(),
        designs,
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=verifier,
    )


async def test_loop_passes_without_repair() -> None:
    llm, backend = _llm()
    out = await _build(ScriptedVerifier(_one_case(NAIVE_OK, D1_OK)), llm)
    assert out.status == "review" and out.rounds == 0 and not out.repairs
    assert sorted(backend.calls) == ["gen_accepted", "gen_hidden", "gen_naive", "gen_starter"]
    assert set(out.overlays) == {"starter", "hidden", "naive-all", "d1"}


async def test_starter_repair_regenerates_refs_too() -> None:
    broken = VerifyReport(
        runs=[
            TargetRun.model_validate(
                {
                    "target": "starter",
                    "kind": "starter",
                    "tests": [_t("existing", "OrderControllerTest", "ASSERT", msg="400≠200")],
                }
            )
        ]
    )
    llm, backend = _llm()
    out = await _build(ScriptedVerifier(broken, _one_case(NAIVE_OK, D1_OK)), llm)
    assert out.status == "review" and out.rounds == 1
    assert out.regen_counts == {"gen_starter": 1}  # 참조 구현은 예산을 쓰지 않는다
    assert backend.calls.count("gen_starter") == 2 and backend.calls.count("gen_naive") == 2
    assert backend.calls.count("gen_accepted") == 2 and backend.calls.count("gen_hidden") == 1
    assert [(r.gate, r.target, r.result) for r in out.repairs] == [
        ("②", "gen_starter", "regenerated")
    ]


async def test_single_accepted_design_regenerated_then_human() -> None:
    """허용 구현을 다시 만들어도 실패하고, 흔한 구현은 그 케이스를 의도한 사유로 실패 → 숨은 테스트를 건드리지 않고 사람 확인."""
    llm, backend = _llm()
    out = await _build(ScriptedVerifier(_one_case(NAIVE_OK, D1_FAIL)), llm)
    assert out.status == "failed" and out.rounds == 1
    assert [(r.target, r.result) for r in out.repairs] == [("gen_accepted", "regenerated")]
    assert any("설계를 못 따름" in f for f in out.review_flags)
    assert backend.calls.count("gen_accepted") == 2 and backend.calls.count("gen_hidden") == 1
    assert len(out.snapshots) == 2 and out.snapshots[0]["d1"] is not out.snapshots[1]["d1"]


async def test_budget_exhausted_fails_with_flag() -> None:
    wrong = _run("naive-all", "ref_naive", _t("hidden", "hc1", "ASSERT", "record_missing", "x"))
    llm, _ = _llm()
    out = await _build(ScriptedVerifier(_one_case(wrong, D1_OK)), llm)
    assert out.status == "failed"
    assert any("재생성 예산을 다 씀 — gen_hidden" in f for f in out.review_flags)
    assert [(r.target, r.result) for r in out.repairs] == [
        ("gen_hidden", "regenerated"),
        ("gen_hidden", "skipped"),
    ]


async def test_generation_failure_stops_before_verify() -> None:
    bad = _files(("build.gradle", "plugins {}"))  # 잠긴 파일 — 재요청에도 같은 답
    llm, _ = _llm(gen_starter=[bad])
    verifier = ScriptedVerifier(_one_case(NAIVE_OK, D1_OK))
    out = await _build(verifier, llm)
    assert out.status == "failed" and not verifier.seen
    assert any("gen_starter 생성 실패" in f for f in out.review_flags)


async def test_two_designs_and_static_cross_check() -> None:
    llm, _ = _llm()
    verifier = ScriptedVerifier(
        _one_case(NAIVE_OK, D1_OK, _run("d2", "ref_accepted", _t("hidden", "hc1")))
    )
    out = await _build(verifier, llm, designs=(D1, D2))
    assert out.status == "review" and verifier.seen == [["naive-all", "d1", "d2"]]


NOT_NULL = "DataIntegrityViolationException: not-null property references a null value : AuditLog.beforeStatus (id=999)"


def _breaks(target: str, kind: str, msg: str) -> TargetRun:
    tests = [_t("existing", "OrderControllerTest", "HARNESS_ERR", msg=msg)]
    tests += list(GOOD_NAIVE if kind == "ref_naive" else GOOD_ACC)
    return TargetRun.model_validate({"target": target, "kind": kind, "tests": tests})


def test_all_refs_breaking_the_same_test_the_same_way_is_contract_ambiguity() -> None:
    """2026-11-07 수리 루프: 계약이 이전 상태를 필수로 둬, 두 구현 모두 없는 배송 건 요청을 기록하다 같은 not-null 오류."""
    plan = _assign(
        _report(
            _breaks("naive-all", "ref_naive", NOT_NULL),
            _breaks("d1", "ref_accepted", NOT_NULL.replace("999", "1000")),
        )
    )
    assert not plan.actions  # 숫자만 다르면 같은 오류 — 다시 만들지 않는다
    assert len(plan.unresolved) == 1 and "계약 모호 의심" in plan.unresolved[0]
    assert "OrderControllerTest" in plan.unresolved[0] and "beforeStatus" in plan.unresolved[0]


def test_different_errors_or_one_ref_are_regenerated_as_before() -> None:
    other = "AssertionError: Status expected:<404> but was:<500>"
    plan = _assign(
        _report(_breaks("naive-all", "ref_naive", NOT_NULL), _breaks("d1", "ref_accepted", other))
    )
    assert _keys(plan) == ["gen_accepted:d1", "gen_naive:naive-all"] and not plan.unresolved
    plan = _assign(
        _report(_breaks("naive-all", "ref_naive", NOT_NULL), _run("d1", "ref_accepted", *GOOD_ACC))
    )
    assert _keys(plan) == ["gen_naive:naive-all"] and not plan.unresolved


def test_same_failure_as_naive_is_not_evidence_against_the_accepted_design() -> None:
    """2026-11-07 루프: 계약에 상태 전이 규칙이 없어 숨은 테스트가 무효한 전이를 골랐고, 'FAILURE 있음' 단언에 의도한 사유가
    잘못 붙어 흔한 구현과 허용 구현이 같은 단언·메시지로 실패했다 — 허용 구현 탓으로 보지 않는다."""
    same = _t(
        "hidden",
        "hc2",
        "ASSERT",
        "rollback_left_success",
        "[성공 기록이 남음 21] Expecting value to be true but was false",
    )
    naive = _run("naive-all", "ref_naive", GOOD_NAIVE[0], same)
    d1 = _run(
        "d1", "ref_accepted", GOOD_ACC[0], same | {"message": same["message"].replace("21", "22")}
    )
    first = _assign(_report(naive, d1))
    assert _keys(first) == [
        "gen_accepted:d1"
    ]  # 처음엔 구현을 한 번 다시 (같은 함정에 빠졌을 수도 있다)
    again = _assign(_report(naive, d1), gen_accepted__d1=1)
    assert not again.actions
    assert any(
        "숨은 테스트·계약 의심" in u and "같은 단언·같은 메시지" in u for u in again.unresolved
    )


class _Pinned:
    def __init__(self, **pins: int) -> None:
        self.pins = pins

    def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
        return PROMPTS.get(task, version or self.pins.get(task))


async def test_repair_passes_the_generators_own_previous_output() -> None:
    """수리 때 다시 만드는 생성기는 자기 이전 출력을 받는다 (수리판 프롬프트) — 처음부터 다시 쓰지 않게."""
    llm, backend = _llm()
    verifier = ScriptedVerifier(_one_case(NAIVE_OK, D1_FAIL), _one_case(NAIVE_OK, D1_OK))
    out = await build_and_repair(
        _inputs(),
        (D1,),
        llm=llm,
        prompts=_Pinned(gen_starter=3, gen_hidden=2, gen_naive=2, gen_accepted=2),  # type: ignore[arg-type]
        ontology_version="onto-test",
        verifier=verifier,
    )
    assert out.status == "review" and out.rounds == 1
    accepted = [u for name, u in backend.users if name == "gen_accepted"]
    assert len(accepted) == 2
    assert "(없음)" in accepted[0].split("이전 출력 (수리일 때만")[1][:80]
    assert (
        f"=== {PKG}/order/OrderService.java ===" in accepted[1].split("이전 출력 (수리일 때만")[1]
    )


def _compile_broken(file: str = "OrderStatusBurstTest.java") -> VerifyReport:
    hidden_err = {
        "path": f"{CASES}/{file}",
        "line": 52,
        "message": "reference to assertThat is ambiguous",
    }
    return VerifyReport(
        runs=[
            _run("starter", "starter"),
            _run("naive-all", "ref_naive", setup="compile_error", compile_errors=[hidden_err]),
            _run("d1", "ref_accepted", setup="compile_error", compile_errors=[hidden_err]),
        ]
    )


def test_compile_actions_are_marked() -> None:
    plan = _assign(_compile_broken("Hc1Test.java"))  # OVERLAYS 의 숨은 테스트 파일
    assert [(a.key, a.compile) for a in plan.actions] == [("gen_hidden", True)]
    # 컴파일이 아닌 사유와 합쳐지면 표시를 잃는다 (예산 1)
    naive = _run(
        "naive-all",
        "ref_naive",
        _t("hidden", "hc1"),
        _t("hidden", "hc2", "ASSERT", "record_missing", "x"),
    )
    assert [
        a.compile for a in _assign(_report(naive, _run("d1", "ref_accepted", *GOOD_ACC))).actions
    ] == [False]


async def test_compile_errors_get_two_repairs() -> None:
    """2026-11-07 루프: 숨은 테스트 컴파일 오류를 1회 수리가 틀린 방법으로 고쳐 예산이 끝났다 → 컴파일 오류만이면 2회."""
    llm, backend = _llm()
    verifier = ScriptedVerifier(_compile_broken(), _compile_broken(), _one_case(NAIVE_OK, D1_OK))
    out = await _build(verifier, llm)
    assert out.status == "review" and out.rounds == 2
    assert out.regen_counts == {"gen_hidden#compile": 2}  # 컴파일 수리는 일반 예산과 따로
    assert backend.calls.count("gen_hidden") == 3
    # 세 번째도 컴파일 오류면 예산 소진
    llm, _ = _llm()
    out = await _build(ScriptedVerifier(_compile_broken()), llm)
    assert out.status == "failed" and any(
        "재생성 예산을 다 씀 — gen_hidden" in f for f in out.review_flags
    )
