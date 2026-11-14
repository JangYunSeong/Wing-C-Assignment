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
from task_spec.starter.repair import DESIGN, assign
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


def test_broken_or_missing_hidden_case_is_assigned_by_comparing_implementations() -> None:
    """2026-11-08: 한 구현만 계약과 다른 응답 모양을 돌려 숨은 테스트가 파싱하다 예외 — 그 구현 탓이지 테스트 탓이 아니다."""
    err = _t(
        "hidden",
        "hc1",
        "HARNESS_ERR",
        msg="MismatchedInputException: Cannot deserialize List from Object",
    )
    # 흔한 구현에서만 예외(hc1)·결과 없음(hc2), 허용 구현은 판정까지 감 → 흔한 구현을 다시
    naive = _run("naive-all", "ref_naive", err)
    plan = _assign(_report(naive, _run("d1", "ref_accepted", *GOOD_ACC)))
    assert _keys(plan) == ["gen_naive:naive-all"]
    note = plan.actions[0].note
    assert "hc1 가 이 구현(naive-all)에서만 단언이 아닌 예외" in note and "다른 구현(d1)" in note
    assert "hc2 가 이 구현(naive-all)에서만 실행되지 않았다" in note
    # 모든 구현에서 → 숨은 테스트
    both = _assign(
        _report(
            _run("naive-all", "ref_naive", err),
            _run("d1", "ref_accepted", err),
        )
    )
    assert _keys(both) == ["gen_hidden"]
    assert "모든 구현에서 판정까지 가지 못했다" in both.actions[0].note
    # 준비된 구현이 하나뿐이면 비교할 수 없어 숨은 테스트
    alone = _assign(
        _report(
            _run("naive-all", "ref_naive", err, GOOD_NAIVE[1]),
            _run("d1", "ref_accepted", setup="boot_error"),
        )
    )
    assert "gen_hidden" in _keys(alone)


@pytest.mark.parametrize(
    ("hc2", "needle"),
    [
        (
            _t("hidden", "hc2", "ASSERT", "record_missing", "실패 기록 없음"),
            "의도한 사유(rollback_left_success) 대신 record_missing",  # 2026-11-07 스모크
        ),
        (_t("hidden", "hc2", "ASSERT", None, "expected 1 but was 0"), "사유 표시 없는 단언"),
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
    assert first.actions[0].budget_key == "gen_naive:naive-all#naive-first"
    again = _assign(report, **{"gen_naive__naive-all#naive-first": 1})
    assert _keys(again) == ["gen_hidden"]
    note = again.actions[0].note
    assert (
        "일어나야 한다**고 단언하지 않는다" in note and "통과 조건" in note
    )  # 2026-11-07 알파 5회차: 단언을 거꾸로 씀
    assert (
        "흔한 구현(naive-all)이 숨은 케이스 hc1 를 통과" in note and "hc2 가 흔한 구현에서" in note
    )
    # 컴파일 수리·다른 게이트로 다시 만든 것은 세지 않는다 (예산 키가 따로).
    # 2026-11-09 알파: 2회차 ⑤(기존 테스트 깨짐) 재생성 뒤 3회차 흔한 구현이 구조를 버리고 통과했는데 숨은 테스트로 갔다
    for other in ("gen_naive__naive-all#compile", "gen_naive__naive-all"):
        assert _keys(_assign(report, **{other: 1})) == ["gen_naive:naive-all"], other


def test_naive_that_shows_the_trap_elsewhere_is_not_regenerated() -> None:
    """2026-11-09 v19-loop1: 흔한 구현이 hc2 를 의도한 사유로 떨어졌는데 hc1 만 통과 — 함정은 갖고 있으니 다시 만들지 않고
    hc1 숨은 테스트를 본다 (다시 만든 흔한 구현은 REQUIRES_NEW 를 같은 클래스에서 불러 함정을 잃었다)."""
    naive = _run(
        "naive-all",
        "ref_naive",
        _t("hidden", "hc1"),
        _t("hidden", "hc2", "ASSERT", "rollback_left_success", "성공 기록이 남음"),
    )
    plan = _assign(_report(naive, _run("d1", "ref_accepted", *GOOD_ACC)))
    assert _keys(plan) == ["gen_hidden"]
    assert "흔한 구현(naive-all)이 숨은 케이스 hc1 를 통과" in plan.actions[0].note


def test_naive_regenerated_for_another_failure_keeps_its_structure() -> None:
    """다른 사유(공개 테스트 실패)로 흔한 구현을 다시 만들 때 설명의 구조를 '더 안전하게' 바꾸지 말라고 붙인다 — ③ 수리 요청엔 이미 있다."""
    naive = _run("naive-all", "ref_naive", _t("public", "Pt1Test", "ASSERT", None, "기록 없음"))
    d1 = _run("d1", "ref_accepted", *GOOD_ACC, _t("public", "Pt1Test"))
    plan = _assign(_report(naive, d1))
    [a] = [a for a in plan.actions if a.generator == "gen_naive"]
    assert a.gate == "⑤" and "지적된 실패만 고친다" in a.note and a.pool.suffix == ""


def test_starter_that_already_passes_a_hidden_case_is_regenerated() -> None:
    """2026-11-07 알파 테스트: 시작 코드가 새 기능까지 구현해 시작 코드 그대로 숨은 케이스를 통과 — 2회차는 모두, 4회차(배송 감사)는 hc1 만."""
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
    # 하나만 통과해도 시작 코드를 다시 만든다 (2026-11-07 배송 감사 4회차 — '모두 통과' 기준이 놓침)
    partly = _run(
        "starter+hidden",
        "starter_hidden",
        _t("hidden", "hc1"),
        _t("hidden", "hc2", "ASSERT", "record_missing", "x"),
    )
    plan = _assign(_report(partly, *refs))
    assert _keys(plan) == ["gen_starter"] and "숨은 케이스 hc1 통과" in plan.actions[0].reason
    # 이미 다시 만든 시작 코드가 또 통과하면 막지 않고 표시만 — 나머지 게이트는 그대로 본다
    again = _assign(_report(partly, *refs), gen_starter=1)
    assert again.passed and any("다시 만든 뒤에도" in f and "hc1" in f for f in again.flags)
    # 숨은 테스트가 컴파일되지 않으면 여기서 보지 않는다 (참조 구현 쪽에서 배정)
    broken = _run("starter+hidden", "starter_hidden", setup="compile_error")
    assert _assign(_report(broken, *refs)).passed


def test_compile_repair_asks_to_fix_the_same_kind_in_every_file() -> None:
    """2026-11-07 알파 4회차: 수리마다 지적된 파일만 고쳐 다음 빌드에서 다른 파일의 같은 종류 오류가 이어서 드러남."""
    err = {
        "path": f"{CASES}/Hc1Test.java",
        "line": 41,
        "message": "unreported exception IOException",
    }
    naive = _run("naive-all", "ref_naive", setup="compile_error", compile_errors=[err])
    plan = _assign(_report(naive, _run("d1", "ref_accepted", *GOOD_ACC)))
    hidden = next(a for a in plan.actions if a.generator == "gen_hidden")
    assert hidden.compile and "모든 파일" in hidden.note and "IOException" in hidden.note


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


def test_regenerated_design_alone_stuck_in_a_hidden_case_suspects_the_test_once() -> None:
    """2026-11-11 fe8 오프라인 hc2: 숨은 테스트가 첫 요청을 붙잡은 채 두 번째를 기다려, 하나씩 보내는 허용 설계만 시간 초과(HARNESS_ERR)로
    떨어질 수 있었다 — 게이트 ④ 는 단언 실패만 봐서 그 구현을 계속 다시 만들었다. 다시 만든 뒤에도 혼자면 테스트를 한 번 의심한다."""
    stuck = _t(
        "hidden",
        "hc2",
        "HARNESS_ERR",
        None,
        "Error: 요청이 오지 않음: POST /api/memos — 응답하지 않은 요청 1개가 붙잡혀 있는 동안 기다렸다",
    )
    naive = _run("naive-all", "ref_naive", *GOOD_NAIVE)
    d1 = _run("d1", "ref_accepted", GOOD_ACC[0], stuck)
    # 처음엔 그 구현 탓
    plan = _assign(_report(naive, d1))
    assert _keys(plan) == ["gen_accepted:d1"] and plan.actions[0].gate == "setup"
    # 다시 만든 뒤에도 그 구현만 → 숨은 테스트를 한 번 의심 (설계 모순 예산)
    plan = _assign(_report(naive, d1), gen_accepted__d1=1)
    assert _keys(plan) == ["gen_hidden"] and plan.actions[0].pool is DESIGN
    assert "다른 구현(naive-all)은 판정까지 갔다" in plan.actions[0].note
    assert "구현마다 다를 수 있는 진행을 기다리지 말고" in plan.actions[0].note  # 스택 중립 (#182)
    # 그 한 번을 썼으면 예전처럼 그 구현 (예산은 재생성 쪽이 센다)
    counts = {"gen_accepted:d1": 1, f"gen_hidden{DESIGN.suffix}": 1}
    plan = assign(_report(naive, d1), PLAN2, OVERLAYS, MISSED, counts)
    assert [a.key for a in plan.actions] == ["gen_accepted:d1"]
    # 테스트를 의심하는 회차에는 같은 케이스에서 함께 깨진 흔한 구현을 다시 만들지 않는다 (고친 테스트로 다음 회차에 비교)
    both = _run("naive-all", "ref_naive", GOOD_NAIVE[0], stuck)
    d2 = _run("d2", "ref_accepted", *GOOD_ACC)
    plan = _assign(_report(both, d1, d2), gen_accepted__d1=1)
    assert _keys(plan) == ["gen_hidden"] and "다른 구현(d2)은" in plan.actions[0].note
    # 흔한 구현만 혼자 떨어지면 예전 그대로 그 구현
    n_stuck = _run("naive-all", "ref_naive", GOOD_NAIVE[0], stuck)
    plan = _assign(_report(n_stuck, _run("d1", "ref_accepted", *GOOD_ACC)), gen_accepted__d1=1)
    assert [a.generator for a in plan.actions] == ["gen_naive"]


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
    # 허용 구현이 여럿인데 다시 만든 하나가 또 혼자 실패(다른 허용 구현은 통과) → 숨은 테스트가 한 설계만 단언했는지 한 번 의심
    # (2026-11-10 프론트 알파: 흔한 구현은 의도한 사유로 잡혀도 다른 허용 설계를 막는 케이스가 있었다)
    d2_ok = _run("d2", "ref_accepted", *GOOD_ACC)
    plan = _assign(_report(naive, d1, d2_ok), gen_accepted__d1=1)
    assert _keys(plan) == ["gen_hidden"] and plan.actions[0].pool is DESIGN
    assert "다른 허용 설계 구현(d2)은 통과" in plan.actions[0].note
    # 그 한 번을 썼으면 → 사람 확인 (설계를 못 따름 — 흔한 구현을 의도한 사유로 잡으므로)
    counts = {"gen_accepted:d1": 1, f"gen_hidden{DESIGN.suffix}": 1}
    plan = assign(_report(naive, d1, d2_ok), PLAN2, OVERLAYS, MISSED, counts)
    assert not plan.actions and any("설계를 못 따름" in u for u in plan.unresolved)
    # 일반 숨은 테스트 예산(③ 등)을 이미 써도 이 의심은 따로 센다
    plan = _assign(_report(naive, d1, d2_ok), gen_accepted__d1=1, gen_hidden=1)
    assert _keys(plan) == ["gen_hidden"]


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
        "gen_public": "**공개 테스트**(기능 테스트)",
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
        self,
        starter: Overlay,
        hidden: Overlay,
        refs: Sequence[Overlay],
        public: Overlay | None = None,
    ) -> VerifyReport:
        self.seen.append([o.label for o in refs])
        self.public = public
        return self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]


SVC = _files((f"{PKG}/order/OrderService.java", SERVICE))
PUBLIC_TEST = """package com.example.service.publictests;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

@DisplayName("pt1 변경 성공 시 이력 1건 · pt2 조회가 최신순")
class OrderHistoryPublicTest extends PublicCase {
    @Test
    void pt1() throws Exception {
    }
}
"""
PUBLIC = _files(
    ("src/test/java/com/example/service/publictests/OrderHistoryPublicTest.java", PUBLIC_TEST)
)
HIDDEN = _files((f"{CASES}/OrderStatusBurstTest.java", HC1))


def _llm(**over: list[Completion]) -> tuple[LLMClient, RoutedBackend]:
    routes = {
        "gen_public": [PUBLIC],
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
    verifier = ScriptedVerifier(_one_case(NAIVE_OK, D1_OK))
    out = await _build(verifier, llm)
    assert out.status == "review" and out.rounds == 0 and not out.repairs
    assert sorted(backend.calls) == [
        "gen_accepted",
        "gen_hidden",
        "gen_naive",
        "gen_public",
        "gen_starter",
    ]
    assert set(out.overlays) == {"starter", "public", "hidden", "naive-all", "d1"}
    # 테스트 먼저(TDD): 공개 테스트를 시작 코드보다 먼저 만들고, 검증에 넣는다
    assert backend.calls.index("gen_public") < backend.calls.index("gen_starter")
    assert verifier.public is not None and any(
        f.path.endswith("PersistenceCheckTest.java") for f in verifier.public.files
    )
    starter_prompt = next(u for n, u in backend.users if n == "gen_starter")
    assert (
        "OrderHistoryPublicTest" in starter_prompt
    )  # CURRENT gen_starter v8(테스트 먼저, v7 지시문)은 공개 테스트를 보고 쓴다 — 2026-11-10 CURRENT 승격


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


POOL = _files(
    (f"{PKG}/order/OrderService.java", SERVICE),
    (
        "src/main/resources/application.yml",
        "spring:\n  datasource:\n    hikari:\n      maximum-pool-size: 200\n",
    ),
)


async def test_design_relying_on_harness_config_is_dropped() -> None:
    """알파 테스트 2026-11-07: '풀 크기 산정' 설계는 하네스가 pool_size 를 덮어써 채점에서 무효 — 빼고 남은 설계로."""
    llm, backend = _llm(gen_accepted=[SVC, POOL])  # d1 은 코드, d2 는 재요청에도 풀 설정
    verifier = ScriptedVerifier(_one_case(NAIVE_OK, D1_OK))
    out = await _build(verifier, llm, designs=(D1, D2))
    assert out.status == "review" and verifier.seen == [["naive-all", "d1"]]
    assert "d2" not in out.overlays and backend.calls.count("gen_accepted") == 3
    assert any("허용 설계 d2 제외" in f for f in out.review_flags)
    # 허용 설계가 그것 하나뿐이면 빼지 않고 생성 실패로 멈춘다
    llm, _ = _llm(gen_accepted=[POOL])
    out = await _build(ScriptedVerifier(_one_case(NAIVE_OK, D1_OK)), llm)
    assert out.status == "failed" and any("gen_accepted 생성 실패" in f for f in out.review_flags)


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


def test_boot_error_note_carries_the_root_cause() -> None:
    """2026-11-08 알파 4케이스: 'boot_error' 만 가서 시작 코드가 같은 H2 문법 오류를 되풀이했다."""
    detail = 'org.h2.jdbc.JdbcSQLSyntaxErrorException: Syntax error in SQL statement "CREATE TABLE t (id BIGINT PRIMARY KEY [*]GENERATED …"'
    starter = _run("starter", "starter", setup="boot_error", setup_detail=detail)
    plan = assign(VerifyReport(runs=[starter]), PLAN2, OVERLAYS, MISSED)
    a = plan.actions[0]
    assert (
        a.generator == "gen_starter"
        and "근본 원인: org.h2.jdbc.JdbcSQLSyntaxErrorException" in a.note
    )
    assert "[*]GENERATED" in a.reason
    ref = _run("d1", "ref_accepted", setup="boot_error", setup_detail=detail)
    acts = _assign(_report(_run("naive-all", "ref_naive", *GOOD_NAIVE), ref)).actions
    assert any(x.key == "gen_accepted:d1" and "근본 원인" in x.note for x in acts)


def test_shared_file_rewritten_by_every_impl_is_named_as_a_cause() -> None:
    """2026-11-08 알파: 참조 구현이 모두 application.yml 을 다시 써 같은 404 를 냈는데 '계약 모호'·'테스트 의심'으로만 짚었다
    → 모두 다시 쓴 공유 파일(템플릿·시작 코드에 이미 있는 파일)을 원인 후보로 싣는다."""
    yml = "src/main/resources/application.yml"
    overlays = {
        k: (
            o
            if o.kind not in ("ref_naive", "ref_accepted")
            else Overlay(
                kind=o.kind,
                label=o.label,
                files=(*o.files, GeneratedFile(path=yml, content="spring: {}\n")),
            )
        )
        for k, o in OVERLAYS.items()
    }
    report = _report(
        _breaks("naive-all", "ref_naive", NOT_NULL), _breaks("d1", "ref_accepted", NOT_NULL)
    )
    plan = assign(report, PLAN2, overlays, MISSED, {}, shared_paths={yml})
    assert "공유 파일 src/main/resources/application.yml" in plan.unresolved[0]
    assert "공유 파일" not in assign(report, PLAN2, overlays, MISSED, {}).unresolved[0]
    # 같은 기동 실패도
    boot = 'org.h2.jdbc.JdbcSQLSyntaxErrorException: Table "T" already exists'
    report = _report(
        _run("naive-all", "ref_naive", setup="boot_error", setup_detail=boot),
        _run("d1", "ref_accepted", setup="boot_error", setup_detail=boot),
    )
    acts = assign(report, PLAN2, overlays, MISSED, {}, shared_paths={yml}).actions
    assert len(acts) == 2 and all("공유 파일" in a.reason for a in acts)


def test_hidden_case_passed_by_a_starter_without_the_feature_regenerates_the_hidden_test() -> None:
    """2026-11-08 알파 (2df170e2): 숨은 테스트가 계약에 없는 요청자로 요청해 모두 403 — hc2 는 '성공 기록 0건'만 단언해
    기능 없는 시작 코드(공개 테스트 실패)도 통과했는데, 배정은 시작 코드를 다시 만들었다 → 숨은 테스트를 다시 만든다."""
    refs = (_run("naive-all", "ref_naive", *GOOD_NAIVE), _run("d1", "ref_accepted", *GOOD_ACC))
    no_feature = TargetRun.model_validate(
        {
            "target": "starter",
            "kind": "starter",
            "tests": [
                _t("existing", "OrderControllerTest"),
                _t("public", "PersistenceCheckTest"),
                _t("public", "Pt1Test", "ASSERT", msg="expected 200 but was 501"),
            ],
        }
    )
    solved = _run("starter+hidden", "starter_hidden", *GOOD_ACC)
    report = VerifyReport(runs=[no_feature, solved, *refs])
    plan = assign(report, PLAN2, OVERLAYS, MISSED)
    assert _keys(plan) == ["gen_hidden"] and plan.actions[0].gate == "②'"
    assert "Pt1Test" in plan.actions[0].note and "기능 단계" in plan.actions[0].note
    # 숨은 테스트를 이미 다시 만들었으면 막지 않고 표시만
    again = assign(report, PLAN2, OVERLAYS, MISSED, {"gen_hidden": 1})
    assert not any(a.generator == "gen_hidden" and a.gate == "②'" for a in again.actions)
    assert any("숨은 테스트를 다시 만든 뒤에도" in f for f in again.flags)


def test_python_job_crash_blames_that_implementation_not_the_public_test() -> None:
    """2026-11-09 데이터 알파 3회차: 허용 구현의 SQL 오류·흔한 구현의 값 불일치를 '테스트 의심'으로 짚었다."""
    from task_spec.schemas import TargetRun
    from task_spec.schemas.template import RepairTerms
    from task_spec.starter.repair import _public_refs

    def run(target: str, kind: str, result: str, message: str) -> TargetRun:
        return TargetRun.model_validate(
            {
                "target": target,
                "kind": kind,
                "setup": "ok",
                "tests": [{"group": "public", "name": "pt1", "result": result, "message": message}],
            }
        )

    crash = 'assert 1 == 0 + where 1 = JobResult(code=1, stderr=\'{"message": "Binder Error: No function matches substr(TIMESTAMP)"}\').code'
    runs = [
        run("d1", "ref_accepted", "ASSERT", crash),
        run("naive-all", "ref_naive", "ASSERT", "assert 3000 == 5000"),
    ]
    terms = RepairTerms(impl_crash=r"\b[A-Z][A-Za-z]*(?: [A-Z][a-z]+)? ?(?:Error|Exception):")
    acts, trusted = _public_refs(runs, {}, "", terms)
    assert [(a.generator, a.label) for a in acts] == [("gen_accepted", "d1")]
    assert "실행 오류" in acts[0].reason and trusted == set()
    assert (
        acts[0].budget_key == "gen_accepted:d1#crash"
    )  # 일반 재생성 예산과 따로 센다 (알파 6회차)
    # 패턴이 없는 스택(Java·Node)은 그대로 — 모든 구현 실패는 테스트 의심
    acts, _ = _public_refs(runs, {}, "", RepairTerms())
    assert [a.generator for a in acts] == ["gen_public"]
    # 이미 다시 만든 구현이 또 실행 오류 — 구현 탓을 반복하지 않고 기존 규칙(테스트 의심)으로 (알파 5회차)
    acts, _ = _public_refs(runs, {"gen_accepted:d1": 1}, "", terms)  # 일반 예산을 쓴 것은 상관없음
    assert [(a.generator, a.label) for a in acts] == [("gen_accepted", "d1")]
    acts, _ = _public_refs(runs, {"gen_accepted:d1#crash": 1}, "", terms)
    assert [a.generator for a in acts] == ["gen_public"]
    # 모든 구현이 같은 실행 오류 — 공유 원인(입력·계약)일 수 있어 기존 규칙대로
    same = [
        run("d1", "ref_accepted", "ASSERT", crash),
        run("naive-all", "ref_naive", "ASSERT", crash),
    ]
    acts, _ = _public_refs(same, {}, "", terms)
    assert [a.generator for a in acts] == ["gen_public"]


def test_grading_environment_error_in_every_implementation_is_escalated_not_regenerated() -> None:
    """2026-11-10 프론트 알파 0108be12: jsdom 에 IndexedDB 가 없어 모든 구현이 `indexedDB is not defined` 였는데
    '테스트 의심'으로 공개 테스트를 두 번 다시 만들었다 — 무엇을 다시 만들어도 같으므로 '채점 환경 확인 필요'로 올린다."""
    from task_spec.starter.templates import load_template

    terms = load_template("frontend/react-vite-ts").manifest.repair_terms
    env = "ReferenceError: indexedDB is not defined"

    def run(target: str, kind: str, hc2: str) -> TargetRun:
        return TargetRun.model_validate(
            {
                "target": target,
                "kind": kind,
                "setup": "ok",
                "tests": [
                    {
                        "group": "public",
                        "name": "pt1",
                        "result": "HARNESS_ERR",
                        "message": "Unable to find an element",
                    },
                    {"group": "hidden", "name": "hc2", "result": "HARNESS_ERR", "message": hc2},
                ],
            }
        )

    starter = TargetRun.model_validate(
        {"target": "starter", "kind": "starter", "setup": "ok", "tests": []}
    )
    every = VerifyReport(
        runs=[
            starter,
            run("naive-all", "ref_naive", env),
            run("d1", "ref_accepted", f"{env}\n at x"),
        ]
    )
    plan = assign(every, PLAN2, OVERLAYS, MISSED, {}, (), terms)
    assert plan.actions == [] and not plan.passed
    assert len(plan.unresolved) == 1 and plan.unresolved[0].startswith("채점 환경 확인 필요")
    assert "hidden hc2" in plan.unresolved[0] and "indexedDB" in plan.unresolved[0]
    # 한 구현만 그 오류 — 그 구현의 선택일 수 있어 기존 규칙대로 (다시 만들기)
    one = VerifyReport(
        runs=[
            starter,
            run("naive-all", "ref_naive", env),
            run("d1", "ref_accepted", "AssertionError: x"),
        ]
    )
    plan = assign(one, PLAN2, OVERLAYS, MISSED, {}, (), terms)
    assert not any(u.startswith("채점 환경") for u in plan.unresolved)
    # 패턴이 없는 템플릿(Java 등)은 그대로
    plan = assign(every, PLAN2, OVERLAYS, MISSED, {}, ())
    assert not any(u.startswith("채점 환경") for u in plan.unresolved)


def test_case_that_still_misses_the_naive_after_a_rewrite_is_flagged_not_failed() -> None:
    """2026-11-10 gen-item2: hc2 가 의도한 사유로 함정을 잡는데 hc1 만 다시 만든 뒤에도 흔한 구현이 통과 — 예산을 다 써 failed 였다.
    허용 구현은 모두 통과했으니 막지 않고 '변별하지 못하는 케이스'로 채점에서 뺀다 (2026-11-11 사용자 — 예전엔 표시만)."""
    naive = _run(
        "naive-all",
        "ref_naive",
        _t("hidden", "hc1"),
        _t("hidden", "hc2", "ASSERT", "rollback_left_success", "성공 기록이 남음"),
    )
    report = _report(naive, _run("d1", "ref_accepted", *GOOD_ACC))
    assert _keys(_assign(report)) == ["gen_hidden"]  # 처음엔 숨은 테스트를 다시
    again = _assign(report, gen_hidden=1)
    assert again.passed and not again.actions
    assert any("hc1" in f and "hc2" in f and "채점에서 뺐다" in f for f in again.flags)
    assert again.weak == [
        "hc1"
    ]  # 채점에서 뺀다 [확정: 2026-11-11 사용자] — hc2 가 함정을 잡으니 숨은 요구는 남는다
    assert _assign(report).weak == []  # 다시 만들기 전에는 빼지 않는다


def test_gate3_naive_with_fix_markers_is_rebuilt_then_judged_as_material() -> None:
    """흔한 구현이 숨은 케이스를 통과했고 허용 설계의 장치(재료 fix_markers)를 썼다 — 숨은 테스트가 아니라 흔한 구현을 그 장치를 짚어
    다시 만들고, 다시 만든 뒤에도 그렇면 재료 판정(막는 표시)으로 사람에게 (2026-11-10 응답 순서 cf: React 정리 패턴 let cancelled)."""
    naive = _run("naive-all", "ref_naive", *GOOD_ACC)  # 숨은 케이스를 모두 통과
    code = Overlay(
        kind="ref_naive",
        label="naive-all",
        files=(GeneratedFile(path="src/pages/Search.tsx", content="let cancelled = false;"),),
    )
    overlays = {**OVERLAYS, "naive-all": code}
    markers = [r"\blet\s+cancelled\s*="]
    report = _report(naive, _run("d1", "ref_accepted", *GOOD_ACC))
    plan = assign(report, PLAN2, overlays, MISSED, {}, fix_markers=markers)
    assert [(a.key, a.gate) for a in plan.actions] == [("gen_naive:naive-all", "③")]
    assert "let cancelled =" in plan.actions[0].note and not plan.unresolved
    again = assign(
        report, PLAN2, overlays, MISSED, {"gen_naive:naive-all#naive-first": 1}, fix_markers=markers
    )
    assert not again.actions and any(u.startswith("재료 판정: ") for u in again.unresolved)
    # 표지가 없으면 지금 규칙 그대로 (흔한 구현을 먼저 다시)
    plain = assign(report, PLAN2, OVERLAYS, MISSED, {}, fix_markers=markers)
    assert [a.key for a in plain.actions] == [
        "gen_naive:naive-all"
    ] and "정답 장치" not in plain.actions[0].reason


def test_gate5_rebuilt_accepted_alone_failing_public_suspects_the_test_once() -> None:
    """2026-11-10 응답 순서 cf4: 공개 테스트가 지난 요청에 응답해, 지난 응답을 무시하는 허용 구현 d2 만 계속 떨어졌다."""

    pub = _t("public", "pt1", "HARNESS_ERR", None, "Unable to find recommendation-list")
    report = _report(
        _run("naive-all", "ref_naive", *GOOD_NAIVE, _t("public", "pt1")),
        _run("d1", "ref_accepted", *GOOD_ACC, _t("public", "pt1")),
        _run("d2", "ref_accepted", *GOOD_ACC, pub),
    )
    first = assign(report, PLAN2, OVERLAYS, MISSED, {})
    assert [a.key for a in first.actions] == ["gen_accepted:d2"]  # 처음엔 그 구현
    again = assign(report, PLAN2, OVERLAYS, MISSED, {"gen_accepted:d2": 1})
    assert [(a.key, a.pool) for a in again.actions] == [("gen_public", DESIGN)]
    assert (
        "naive-all, d1" in again.actions[0].note and "골라 확인" in again.actions[0].note
    )  # 스택 중립 (#182)
    spent = assign(
        report, PLAN2, OVERLAYS, MISSED, {"gen_accepted:d2": 1, f"gen_public{DESIGN.suffix}": 1}
    )
    assert [a.key for a in spent.actions] == [
        "gen_accepted:d2"
    ]  # 한 번 의심했으면 원래대로 (예산이 막는다)


def test_harness_diagnosed_test_defect_goes_straight_to_the_test() -> None:
    """React 템플릿 v7: match 없는 기다림에 붙잡힌 요청이 여럿이면 하네스가 테스트 결함이라고 짚는다 — 어느 구현에서 났든 첫 회차에 그 테스트를."""
    from task_spec.schemas.template import RepairTerms

    terms = RepairTerms(test_defect=r"붙잡힌 \w+ \S+ 요청이 \d+개 — match 로")
    msg = "Error: 붙잡힌 GET /api/x 요청이 2개 — match 로 확인할 요청을 고른다"
    report = _report(
        _run("naive-all", "ref_naive", *GOOD_NAIVE, _t("public", "pt1", "HARNESS_ERR", None, msg)),
        _run("d1", "ref_accepted", *GOOD_ACC, _t("public", "pt1")),
    )
    plan = assign(report, PLAN2, OVERLAYS, MISSED, {}, terms=terms)
    assert [(a.key, a.gate) for a in plan.actions] == [("gen_public", "⑤")]
    hidden = _report(
        _run("naive-all", "ref_naive", *GOOD_NAIVE),
        _run(
            "d1", "ref_accepted", _t("hidden", "hc1", "HARNESS_ERR", None, msg), _t("hidden", "hc2")
        ),
        _run("d2", "ref_accepted", *GOOD_ACC),
    )
    plan = assign(hidden, PLAN2, OVERLAYS, MISSED, {}, terms=terms)
    assert [(a.key, a.gate) for a in plan.actions] == [("gen_hidden", "④")]
    # 패턴이 없는 템플릿은 지금 규칙 그대로 (그 구현)
    plan = assign(report, PLAN2, OVERLAYS, MISSED, {})
    assert [a.key for a in plan.actions] == ["gen_naive:naive-all"]


def test_gate3_accepts_another_reason_of_the_same_hidden_need_and_flags_the_gap() -> None:
    """2026-11-11 재측정 오프라인: 흔한 구현이 중복(duplicated) 대신 손실(lost)로 떨어졌다 — 재료가 정한 같은 숨은 요구의 사유면 인정,
    다만 의도한 사유를 확인하는 케이스가 없으면 승인자 표시."""
    hc2 = _t("hidden", "hc2", "ASSERT", "record_missing", "실패 기록 없음")
    report = _report(
        _run("naive-all", "ref_naive", GOOD_NAIVE[0], hc2), _run("d1", "ref_accepted", *GOOD_ACC)
    )
    family = ("pool_acquire_timeout", "rollback_left_success", "record_missing")
    plan = assign(report, PLAN2, OVERLAYS, MISSED, {}, feature_reasons=family)
    assert plan.actions == []
    assert any(
        "hc2" in f and "rollback_left_success 대신 record_missing" in f and "사람 확인" in f
        for f in plan.flags
    ), plan.flags
    # 묶음 밖 사유는 지금처럼 숨은 테스트를 다시
    plan = assign(report, PLAN2, OVERLAYS, MISSED, {}, feature_reasons=("pool_acquire_timeout",))
    assert _keys(plan) == ["gen_hidden"]


def test_gate3_unreasoned_assert_names_the_line_and_the_two_ways() -> None:
    """사유 없는 단언에서 먼저 떨어지면 그 줄(at)과 갈래(함정 결과면 사유를, 준비 확인이면 흔한 구현도 지나가게)를 수정 요청에."""
    hc2 = _t("hidden", "hc2", "ASSERT", None, "expected 2 to be greater than or equal to 3")
    hc2["at"] = "src/__grading__/cases/hc2.test.tsx:66"
    report = _report(
        _run("naive-all", "ref_naive", GOOD_NAIVE[0], hc2), _run("d1", "ref_accepted", *GOOD_ACC)
    )
    family = ("rollback_left_success", "record_missing")
    plan = assign(report, PLAN2, OVERLAYS, MISSED, {}, feature_reasons=family)
    note = plan.actions[0].note
    assert "(src/__grading__/cases/hc2.test.tsx:66)" in note
    assert "이 요구의 사유: record_missing, rollback_left_success" in note
    assert "준비 확인이면 흔한 구현도 지나가게" in note


def test_budget_pools_are_one_table_with_the_old_limits() -> None:
    """#183: 사례마다 접미사를 하나씩 더하던 예산을 표 하나로 — 상한은 정리 전과 같다 (일반 1·컴파일 2·나머지 1)."""
    from task_spec.starter.repair import (
        COMPILE,
        GENERAL,
        NAIVE_FIRST,
        POOLS,
        budget_key,
        exhausted,
    )

    assert len({p.suffix for p in POOLS}) == len(
        POOLS
    )  # 키가 겹치지 않는다 (예전 ③·⑪ 이 둘 다 '#trap')
    assert {p.suffix: p.limit for p in POOLS if p.limit != 1} == {COMPILE.suffix: 2}
    assert budget_key("gen_naive", "naive-all", NAIVE_FIRST) == "gen_naive:naive-all#naive-first"
    assert budget_key("gen_hidden") == "gen_hidden" and GENERAL.suffix == ""
    counts = {"gen_hidden#compile": 1}
    assert not exhausted(counts, "gen_hidden", pool=COMPILE) and not exhausted(counts, "gen_hidden")
    assert exhausted({"gen_hidden#compile": 2}, "gen_hidden", pool=COMPILE)


def test_merged_gates_are_split_before_comparing() -> None:
    """#183: KEEP_GATES 가 글자 집합이라 우연히 맞던 비교 — 합친 수리의 이어 붙은 게이트("③③'")는 나눠 비교한다."""
    from task_spec.nodes.starter.build import KEEP_GATES
    from task_spec.starter.repair import gates_of

    assert gates_of("③③'") == ["③", "③'"] and set(gates_of("③③'")) <= KEEP_GATES
    assert not set(gates_of("③④")) <= KEEP_GATES and not set(gates_of("setup")) <= KEEP_GATES


def test_regenerated_design_that_regressed_is_rebuilt_not_the_test() -> None:
    """#183 (2026-11-14 naver_2026#002): 공개 테스트(⑤)로 다시 만든 허용 구현이 이전 판이 통과하던 숨은 케이스에서 떨어졌는데
    '다시 만든 d1 만 실패 — 설계와 모순 의심'으로 멀쩡한 숨은 테스트를 다시 만들었다. 이전 판이 같은 테스트로 통과했으면 퇴행이다."""
    from task_spec.starter.repair import REGRESS, RegenNote

    naive = _run("naive-all", "ref_naive", *GOOD_NAIVE)
    broke = _t("hidden", "hc1", "ASSERT", "pool_acquire_timeout", "동시 요청 중 5xx 3")
    d1 = _run("d1", "ref_accepted", broke, GOOD_ACC[1])
    d2 = _run("d2", "ref_accepted", *GOOD_ACC)
    counts = {"gen_accepted:d1": 1}
    # 이전 판 근거가 없으면 예전처럼 테스트를 한 번 의심
    plan = assign(_report(naive, d1, d2), PLAN2, OVERLAYS, MISSED, counts)
    assert _keys(plan) == ["gen_hidden"] and plan.actions[0].pool is DESIGN
    # 이전 판이 같은 숨은 테스트로 hc1 을 통과했다 → 구현을 다시 (원래 고치려던 사유와 함께)
    log = {"gen_accepted:d1": RegenNote("⑤", (), "d1 가 공개 테스트 Pt3 실패")}
    plan = assign(
        _report(naive, d1, d2),
        PLAN2,
        OVERLAYS,
        MISSED,
        counts,
        regen_log=log,
        passed_before={"d1": {"hc1", "hc2"}},
    )
    assert _keys(plan) == ["gen_accepted:d1"]
    a = plan.actions[0]
    assert a.pool is REGRESS and a.cases == ("hc1",)
    assert "이전 판이 통과하던" in a.note and "공개 테스트 Pt3" in a.note
    # 퇴행 풀도 썼으면 테스트를 의심하지 않고 사람 확인 (숨은 테스트는 이전 판이 통과했다)
    plan = assign(
        _report(naive, d1, d2),
        PLAN2,
        OVERLAYS,
        MISSED,
        {**counts, f"gen_accepted:d1{REGRESS.suffix}": 1},
        regen_log=log,
        passed_before={"d1": {"hc1"}},
    )
    assert not plan.actions and any("구현 생성의 한계" in u for u in plan.unresolved)


def test_build_collects_the_cases_any_version_of_a_design_passed() -> None:
    """퇴행 판정의 근거 — 참조 구현마다 어느 판이든 지금 숨은 테스트로 통과한 케이스 (숨은 테스트·시작 코드를 다시 만들면
    build._regenerate 가 비운다)."""
    from task_spec.nodes.starter.build import BuildOutcome, _note_passed

    out = BuildOutcome(status="failed")
    naive = _run("naive-all", "ref_naive", *GOOD_NAIVE)
    _note_passed(out, _report(naive, _run("d1", "ref_accepted", *GOOD_ACC)))
    assert out.passed_before == {"naive-all": set(), "d1": {"hc1", "hc2"}}
    _note_passed(out, _report(_run("d1", "ref_accepted", GOOD_NAIVE[0], GOOD_ACC[1])))
    assert out.passed_before["d1"] == {
        "hc1",
        "hc2",
    }  # 어느 판이든 통과한 케이스 — 지금 판이 떨어져도 남는다


def test_repair_notes_carry_no_other_stack_or_case_shapes() -> None:
    """#182: 수리 요청 문구의 분기 조건은 일반 규칙인데 문구에 한 사례·한 스택의 모양(React 하네스 API·Java 컴파일 예시·트랜잭션
    구조·사례 날짜)이 들어 다른 스택의 수리에도 나갔다 — 코드의 문자열에는 스택 중립 원칙만 둔다 (사례 인용은 주석으로만).
    매니페스트가 처방을 고르는 데이터·React 문구(질의 문법·MemoryRouter)는 매니페스트 repair_terms 로 옮길 때(#176) 함께 뺀다."""
    import ast
    from pathlib import Path

    import task_spec.starter.repair as repair_mod

    tree = ast.parse(Path(repair_mod.__file__).read_text("utf-8"))
    texts = [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    forbidden = (
        "waitFor",
        "TestRestTemplate",
        "람다 안에서",
        "검사 예외",
        "무엇을 어디서 열고",
        "응답하지 않은 요청을 붙잡은 채",
        "정리 함수의 무시 표시",
        "목록 길이·한 번에 보내는 건수",
        "알파 5회차",
    )
    hits = [(f, t[:60]) for t in texts for f in forbidden if f in t]
    assert hits == []
