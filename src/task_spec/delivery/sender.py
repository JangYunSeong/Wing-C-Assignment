"""전달 백그라운드 실행기 (이슈 #116 2단계) — API 가 전달 기록(requested)을 만든 뒤 여기에 넘긴다.

`asyncio.create_task` + 프로세스 내 레지스트리 (api/launcher.py 와 같은 방식). 전달 상태의 SSOT 는 저장소의 artifact_delivery.
API 는 작업자 1개라(Dockerfile `--workers 1`) 시작할 때 남은 requested 를 실패(`interrupted`)로 돌린다 — 다시 요청하면 된다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from task_spec.delivery.sftp import SendResult, remote_subdir, send_role_dir
from task_spec.delivery.target import SftpTarget
from task_spec.schemas.build_record import Delivery
from task_spec.schemas.package import ArtifactFile
from task_spec.starter.store import BuildRepo, role_dir

logger = logging.getLogger(__name__)

SendFn = Callable[[SftpTarget, Path, Sequence[ArtifactFile], str, float], Awaitable[SendResult]]


class DeliverySender:
    def __init__(
        self,
        store: BuildRepo,
        images_root: Path,
        targets: Mapping[str, SftpTarget],
        timeout_s: float,
        send: SendFn = send_role_dir,
    ) -> None:
        self._store = store
        self._images_root = images_root
        self._targets = dict(targets)
        self._timeout_s = timeout_s
        self._send = send
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def has_target(self, name: str) -> bool:
        return name in self._targets

    def is_active(self, delivery_id: str) -> bool:
        task = self._tasks.get(delivery_id)
        return task is not None and not task.done()

    async def recover(self) -> int:
        """시작할 때 — 지난 프로세스에서 보내다 멈춘 전달을 실패로."""
        left = await self._store.abandon_deliveries("interrupted", datetime.now(UTC))
        if left:
            logger.warning("deliveries interrupted by restart: count=%d", len(left))
        return len(left)

    def launch(self, delivery: Delivery, common_id: str, revision: int) -> None:
        """전달 기록 하나를 백그라운드로 보내고 즉시 반환한다."""
        self._tasks[delivery.delivery_id] = asyncio.create_task(
            self._run(delivery, remote_subdir(common_id, revision, delivery.role))
        )

    async def _run(self, d: Delivery, sub: str) -> None:
        logger.info("delivery start id=%s build=%s role=%s", d.delivery_id, d.build_id, d.role)
        try:
            target = self._targets[d.target]  # API 가 요청 때 설정에 있는 대상만 받는다
            local = role_dir(self._images_root, d.build_id, d.role)
            res = await self._send(target, local, d.files, sub, self._timeout_s)
            await self._store.finish_delivery(d.delivery_id, res.ok, datetime.now(UTC), res.error)
            logger.info(
                "delivery done id=%s ok=%s error=%s bytes=%d",
                d.delivery_id,
                res.ok,
                res.error,
                res.bytes_sent,
            )
        except asyncio.CancelledError:
            raise  # 종료 중 — 다음 시작 때 recover 가 실패로 돌린다
        except Exception as e:  # 원문 없이 타입만
            logger.error("delivery crashed id=%s exc=%s", d.delivery_id, type(e).__name__)
            with contextlib.suppress(Exception):
                await self._store.finish_delivery(
                    d.delivery_id, False, datetime.now(UTC), "internal"
                )
        finally:
            self._tasks.pop(d.delivery_id, None)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
