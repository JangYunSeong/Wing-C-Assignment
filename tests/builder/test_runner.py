"""builder 작업 실행기 (#129) — 가짜 단계 + 메모리 저장소로 상태 전이·비용 상한·실패 사유·lease 를 본다. 가상 데이터만, LLM·podman 없이."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from task_spec.builder.runner import (
    BuildResult,
    ContractResult,
    ImagesResult,
    Job,
    RunConfig,
    StepFailed,
    Worker,
    run_job,
    run_with_lease,
)
from task_spec.schemas import GeneratedFile
from task_spec.schemas.build import RepairRecord
from task_spec.schemas.build_record import BuildFailure, BuildRecord
from task_spec.schemas.package import VerifySummary
from task_spec.starter.static_check import Overlay
from task_spec.starter.store import (
    LEASE_EXPIRED,
    BuildRepo,
    CommonRef,
    InMemoryBuildStore,
    TaskLabel,
)
from task_spec.starter.templates import load_template
from tests.starter.test_build_store import H_OLD, _artifact
from tests.starter.test_data_template import _contract, _plan
from tests.starter.test_kit import _task

CID = "ct-0123456789ab"
SECRET = "가상 생성 코드 SECRET-CODE"


class FakeSteps:
    def __init__(self, tmp: Path, **kw: object) -> None:
        self.cost = {"contract": 0.01, "build": 0.04, **kw.pop("cost", {})}  # type: ignore[dict-item]
        self.fail_at = kw.pop("fail_at", None)
        self.error = kw.pop("error", None)
        self.block_build = kw.pop("block_build", False)
        self.calls: list[str] = []
        self.cleaned: list[list[str]] = []
        self.works: list[Path] = []

    def _maybe_fail(self, stage: str) -> None:
        self.calls.append(stage)
        if self.fail_at == stage:
            raise self.error or StepFailed(f"{stage}: 가상 실패")

    async def contract(self, job: Job) -> ContractResult:
        self.works.append(job.work)
        self._maybe_fail("contract")
        return ContractResult(
            template=load_template("data/python-duckdb", 1),
            contract=_contract(),
            plan=_plan(),
            cost_usd=self.cost["contract"],
            review_flags=("계약 단계 가상 표시",),
        )

    async def build(self, job: Job, c: ContractResult) -> BuildResult:
        self._maybe_fail("build")
        if self.block_build:
            await asyncio.Event().wait()  # lease 를 잃을 때까지
        return BuildResult(
            overlays={
                "starter": Overlay(
                    kind="starter", files=(GeneratedFile(path="pipeline/run.py", content=SECRET),)
                ),
                "hidden": Overlay(
                    kind="hidden",
                    files=(GeneratedFile(path="grading/cases/test_hc1.py", content="x"),),
                ),
            },
            plan=c.plan,
            readme="# 가상 README",
            review_flags=["빌드 단계 가상 표시"],
            repairs=[],
            cost_usd=self.cost["build"],
        )

    async def images(self, job: Job, c: ContractResult, b: BuildResult) -> ImagesResult:
        self._maybe_fail("images")
        s = VerifySummary(hidden_passed=0, hidden_total=1, public_passed=0, public_total=1)
        return ImagesResult(
            template_ref="data/python-duckdb@v1",
            tree_sha256=H_OLD,
            grader_sha256=H_OLD,
            verification={"starter-only": s},
            cleanup=["localhost/task/가상-candidate"],
        )

    async def register(
        self, store: BuildRepo, job: Job, c: ContractResult, b: BuildResult, i: ImagesResult
    ) -> None:
        self._maybe_fail("package")
        for role in ("candidate", "grader"):
            await store.put_artifact(_artifact(job.build.build_id, role, H_OLD))  # type: ignore[arg-type]

    async def cleanup(self, images: list[str]) -> None:
        self.cleaned.append(list(images))


async def _claimed(worker: str = "w1") -> tuple[InMemoryBuildStore, BuildRecord]:
    s = InMemoryBuildStore()
    s.commons[CID] = CommonRef("approved", "feature", "onto-test")
    s.tasks[CID] = _task(review_flags=["명세 가상 표시"])
    s.labels[CID] = TaskLabel(title="가상 과제", posting_id="가상_공고#001", revision=2)
    await s.request_build(CID, "가상승인자")
    b = await s.claim(worker, 60)
    assert b is not None
    return s, b


def _cfg(tmp: Path, budget: float = 0.5) -> RunConfig:
    return RunConfig(work_root=tmp / "work", budget_usd=budget, lease_seconds=60)


async def test_a_claimed_build_reaches_review_with_files_outcome_and_packages(
    tmp_path: Path,
) -> None:
    store, b = await _claimed()
    steps = FakeSteps(tmp_path)
    done = await run_job(store, steps, _cfg(tmp_path), b)
    assert done.status == "review" and done.stage == "package" and done.finished_at is not None
    assert done.cost_usd == pytest.approx(0.05)
    assert done.outcome is not None and done.outcome.review_flags == [
        "명세 가상 표시",
        "계약 단계 가상 표시",  # 운영 경로도 계약 단계 승인자 표시를 저장한다 (예전에는 버렸다)
        "빌드 단계 가상 표시",
    ]
    kinds = {f.kind for f in await store.files(b.build_id)}
    assert kinds == {"starter", "hidden", "readme"}
    assert [a.role for a in await store.artifacts(b.build_id)] == ["candidate", "grader"]
    assert steps.cleaned == [["localhost/task/가상-candidate"]]
    assert not steps.works[0].exists()  # 성공하면 작업 폴더(생성 코드 평문)를 지운다


async def test_a_step_failure_keeps_a_short_reason_and_the_work_dir(tmp_path: Path) -> None:
    store, b = await _claimed()
    steps = FakeSteps(tmp_path, fail_at="contract")
    done = await run_job(store, steps, _cfg(tmp_path), b)
    assert (done.status, done.error) == ("failed", "contract: 가상 실패")
    assert steps.calls == ["contract"] and steps.works[0].is_dir()  # 원인 확인용으로 남긴다


async def test_a_failed_build_keeps_its_contract_plan_flags_and_repairs(tmp_path: Path) -> None:
    """2026-11-15 프론트 빌드: 실패한 빌드는 계약을 저장하지 않아 하네스 경로 버그를 메시지 구조로만 짚었다 —
    끝까지 가지 못한 데까지의 계약·숨은 계획·표시·수리 기록을 DB 에 (사유 칸은 그대로 짧게)."""
    store, b = await _claimed()
    rec = RepairRecord(
        gate="⑤", target="gen_public", reason="모든 구현이 pt1 실패 — 테스트 의심", result="skipped"
    )
    part = BuildFailure(hidden_plan=_plan(), review_flags=["빌드 단계 가상 표시"], repairs=[rec])
    steps = FakeSteps(
        tmp_path, fail_at="build", error=StepFailed("build: 수리 루프 실패 (회차 1)", detail=part)
    )
    done = await run_job(store, steps, _cfg(tmp_path), b)
    assert (done.status, done.error) == ("failed", "build: 수리 루프 실패 (회차 1)")
    assert done.outcome is None and done.failure is not None
    assert done.failure.contract == _contract() and done.failure.hidden_plan == _plan()
    assert done.failure.repairs == [rec]
    assert done.failure.review_flags == [
        "명세 가상 표시",
        "계약 단계 가상 표시",
        "빌드 단계 가상 표시",
    ]
    # 계약 단계에서 실패하면 남길 것이 없다
    store, b = await _claimed()
    done = await run_job(store, FakeSteps(tmp_path, fail_at="contract"), _cfg(tmp_path), b)
    assert done.status == "failed" and done.failure is None


async def test_budget_stops_before_the_next_stage(tmp_path: Path) -> None:
    store, b = await _claimed()
    steps = FakeSteps(tmp_path, cost={"contract": 0.6})
    done = await run_job(store, steps, _cfg(tmp_path, budget=0.5), b)
    assert done.status == "failed" and done.error is not None and done.error.startswith("비용 상한")
    assert steps.calls == ["contract"]  # 빌드(LLM 비용이 큰 단계)로 가지 않았다


async def test_unexpected_error_keeps_only_the_stage_and_type(tmp_path: Path) -> None:
    """예외 메시지(생성 코드·명세가 섞일 수 있음)는 저장소에 넣지 않고 작업 폴더 error.txt 에만."""
    store, b = await _claimed()
    steps = FakeSteps(tmp_path, fail_at="images", error=RuntimeError(SECRET))
    done = await run_job(store, steps, _cfg(tmp_path), b)
    assert (done.status, done.error) == ("failed", "images: RuntimeError")
    assert SECRET in (steps.works[0] / "error.txt").read_text("utf-8")


async def test_package_failure_marks_the_build_failed(tmp_path: Path) -> None:
    """묶음 등록이 실패하면 승인할 수 없는 빌드 — review 로 가지 않고 failed 로 남긴다 (사람이 다시 요청)."""
    store, b = await _claimed()
    done = await run_job(store, FakeSteps(tmp_path, fail_at="package"), _cfg(tmp_path), b)
    assert (done.status, done.error) == ("failed", "package: 가상 실패")


async def test_heartbeat_during_packaging_keeps_the_lease(tmp_path: Path) -> None:
    """2026-11-17 ct-1eaccb671a03-b1: 묶음 단계 직전에 review 로 바꿔서, 묶음을 쓰는 동안 돈 heartbeat 가 '진행 중 아님'으로
    실패해 작업이 취소됐다 — 응시용 묶음만 쓰고 등록 없이 review 로 남음. 묶음을 다 등록한 뒤 review 로 바꾼다."""
    store, b = await _claimed(worker="w1")
    seen: list[str] = []

    class SlowPackaging(FakeSteps):
        async def register(
            self, store: BuildRepo, job: Job, c: ContractResult, b: BuildResult, i: ImagesResult
        ) -> None:
            now = await store.get(job.build.build_id)
            seen.append(now.status if now is not None else "-")
            for _ in range(5):  # 묶음을 쓰는 동안 heartbeat 가 여러 번 돈다
                await asyncio.sleep(0)
            await super().register(store, job, c, b, i)

    async def instant(_: float) -> None:
        await asyncio.sleep(0)

    got = await run_with_lease(
        store, SlowPackaging(tmp_path), _cfg(tmp_path), b, worker="w1", sleep=instant
    )
    assert got is not None and got.status == "review"
    assert seen == ["verifying"]  # 묶음을 쓰는 동안은 아직 진행 중 상태
    assert [a.role for a in await store.artifacts(b.build_id)] == ["candidate", "grader"]


async def test_a_heartbeat_store_error_is_logged_and_retried_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#245: heartbeat 가 저장소 예외(DB 일시 오류)를 내면 그 태스크가 예외로 끝나(이후 lease 를 늘리지 않음), 작업이 끝난 뒤
    finally 의 await 가 그 예외를 다시 던졌다 — 결과는 이미 저장됐는데 worker 프로세스가 끝났다. 이제 기록하고 다음 차례에 다시."""
    store, b = await _claimed(worker="w1")
    real = store.heartbeat
    calls: list[str] = []

    async def flaky(build_id: str, worker: str, lease_s: float, at: datetime | None = None) -> bool:
        calls.append(build_id)
        if len(calls) == 1:  # 메시지(접속 정보·SQL 이 섞일 수 있음)는 로그에 남기지 않는다
            raise RuntimeError("가상 DB 오류 SECRET-DSN")
        return await real(build_id, worker, lease_s, at)

    class SlowPackaging(FakeSteps):
        async def register(
            self, store: BuildRepo, job: Job, c: ContractResult, b: BuildResult, i: ImagesResult
        ) -> None:
            for _ in range(5):  # 묶음을 쓰는 동안 heartbeat 가 여러 번 돈다
                await asyncio.sleep(0)
            await super().register(store, job, c, b, i)

    async def instant(_: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(store, "heartbeat", flaky)
    log = logging.getLogger("task_spec.builder.runner")
    log.addHandler(caplog.handler)  # task_spec 로거가 전파를 꺼도(configure_logging) 잡게
    try:
        got = await run_with_lease(
            store, SlowPackaging(tmp_path), _cfg(tmp_path), b, worker="w1", sleep=instant
        )
    finally:
        log.removeHandler(caplog.handler)
    assert got is not None and got.status == "review"
    assert len(calls) >= 2  # 다음 차례에 다시 불렀다
    msgs = [r.getMessage() for r in caplog.records]
    assert any(b.build_id in m and "RuntimeError" in m for m in msgs)  # 빌드 id·예외 종류만
    assert not any("SECRET-DSN" in m for m in msgs)


async def test_a_build_in_review_is_not_cancelled_while_cleaning_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#221 그대로: review 로 바꾼 뒤 정리(이미지 지우기) 중에는 heartbeat 가 '진행 중 아님'(False)이어도 이 worker 의 빌드면
    작업을 취소하지 않는다 — #245 에서 heartbeat 를 다시 짜도 같아야 한다."""
    store, b = await _claimed(worker="w1")
    real = store.heartbeat
    seen: list[bool] = []

    async def spy(build_id: str, worker: str, lease_s: float, at: datetime | None = None) -> bool:
        seen.append(await real(build_id, worker, lease_s, at))
        return seen[-1]

    class SlowCleanup(FakeSteps):
        async def cleanup(self, images: list[str]) -> None:
            for _ in range(5):  # 빌드는 이미 review — 그사이 heartbeat 가 돈다
                await asyncio.sleep(0)
            await super().cleanup(images)

    async def instant(_: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(store, "heartbeat", spy)
    steps = SlowCleanup(tmp_path)
    got = await run_with_lease(store, steps, _cfg(tmp_path), b, worker="w1", sleep=instant)
    assert got is not None and got.status == "review"
    assert False in seen  # review 뒤 heartbeat 가 실제로 False 를 받았다
    assert steps.cleaned == [["localhost/task/가상-candidate"]]  # 정리를 끝까지 했다


async def test_a_build_that_died_while_packaging_is_not_retried(tmp_path: Path) -> None:
    """#244: 응시용 묶음을 등록한 직후 builder 가 죽으면(SIGKILL·OOM) 복구가 묶음 행을 남긴 채 다시 대기열에 넣어, 재시도가
    생성·검증을 처음부터 다 하고(LLM 비용) 묶음 단계에서 '이미 등록된 묶음'으로 실패했다 (failed, package: BuildRuleError).
    이제 복구가 failed + lease_expired 로 두고 다시 가져가지 않는다 — 사람이 다시 요청한다."""
    store, b = await _claimed()
    registered = asyncio.Event()

    class DiesWhilePackaging(FakeSteps):
        async def register(
            self, store: BuildRepo, job: Job, c: ContractResult, b: BuildResult, i: ImagesResult
        ) -> None:
            await store.put_artifact(_artifact(job.build.build_id, "candidate", H_OLD))
            registered.set()
            await asyncio.Event().wait()  # 채점용을 등록하기 전에 프로세스가 죽는다

    first = asyncio.create_task(run_job(store, DiesWhilePackaging(tmp_path), _cfg(tmp_path), b))
    await registered.wait()
    first.cancel()  # 죽은 것처럼 — 상태·lease 는 그대로 남는다
    with contextlib.suppress(asyncio.CancelledError):
        await first
    assert b.lease_until is not None
    await store.recover_expired(b.lease_until + timedelta(seconds=1), max_attempts=2)
    steps = FakeSteps(tmp_path)
    w = Worker(
        store=store,
        steps=steps,
        cfg=_cfg(tmp_path),
        name="w2",
        daily_budget_usd=5.0,
        poll_seconds=0.01,
        max_attempts=2,
    )
    got = await w.once()
    now = await store.get(b.build_id)
    assert now is not None and (now.status, now.error) == ("failed", LEASE_EXPIRED)
    assert got is None and steps.calls == []  # 생성·검증을 다시 하지 않았다


async def test_losing_the_lease_stops_the_job_without_touching_the_build(tmp_path: Path) -> None:
    store, b = await _claimed(worker="w1")

    async def instant(_: float) -> None:
        await asyncio.sleep(0)

    steps = FakeSteps(tmp_path, block_build=True)
    got = await run_with_lease(
        store, steps, _cfg(tmp_path), b, worker="w2", sleep=instant
    )  # 다른 worker 의 lease
    assert got is None
    now = await store.get(b.build_id)
    assert (
        now is not None and now.status == "generating"
    )  # 복구는 lease 를 쥔 쪽·recover_expired 몫


async def test_worker_takes_one_request_at_a_time(tmp_path: Path) -> None:
    store, _ = await _claimed()
    await store.fail(f"{CID}-b1", "가상")  # 첫 요청은 정리하고 새로 요청
    await store.request_build(CID, "가상승인자")
    w = Worker(
        store=store,
        steps=FakeSteps(tmp_path),
        cfg=_cfg(tmp_path),
        name="w1",
        daily_budget_usd=5.0,
        poll_seconds=0.01,
        max_attempts=2,
    )
    done = await w.once()
    assert done is not None and done.status == "review" and done.build_id == f"{CID}-b2"
    assert await w.once() is None  # 빈 큐


async def test_a_failed_stage_still_counts_its_llm_cost(tmp_path: Path) -> None:
    """2026-11-12 스모크: 계약을 3번 부르고 실패했는데 비용이 $0 으로 남아 하루 상한에 안 잡혔다."""
    store, b = await _claimed()
    steps = FakeSteps(
        tmp_path, fail_at="contract", error=StepFailed("contract: 가상 실패", cost_usd=0.03)
    )
    done = await run_job(store, steps, _cfg(tmp_path), b)
    assert done.status == "failed" and done.cost_usd == pytest.approx(0.03)
