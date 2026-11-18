"""빌드 작업 실행기 — 큐에서 가져간 빌드 하나를 review 까지 (상태 전이·비용 상한·lease·실패 사유·정리).

생성·이미지 단계(`Steps`)는 바꿔 끼운다 — 실제는 `builder/steps.py`, 시험은 가짜. 순서:

    claim(generating) ─ contract ─ build ─ 파일·README 저장 ─ verifying ─ images ─ 결과 저장 ─ 묶음 등록 ─ review

- 비용: 단계마다 provenance 비용을 `record_stage` 로 쌓고, 작업당 상한을 넘으면 다음 단계로 가지 않는다 (단계 중간에는 끊지 않음 —
  수리 루프는 MAX_ROUNDS 가 막는다). 하루 상한은 `claim` 이 막는다.
- lease: heartbeat 를 lease 의 1/3 마다. lease 를 잃으면(다른 worker 가 복구했거나 실패로 바뀜) 이 작업을 멈추고 상태를 건드리지 않는다.
  heartbeat 의 저장소 오류(DB 일시 오류)는 빌드 id·예외 종류만 기록하고 다음 차례에 다시 (#245).
- 실패: 사유는 짧게 (단계 + 원인 요약 또는 예외 종류) — 생성 코드·명세 원문·예외 메시지는 저장소·로그에 넣지 않는다.
  예외 전문은 작업 폴더의 error.txt 에만 (git 밖, 실패 작업 폴더를 남길 때).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from task_spec.schemas import HiddenPlan, TaskContract, TaskItem
from task_spec.schemas.build import RepairRecord
from task_spec.schemas.build_record import (
    README_FULL_PATH,
    README_PATH,
    BuildFailure,
    BuildOutcome,
    BuildRecord,
    StarterFile,
)
from task_spec.schemas.incident import FeatureSeed
from task_spec.schemas.package import VerifySummary
from task_spec.starter.artifacts import merged_flags
from task_spec.starter.static_check import Overlay
from task_spec.starter.store import BuildRepo, TaskLabel
from task_spec.starter.templates import Template

logger = logging.getLogger(__name__)


class StepFailed(Exception):
    """생성·검증이 끝까지 가지 못함 — 메시지는 저장소 실패 사유로 그대로 (짧게, 원문 없이).
    cost_usd: 실패하기 전까지 그 단계가 쓴 LLM 비용 — 하루 상한에 들어가야 한다 (2026-11-12 스모크: 계약 3회 실패가 $0 으로 남음)."""

    def __init__(
        self, message: str, cost_usd: float = 0.0, detail: BuildFailure | None = None
    ) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd
        self.detail = (
            detail  # 실패한 단계가 그때까지 만든 것 (숨은 계획·표시·수리 기록) — 실패 빌드 기록에
        )


class BudgetExceeded(StepFailed):
    pass


@dataclass(frozen=True)
class Job:
    build: BuildRecord
    task: TaskItem  # 승인된 공통 과제 명세 (공고·재료에서 — 지원자 문서 없음)
    label: TaskLabel  # 제목·공고 id·판
    work: Path  # 이 빌드의 작업 폴더 (절대 경로)


@dataclass(frozen=True)
class ContractResult:
    template: Template
    contract: TaskContract
    plan: HiddenPlan
    cost_usd: float
    seed: FeatureSeed | None = None  # 재료의 기능형 칸 — 생성기 입력 그대로 넘긴다
    avoid_terms: tuple[str, ...] = ()
    # 계약 단계의 승인자 표시 (plan_contract flags — '운영 값 후보가 여럿', 근거 조각 등). 예전에는 운영 경로가 버려 승인 화면에
    # 나오지 않았다 (평가 러너에서만 보였다)
    review_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildResult:
    overlays: dict[str, Overlay]  # starter · hidden · public · 참조 구현 라벨
    plan: HiddenPlan  # 변별 못 하는 케이스를 뺀 뒤의 숨은 계획 (이미지·저장이 쓴다)
    readme: str  # 지원자 README (시작 코드가 정해진 뒤 렌더) — 문제 분석 판은 얇은 판
    review_flags: list[str]
    repairs: list[RepairRecord]
    cost_usd: float
    readme_full: str | None = None  # 문제 분석 판의 원래 판 README — 승인 화면·평가 키트에만


@dataclass(frozen=True)
class ImagesResult:
    template_ref: str
    tree_sha256: str
    grader_sha256: str
    verification: dict[str, VerifySummary]  # 제출물(starter-only·참조 구현)마다 채점 결과
    manifest_sha256: str | None = None  # 검사·수리 칸 판 (#176)
    cleanup: list[str] = field(default_factory=list)  # 작업이 끝나면 지울 로컬 이미지 이름
    package: object | None = None  # Steps.register 가 쓰는 것 (실제: loader.PackageInputs 재료)


class Finalizer(Protocol):
    """승인된 빌드의 묶음 확정 (#130 ③ A, `builder/finalize.py`)."""

    async def run_pending(self) -> int: ...


class Steps(Protocol):
    async def contract(self, job: Job) -> ContractResult: ...

    async def build(self, job: Job, c: ContractResult) -> BuildResult: ...

    async def images(self, job: Job, c: ContractResult, b: BuildResult) -> ImagesResult: ...

    async def register(
        self, store: BuildRepo, job: Job, c: ContractResult, b: BuildResult, i: ImagesResult
    ) -> None: ...

    async def cleanup(self, images: list[str]) -> None: ...


@dataclass(frozen=True)
class RunConfig:
    work_root: Path
    budget_usd: float
    lease_seconds: int
    keep_failed_work: bool = True


def _fresh_dir(work: Path) -> None:
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)


def _write_error(work: Path, exc: BaseException) -> None:
    if work.is_dir():
        (work / "error.txt").write_text(
            "".join(traceback.format_exception(exc)), "utf-8", errors="replace"
        )


def overlay_files(
    build_id: str, overlays: dict[str, Overlay], readme: str, readme_full: str | None = None
) -> list[StarterFile]:
    out = [
        StarterFile.of(build_id, o.kind, f.path, f.content, o.label)
        for o in overlays.values()
        for f in o.files
    ]
    out.append(StarterFile.of(build_id, "readme", README_PATH, readme))
    if readme_full is not None:
        out.append(StarterFile.of(build_id, "readme_full", README_FULL_PATH, readme_full))
    return out


class _Run:
    """빌드 하나의 진행 — 단계 기록·비용 누계."""

    def __init__(self, store: BuildRepo, cfg: RunConfig, build: BuildRecord) -> None:
        self.store, self.cfg, self.build = store, cfg, build
        self.stage = "start"
        self.spent = 0.0

    async def enter(self, stage: str, cost_usd: float = 0.0) -> None:
        """이전 단계의 비용을 더하고 다음 단계로 — 작업당 상한을 넘으면 멈춘다."""
        self.spent += cost_usd
        self.build = await self.store.record_stage(self.build.build_id, stage, cost_usd)
        self.stage = stage
        if self.spent > self.cfg.budget_usd:
            raise BudgetExceeded(
                f"비용 상한 — {stage} 전까지 ${self.spent:.3f} > ${self.cfg.budget_usd:.2f} (BUILD_BUDGET_USD)"
            )


async def run_job(
    store: BuildRepo, steps: Steps, cfg: RunConfig, build: BuildRecord
) -> BuildRecord:
    """claim 으로 가져간(generating) 빌드 하나를 review 까지. 실패하면 failed + 사유. 작업 폴더는 성공이면 지운다."""
    work = cfg.work_root / build.build_id
    run = _Run(store, cfg, build)
    images: list[str] = []
    ok = False
    task: TaskItem | None = None
    c: ContractResult | None = None
    b: BuildResult | None = None
    try:
        task = await store.task(build.common_id)
        label = (await store.task_labels([build.common_id])).get(build.common_id)
        if task is None or label is None:
            raise StepFailed("공통 과제 명세 없음")
        await asyncio.to_thread(_fresh_dir, work)  # 이전 시도의 반쯤 쓴 폴더는 지운다
        job = Job(build=build, task=task, label=label, work=work)

        await run.enter("contract")
        c = await steps.contract(job)
        await run.enter("build", c.cost_usd)
        b = await steps.build(job, c)
        await run.enter("files", b.cost_usd)
        await store.put_files(
            build.build_id, overlay_files(build.build_id, b.overlays, b.readme, b.readme_full)
        )
        await store.set_status(build.build_id, "verifying")
        await run.enter("images")
        i = await steps.images(job, c, b)
        images = i.cleanup
        await store.save_outcome(
            build.build_id,
            BuildOutcome(
                template_ref=i.template_ref,
                manifest_sha256=i.manifest_sha256,
                tree_sha256=i.tree_sha256,
                grader_sha256=i.grader_sha256,
                contract=c.contract,
                hidden_plan=b.plan,
                verification=i.verification,
                review_flags=merged_flags(task.review_flags, c.review_flags, b.review_flags),
                repairs=b.repairs,
            ),
        )
        await run.enter("package")
        await steps.register(store, job, c, b, i)
        # 묶음을 다 등록한 뒤에 review — 묶음을 쓰는 동안 상태가 verifying 이라 heartbeat 가 lease 를 늘린다.
        # 먼저 review 로 바꾸면 그사이 heartbeat 가 '진행 중 아님'으로 실패해 작업이 취소됐다 (2026-11-17 ct-1eaccb671a03-b1)
        await store.set_status(build.build_id, "review")
        ok = True
        done = await store.get(build.build_id)
        assert done is not None
        logger.info(
            "빌드 %s review (비용 $%.3f)",
            build.build_id,
            run.spent,
            extra={"build_id": build.build_id},
        )
        return done
    except asyncio.CancelledError:  # lease 를 잃었다 — 상태는 복구한 쪽이 정한다
        logger.warning("빌드 %s 중단: lease 를 잃음 (%s 단계)", build.build_id, run.stage)
        raise
    except StepFailed as e:
        if e.cost_usd:  # 실패한 단계가 쓴 비용도 쌓는다
            with contextlib.suppress(Exception):
                await store.record_stage(build.build_id, run.stage, e.cost_usd)
        detail = failure_detail(task, c, b, e.detail)
        return await _fail(store, work, build, str(e), run.stage, None, detail)
    except Exception as e:  # 인프라·코드 오류 — 사유는 단계와 예외 종류만
        detail = failure_detail(task, c, b, None)
        return await _fail(
            store, work, build, f"{run.stage}: {type(e).__name__}", run.stage, e, detail
        )
    finally:
        with contextlib.suppress(Exception):
            await steps.cleanup(images)
        if ok or not cfg.keep_failed_work:
            await asyncio.to_thread(shutil.rmtree, work, True)


def failure_detail(
    task: TaskItem | None,
    c: ContractResult | None,
    b: BuildResult | None,
    part: BuildFailure | None,
) -> BuildFailure | None:
    """끝까지 가지 못한 빌드가 그때까지 만든 것 — 계약(계약 단계를 지났으면)·숨은 계획·승인자 표시·수리 기록.
    빌드 단계가 실패하면 그 단계가 StepFailed.detail 로 수리 기록을 넘긴다 (빌드 결과는 돌려주지 못하므로). 아무것도 없으면 None."""
    if c is None and b is None and part is None:
        return None
    plan = (
        (b.plan if b is not None else None)
        or (part.hidden_plan if part is not None else None)
        or (c.plan if c is not None else None)
    )
    return BuildFailure(
        contract=c.contract if c is not None else (part.contract if part is not None else None),
        hidden_plan=plan,
        review_flags=merged_flags(
            task.review_flags if task is not None else [],
            c.review_flags if c is not None else (),
            b.review_flags if b is not None else (),
            part.review_flags if part is not None else (),
        ),
        repairs=list(b.repairs if b is not None else (part.repairs if part is not None else [])),
    )


async def _fail(
    store: BuildRepo,
    work: Path,
    build: BuildRecord,
    reason: str,
    stage: str,
    exc: BaseException | None,
    detail: BuildFailure | None = None,
) -> BuildRecord:
    if exc is not None:  # 예외 전문은 작업 폴더에만 (git 밖, 저장소·로그에는 넣지 않음)
        await asyncio.to_thread(_write_error, work, exc)
    logger.error(
        "빌드 %s 실패 (%s 단계): %s",
        build.build_id,
        stage,
        type(exc).__name__ if exc is not None else "단계 실패",
        extra={"build_id": build.build_id},
    )
    current = await store.get(build.build_id)
    if current is None or current.status not in ("pending", "generating", "verifying", "review"):
        return current or build  # 이미 끝난 빌드 (다른 쪽이 실패 처리)
    return await store.fail(build.build_id, reason, detail=detail)


async def run_with_lease(
    store: BuildRepo,
    steps: Steps,
    cfg: RunConfig,
    build: BuildRecord,
    worker: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BuildRecord | None:
    """lease 를 heartbeat 로 붙든 채 run_job. lease 를 잃으면 작업을 취소하고 None.
    heartbeat 의 저장소 오류(DB 일시 오류)는 기록하고 다음 차례에 다시 — 작업을 멈추거나 worker 를 끝내지 않는다 (#245)."""
    job = asyncio.create_task(run_job(store, steps, cfg, build))

    async def beat() -> None:
        while not job.done():
            await sleep(cfg.lease_seconds / 3)
            if job.done():
                return
            try:
                if await store.heartbeat(build.build_id, worker, cfg.lease_seconds):
                    continue
                # 이 작업이 묶음까지 등록하고 review 로 바꿨으면 lease 가 더 필요 없다 — 남은 정리(이미지·작업 폴더)는 끝까지
                now = await store.get(build.build_id)
            except Exception as e:
                # DB 일시 오류 — 기록하고 다음 차례에 다시. 예전에는 이 태스크가 예외로 끝나 lease 를 더 늘리지 않았고,
                # 작업이 끝난 뒤 아래 finally 가 그 예외를 다시 던져 worker 가 끝났다 (#245)
                logger.warning(
                    "빌드 %s heartbeat 실패 (%s) — 다음 차례에 다시",
                    build.build_id,
                    type(e).__name__,  # 메시지는 남기지 않는다 (접속 정보·SQL 이 섞일 수 있음)
                    extra={"build_id": build.build_id},
                )
                continue
            if now is None or now.status != "review" or now.worker != worker:
                job.cancel()
            return

    hb = asyncio.create_task(beat())
    try:
        return await job
    except asyncio.CancelledError:
        if hb.done() and not hb.cancelled():  # heartbeat 가 취소했다 — 이 worker 는 손을 뗀다
            return None
        raise
    finally:
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # beat 가 잡지 못한 예외도 작업 결과를 덮거나 worker 를 끝내지 않게 — 기록만 (#245)
            logger.warning(
                "빌드 %s heartbeat 실패 (%s)",
                build.build_id,
                type(e).__name__,
                extra={"build_id": build.build_id},
            )


@dataclass
class Worker:
    """큐를 하나씩 — 시작할 때와 빈 큐일 때 죽은 작업을 복구한다."""

    store: BuildRepo
    steps: Steps
    cfg: RunConfig
    name: str
    daily_budget_usd: float
    poll_seconds: float
    max_attempts: int
    finalizer: Finalizer | None = None  # 승인된 빌드의 묶음 확정 — 새 작업을 가져가기 전마다
    stopping: bool = False

    async def finalize_pending(self) -> None:
        if self.finalizer is None:
            return
        try:
            n = await self.finalizer.run_pending()
            if n:
                logger.info("묶음 확정 %d건", n)
        except (
            Exception
        ) as e:  # 확정 실패가 빌드 큐를 막지 않게 (빌드마다 사유는 finalizer 가 남긴다)
            logger.error("묶음 확정 처리 실패: %s", type(e).__name__)

    async def once(self, now: datetime | None = None) -> BuildRecord | None:
        """가져갈 것이 있으면 하나 처리하고 그 결과, 없으면 None."""
        build = await self.store.claim(
            self.name,
            self.cfg.lease_seconds,
            at=now or datetime.now(UTC),
            daily_budget_usd=self.daily_budget_usd,
        )
        if build is None:
            return None
        logger.info(
            "빌드 %s 시작 (시도 %d)",
            build.build_id,
            build.attempt,
            extra={"build_id": build.build_id},
        )
        return await run_with_lease(self.store, self.steps, self.cfg, build, self.name)

    async def serve(self, once: bool = False) -> None:
        for b in await self.store.recover_expired(max_attempts=self.max_attempts):
            logger.warning("죽은 작업 복구: %s → %s", b.build_id, b.status)
        while not self.stopping:
            await (
                self.finalize_pending()
            )  # 승인된 빌드를 먼저 (몇 초, LLM 없음) — 전달이 기다리지 않게
            got = await self.once()
            if once:
                return
            if got is None:
                await asyncio.sleep(self.poll_seconds)
                for b in await self.store.recover_expired(max_attempts=self.max_attempts):
                    logger.warning("죽은 작업 복구: %s → %s", b.build_id, b.status)
