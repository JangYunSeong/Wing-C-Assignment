"""승인 결정이 겹치거나 늦게 들어올 때 (#247) — 두 번째 결정은 409, 아무도 안 본 판에 적용하지 않는다.

ASGI 앱 + InMemorySaver (가상 데이터). raise_app_exceptions=False — 앱이 500 을 내면 예외가 아니라 응답으로 본다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from task_spec.api.app import create_app
from task_spec.graph import build_graph, make_checkpointer
from tests.api.conftest import FAKE_FILE, make_settings, reviewer_decision, wait_status


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async with make_checkpointer(kind="memory") as cp:
        app = create_app(settings=make_settings(tmp_path), graph=build_graph(cp))
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            yield client
        await app.state.launcher.shutdown()


async def _pending(api: httpx.AsyncClient) -> str:
    resp = await api.post(
        "/runs", data={"posting_id": "posting-fake-001"}, files=[("resumes", FAKE_FILE)]
    )
    assert resp.status_code == 202, resp.text
    run_id = str(resp.json()["run_id"])
    await wait_status(api, run_id, "awaiting_approval")
    return run_id


async def _until(api: httpx.AsyncClient, run_id: str, status: str, revision: int) -> None:
    async with asyncio.timeout(5):
        while True:
            view = (await api.get(f"/runs/{run_id}")).json()
            if view["status"] == status and view["revision"] == revision:
                return
            await asyncio.sleep(0.02)


async def test_concurrent_decisions_one_is_applied_the_other_is_409(
    api: httpx.AsyncClient,
) -> None:
    """이슈 재현: 같은 실행에 결정 2건을 동시에 — 예전에는 (202, 500) 이 30회 중 25회."""
    for _ in range(5):
        run_id = await _pending(api)
        a, b = await asyncio.gather(
            api.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved")),
            api.post(f"/runs/{run_id}/approve", json=reviewer_decision("rejected")),
        )
        assert sorted([a.status_code, b.status_code]) == [202, 409], (a.text, b.text)
        lost = a if a.status_code == 409 else b
        assert lost.json()["error"] == {
            "code": "approval_not_pending",
            "message": lost.json()["error"]["message"],
            "run_id": run_id,
        }
        # 202 를 받은 결정이 적용된 결정이다
        final = await wait_status(api, run_id, "done", "rejected")
        assert final["status"] == ("done" if a.status_code == 202 else "rejected")


class _GatedStore:
    """승인 전 공통 과제 확인(missing)에서 멈추는 저장소 — 상태를 읽은 결정이 띄우기 전에 다른 결정이 끝나게 한다."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def missing(self, common_ids: list[str]) -> list[str]:
        self.entered.set()
        await self.release.wait()
        return []


@pytest.mark.parametrize(
    ("first", "settled", "revision"),
    [("rejected", "rejected", 1), ("revise", "awaiting_approval", 2)],
)
async def test_decision_read_before_another_ran_is_409(
    api: httpx.AsyncClient, first: str, settled: str, revision: int
) -> None:
    """먼저 들어온 결정이 다 끝난 뒤 늦은 결정이 띄워지면 예전에는 202 — 반려된 실행에선 무시되고,
    수정 요청 뒤에는 아무도 안 본 2판에 승인이 적용됐다."""
    run_id = await _pending(api)
    store = _GatedStore()
    api._transport.app.state.common_store = store  # type: ignore[attr-defined]
    late = asyncio.create_task(
        api.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    )
    await asyncio.wait_for(store.entered.wait(), 5)  # 1판 승인 대기를 읽고 멈춤
    body: dict[str, Any] = reviewer_decision(first, "가상 사유" if first == "revise" else None)
    assert (await api.post(f"/runs/{run_id}/approve", json=body)).status_code == 202
    await _until(api, run_id, settled, revision)

    store.release.set()
    resp = await late
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "approval_not_pending"
    await asyncio.sleep(0.1)  # 잘못 띄웠다면 그 실행이 돌 시간
    await _until(api, run_id, settled, revision)  # 반려 그대로 / 2판 승인 대기 그대로


async def test_decision_for_a_revision_nobody_is_waiting_on_is_409(
    api: httpx.AsyncClient,
) -> None:
    """?revision= 은 승인자가 보고 있는 판 — 그 사이 다시 만들어졌으면 409, 실행은 그대로 승인 대기."""
    run_id = await _pending(api)
    resp = await api.post(
        f"/runs/{run_id}/approve",
        params={"revision": 1},
        json=reviewer_decision("revise", "가상 사유"),
    )
    assert resp.status_code == 202, resp.text
    await _until(api, run_id, "awaiting_approval", 2)

    stale = await api.post(
        f"/runs/{run_id}/approve", params={"revision": 1}, json=reviewer_decision("approved")
    )
    assert stale.status_code == 409, stale.text
    err = stale.json()["error"]
    assert err["code"] == "approval_revision_mismatch" and err["run_id"] == run_id
    assert "1판" in err["message"] and "2판" in err["message"]
    await asyncio.sleep(0.1)
    await _until(api, run_id, "awaiting_approval", 2)

    bad = await api.post(
        f"/runs/{run_id}/approve", params={"revision": 0}, json=reviewer_decision("approved")
    )
    assert bad.status_code == 422
    ok = await api.post(
        f"/runs/{run_id}/approve", params={"revision": 2}, json=reviewer_decision("approved")
    )
    assert ok.status_code == 202, ok.text
    await wait_status(api, run_id, "done")
