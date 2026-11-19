"""채용담당자 인터뷰 과제가 있는 공고의 실행 API [확정: 2026-11-19 사용자] — 만들기·승인·수정 요청.

ASGI 앱 + memory 체크포인터 + InMemoryCommonStore (그래프와 API 가 같은 저장소). 가상 데이터만.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from task_spec.api.app import create_app
from task_spec.graph import build_graph, make_checkpointer
from task_spec.spec.store import CommonKey, InMemoryCommonStore, StoredCommon
from tests.api.conftest import FAKE_FILE, make_settings, reviewer_decision, wait_status
from tests.spec.test_interview_common import _task

POSTING = "posting-fake-001"
ONTO = "onto-v2026.11.02"  # make_settings 의 ontology_version (온톨로지 없는 앱은 이 판으로 실행)


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, InMemoryCommonStore]]:
    store = InMemoryCommonStore()
    async with make_checkpointer(kind="memory") as cp:
        app = create_app(
            settings=make_settings(tmp_path), graph=build_graph(cp, common_store=store)
        )
        app.state.common_store = store
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, store
        await app.state.launcher.shutdown()


async def _interview(
    store: InMemoryCommonStore,
    interview_id: str,
    *,
    version: str = ONTO,
    replaces: str | None = None,
) -> StoredCommon:
    key = CommonKey(POSTING, version, f"interview:{interview_id}")
    row = await store.save_draft(key, _task(f"가상 인터뷰 과제 {interview_id}"), [])
    await store.approve_interview(
        row.id, "recruiter-fake", datetime(2026, 11, 19, tzinfo=UTC), replaces=replaces
    )
    return row


async def _pending(client: httpx.AsyncClient) -> dict[str, Any]:
    resp = await client.post("/runs", data={"posting_id": POSTING}, files=[("resumes", FAKE_FILE)])
    assert resp.status_code == 202, resp.text
    return await wait_status(client, resp.json()["run_id"], "awaiting_approval")


async def _until_revision(client: httpx.AsyncClient, run_id: str, revision: int) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            view: dict[str, Any] = (await client.get(f"/runs/{run_id}")).json()
            if view["status"] == "awaiting_approval" and view["revision"] == revision:
                return view
            await asyncio.sleep(0.02)


def _row(store: InMemoryCommonStore, common_id: str) -> dict[str, Any]:
    return next(r for r in store.rows if r["id"] == common_id)


async def test_create_run_refuses_when_interview_task_is_on_other_version_only(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],
) -> None:
    client, store = api
    data_dir = client._transport.app.state.settings.data_dir  # type: ignore[attr-defined]
    await _interview(store, "iv-1", version="onto-v2026.10.01")

    resp = await client.post("/runs", data={"posting_id": POSTING}, files=[("resumes", FAKE_FILE)])
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "interview_task_version_mismatch"
    assert "onto-v2026.10.01" in err["message"] and ONTO in err["message"]
    assert not (data_dir / "raw").exists() or not any(
        (data_dir / "raw").iterdir()
    )  # 지원서를 남기지 않음

    await _interview(store, "iv-1", version=ONTO)  # 지금 판에서 다시 승인하면 실행할 수 있다
    view = await _pending(client)
    assert view["spec"]["common_refs"][0]["prompt_ref"] == "interview:iv-1"


async def test_run_uses_interview_task_and_approval_keeps_recruiter_record(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],
) -> None:
    client, store = api
    iv = await _interview(store, "iv-1")
    view = await _pending(client)
    assert [t["task_id"] for t in view["spec"]["common_tasks"]] == [iv.id]

    resp = await client.post(f"/runs/{view['run_id']}/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 202, resp.text
    await wait_status(client, view["run_id"], "done")
    row = _row(store, iv.id)
    assert (row["status"], row["approved_by"]) == ("approved", "recruiter-fake")


async def test_approve_after_interview_replacement_is_409_until_spec_is_remade(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],
) -> None:
    client, store = api
    first = await _interview(store, "iv-1")
    view = await _pending(client)
    run_id = view["run_id"]
    second = await _interview(store, "iv-2", replaces=first.id)

    stale = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    assert stale.status_code == 409, stale.text
    err = stale.json()["error"]
    assert err["code"] == "common_task_replaced" and first.id in err["message"]
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "awaiting_approval"

    body = {**reviewer_decision("revise", "인터뷰 과제 교체 반영"), "revise_target": "verification"}
    assert (await client.post(f"/runs/{run_id}/approve", json=body)).status_code == 202
    remade = await _until_revision(client, run_id, 2)
    assert remade["spec"]["common_tasks"][0]["task_id"] == second.id

    ok = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    assert ok.status_code == 202, ok.text
    await wait_status(client, run_id, "done")
    assert (_row(store, first.id)["status"], _row(store, second.id)["status"]) == (
        "superseded",
        "approved",
    )


@pytest.mark.parametrize("target", ["common", "all"])
async def test_common_revise_on_interview_task_is_409(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore], target: str
) -> None:
    client, store = api
    iv = await _interview(store, "iv-1")
    run_id = (await _pending(client))["run_id"]

    body = {**reviewer_decision("revise", "상황을 줄여 주세요"), "revise_target": target}
    resp = await client.post(f"/runs/{run_id}/approve", json=body)
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "interview_task_locked" and iv.id in err["message"]
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "awaiting_approval"

    body = {
        **reviewer_decision("revise", "면접 질문을 더 구체적으로"),
        "revise_target": "verification",
    }
    assert (await client.post(f"/runs/{run_id}/approve", json=body)).status_code == 202
    await _until_revision(client, run_id, 2)
