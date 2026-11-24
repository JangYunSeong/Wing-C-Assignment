"""builder 진입점 — `uv run python -m task_spec.builder [--once]`.

설정은 BuilderSettings (.env·환경 변수). SIGTERM·SIGINT 를 받으면 지금 작업을 끝까지 두고(컨테이너가 기다려 주는 동안) 다음 작업을
가져가지 않는다 — 도중에 죽으면 lease 가 지나 다른 worker 의 recover_expired 가 다시 대기열에 넣는다 (묶음 등록 중이었으면 실패로).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from task_spec.builder.finalize import PackageFinalizer
from task_spec.builder.runner import RunConfig, Worker
from task_spec.builder.settings import BuilderSettings
from task_spec.builder.steps import RealSteps
from task_spec.llm.factory import build_llm_client
from task_spec.logging_setup import configure_logging
from task_spec.prompting import PromptRegistry
from task_spec.spec.company_incidents import CompanyIncidentStore
from task_spec.starter.store import PgBuildStore

logger = logging.getLogger("task_spec.builder")


async def main(once: bool) -> None:
    s = BuilderSettings()  # type: ignore[call-arg]
    configure_logging(s.log_level, s.log_format)
    # 표는 API(표 주인)가 만든다 — builder 전용 DB 역할은 DDL 을 못 돌린다
    store = await PgBuildStore.open(s.database_url.get_secret_value(), ddl=False)
    # 회사 결정적 사건 (co:…) 읽기만 — 권한은 db-role.sql (GRANT SELECT ON public.company_incident)
    company = await CompanyIncidentStore.open(s.database_url.get_secret_value(), ddl=False)
    try:
        worker = Worker(
            store=store,
            steps=RealSteps(
                build_llm_client(s),
                PromptRegistry(s.prompts_dir),
                s.incidents_dir,
                s.images_dir.resolve(),
                designs=s.build_designs,
                company_incidents=company,
            ),
            cfg=RunConfig(
                work_root=s.work_root,
                budget_usd=s.build_budget_usd,
                lease_seconds=s.build_lease_seconds,
                keep_failed_work=s.build_keep_failed_work,
            ),
            name=s.build_worker,
            daily_budget_usd=s.build_daily_budget_usd,
            poll_seconds=s.build_poll_seconds,
            max_attempts=s.build_max_attempts,
            finalizer=PackageFinalizer(store, s.images_dir.resolve(), s.incidents_dir),
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):

            def stop(sig: signal.Signals = sig) -> None:
                logger.info("%s — 지금 작업 뒤 멈춤", sig.name)
                worker.stopping = True

            loop.add_signal_handler(sig, stop)
        logger.info(
            "builder 시작 (worker %s, 작업당 $%.2f, 하루 $%.2f)",
            worker.name,
            s.build_budget_usd,
            s.build_daily_budget_usd,
        )
        await worker.serve(once=once)
    finally:
        await store.close()
        await company.close()


def cli() -> None:
    ap = argparse.ArgumentParser(prog="python -m task_spec.builder")
    ap.add_argument("--once", action="store_true", help="가져갈 작업이 있으면 하나만 하고 끝낸다")
    a = ap.parse_args()
    asyncio.run(main(a.once))


if __name__ == "__main__":
    cli()
