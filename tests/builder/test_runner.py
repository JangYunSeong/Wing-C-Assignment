"""builder 작업 실행기 (#129) — 가짜 단계 + 메모리 저장소로 상태 전이·비용 상한·실패 사유·lease 를 본다. 가상 데이터만, LLM·podman 없이."""

from __future__ import annotations

import asyncio
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
from task_spec.schemas.build_record import BuildRecord
from task_spec.schemas.package import VerifySummary
from task_spec.starter.static_check import Overlay
from task_spec.starter.store import BuildRepo, CommonRef, InMemoryBuildStore, TaskLabel
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


async def test_failure_after_review_marks_the_build_failed(tmp_path: Path) -> None:
    """review 뒤 묶음 등록이 실패하면 승인할 수 없는 빌드 — failed 로 남긴다 (사람이 다시 요청)."""
    store, b = await _claimed()
    done = await run_job(store, FakeSteps(tmp_path, fail_at="package"), _cfg(tmp_path), b)
    assert (done.status, done.error) == ("failed", "package: 가상 실패")


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
