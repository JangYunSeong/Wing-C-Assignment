"""builder 실제 단계의 실패 사유 — 명세의 숨은 요구·테스트 문구를 사유에 넣지 않는다. LLM·podman 없이."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from task_spec.builder.steps import (
    FAILED_OVERLAYS,
    _counted,
    _kind,
    contract_failure,
    keep_overlays,
)
from task_spec.nodes.starter.plan_contract import PlanResult
from task_spec.schemas import GeneratedFile
from task_spec.starter.static_check import Overlay


def test_reason_keeps_only_the_kind_of_problem() -> None:
    """2026-11-12 스모크: 계약 실패 사유에 숨은 요구 문장('날짜별 입력 폴더: …')이 그대로 들어갔다."""
    problems = [
        "숨은 요구에 필요한 것이 계약에 없음 — 가상 요구: 가상 숨은 요구 본문 SECRET",
        "시드 행 수가 부족: 가상 엔티티 SECRET",
        "숨은 요구에 필요한 것이 계약에 없음 — 다른 가상 요구",
    ]
    got = _counted([_kind(p) for p in problems])
    assert got == "숨은 요구에 필요한 것이 계약에 없음 ×2 | 시드 행 수가 부족"
    assert "SECRET" not in got


def test_harness_problem_kind_stops_before_value_and_quote() -> None:
    """2026-11-15: HiddenPlan 조절값 문장은 머리에 값과 자료 인용이 온다 — 조절값 이름까지만."""
    item = "HiddenPlan request_timeout_s=1 이 근거 인용 '가상 자료 문장 SECRET: 500ms' 에서 재료 규칙으로 만든 값 4 과 다름"
    assert _kind(item) == "HiddenPlan request_timeout_s"
    assert (
        _kind("명세 자료에 운영 값 query_delay_ms(15, 16)이 있으니 넣는다")
        == "명세 자료에 운영 값 query_delay_ms"
    )


def test_contract_failure_lists_call_errors_gaps_and_problems_together() -> None:
    """2026-11-15 운영: 세 번 중 한 번 빈 응답이면 첫 비어 있지 않은 목록(fail_reasons)만 실려 검사 문제가 가려졌다."""
    res = PlanResult(
        fail_reasons=[
            "EmptyResponse(finish_reason=stop): plan_contract@31: 빈 응답 (finish_reason=stop, 재시도 없음)"
        ],
        problems=[
            "HiddenPlan request_timeout_s=1 이 근거 값 글자 '500ms' 를 규칙대로 바꾼 값 2 와 다름",
            "HiddenPlan query_delay_ms 의 근거 값 글자 'SECRET' 이 근거 인용 안에 없음",
        ],
    )
    got = contract_failure(res)
    assert got == (
        "호출 실패 EmptyResponse(finish_reason=stop) · "
        "검사 문제 HiddenPlan request_timeout_s | HiddenPlan query_delay_ms 의 근거 값 글자"
    )
    assert "SECRET" not in got and "500ms" not in got
    gaps = PlanResult(
        spec_gaps=[
            "명세 1단계 자료·추가 요구 구체 답에 운영 수치 pool_size 이 없음 — 명세 수정 필요"
        ]
    )
    assert (
        contract_failure(gaps)
        == "명세 결함 명세 1단계 자료·추가 요구 구체 답에 운영 수치 pool_size 이 없음"
    )
    assert contract_failure(PlanResult()) == "사유 없음"


def test_counted_keeps_the_first_kinds_and_says_how_many_more() -> None:
    assert _counted(["a", "b", "a", "c", "d"], n=2) == "a ×2 | b 외 2종"


def test_failed_build_keeps_the_last_overlays_inside_the_work_dir(tmp_path: Path) -> None:
    """2026-11-18 ct-10eb910705dd-b2: 수리 루프가 실패한 빌드는 검증 로그만 남아 허용 구현·숨은 테스트를 볼 수 없었다 —
    마지막 생성물을 작업 폴더 overlays/<키>/ 에 (알파 실행 폴더와 같은 모양)."""
    overlays = {
        "hidden": Overlay(
            kind="hidden",
            files=(GeneratedFile(path="src/__hidden__/hc1.test.tsx", content="가상 hc1"),),
        ),
        "d1": Overlay(
            kind="ref_accepted",
            label="d1",
            files=(GeneratedFile(path="src/App.tsx", content="가상 d1"),),
        ),
    }
    root = tmp_path / "work" / FAILED_OVERLAYS
    assert keep_overlays(overlays, root) == ["hidden", "d1"]
    assert (root / "hidden/src/__hidden__/hc1.test.tsx").read_text("utf-8") == "가상 hc1"
    assert (root / "d1/src/App.tsx").read_text("utf-8") == "가상 d1"


async def test_build_flags_hidden_terms_in_the_candidate_readme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#232: 운영 빌드는 응시용 README 를 렌더만 하고 누설 검사를 하지 않아 승인 화면 README 경고 칸이 늘 비어 있었다 — 계약 설정의
    뜻에 든 숨은 낱말('커넥션 풀')이 README 설정 표로 나가도 표시가 없었다. 평가 러너의 static_check(…, readme=…)와 같은 문장으로."""
    import task_spec.builder.steps as steps_mod
    from task_spec.builder.runner import ContractResult, Job
    from task_spec.nodes.starter.build import HIDDEN
    from task_spec.schemas import HiddenPlan, TaskContract
    from task_spec.starter.contract import SpecView
    from task_spec.starter.render import README
    from task_spec.starter.repair import STARTER
    from task_spec.starter.static_check import static_check
    from task_spec.starter.store import TaskLabel
    from task_spec.starter.templates import load_template
    from tests.builder.test_runner import _claimed
    from tests.starter.test_contract import BACKEND_SEED, _contract, _plan
    from tests.starter.test_plan_contract import _task

    java = load_template("backend/java-spring-boot3-jpa", 2)
    yml = "shop:\n  order:\n    page-size: 50\n"
    starter = Overlay(
        kind="starter",
        files=(GeneratedFile(path="src/main/resources/application.yml", content=yml),),
    )
    hidden = Overlay(kind="hidden", files=(GeneratedFile(path="가상", content="가상"),))

    async def built(*_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(
            status="review",
            overlays={STARTER: starter, HIDDEN: hidden},
            hidden_plan=None,
            review_flags=["빌드 가상 표시"],
            repairs=[],
            cost_usd=0.0,
            rounds=1,
            provenance=[],  # build_and_repair 결과의 생성기 호출 기록 (#233 이 steps 에서 읽는다)
        )

    monkeypatch.setattr(steps_mod, "build_and_repair", built)
    monkeypatch.setattr(steps_mod, "verifier_for", lambda *_a, **_k: None)
    _, record = await _claimed()
    task = _task()
    label = TaskLabel(title="가상 과제", posting_id="가상_공고#001")
    job = Job(build=record, task=task, label=label, work=tmp_path)
    steps = steps_mod.RealSteps(None, None, tmp_path / "없음", tmp_path / "images")  # type: ignore[arg-type]
    plan = HiddenPlan.model_validate(_plan())

    async def flags(meaning: str) -> tuple[list[str], str]:
        c = _contract()
        c["settings"] = [
            {"key": "shop.order.page-size", "default": 50, "meaning": meaning, "refs": ["r1"]}
        ]
        cr = ContractResult(
            template=java,
            contract=TaskContract.model_validate(c),
            plan=plan,
            cost_usd=0.0,
            seed=BACKEND_SEED,
            avoid_terms=("우아한형제들",),
        )
        out = await steps.build(job, cr)
        return out.review_flags, out.readme

    got, readme = await flags("커넥션 풀 크기에 맞춘 목록 한 번에 돌려줄 최대 개수")
    leak = f"{README} 에 숨은 요구·운영 조건을 노출하는 말: 커넥션 풀, 풀 크기 — 승인한 명세 문장에서 유래함, 명세 수정 필요"
    assert got == ["빌드 가상 표시", leak]
    same = static_check(
        [],
        template=java,
        plan=plan,
        spec=SpecView.from_task(task),
        seed=BACKEND_SEED,
        readme=GeneratedFile(path=README, content=readme),
        avoid_terms=("우아한형제들",),
    )
    assert same.review_flags == [
        leak
    ]  # 승인 화면 README 경고 칸이 고르는 머리(README.md)·문장 그대로
    got, _ = await flags("목록 한 번에 돌려줄 최대 개수")
    assert got == ["빌드 가상 표시"]
