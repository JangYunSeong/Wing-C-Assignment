"""API + Postgres 통합 — task_db_test 에서만 (`uv run pytest -m pg`)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from task_spec.api.app import create_app
from task_spec.graph import build_graph, make_checkpointer
from tests.api.conftest import FAKE_FILE, make_settings, reviewer_decision, wait_status
from tests.graph.test_postgres import test_db_url  # noqa: F401  (픽스처 재사용)

pytestmark = pytest.mark.pg


async def test_create_approve_done_on_postgres(test_db_url: str, tmp_path: Path) -> None:  # noqa: F811
    async with make_checkpointer(kind="postgres", conn_string=test_db_url) as cp:
        app = create_app(settings=make_settings(tmp_path), graph=build_graph(cp))
        run_id = ""
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as client:
                resp = await client.post(
                    "/runs", data={"posting_id": "posting-fake-001"}, files=[("resumes", FAKE_FILE)]
                )
                assert resp.status_code == 202
                run_id = resp.json()["run_id"]
                await wait_status(client, run_id, "awaiting_approval")
                resp = await client.post(
                    f"/runs/{run_id}/approve", json=reviewer_decision("approved")
                )
                assert resp.status_code == 202
                await wait_status(client, run_id, "done")
                assert (await client.get(f"/runs/{run_id}/spec")).status_code == 200
                health = (await client.get("/healthz")).json()
                assert health["database"] == "ok"
        finally:
            await app.state.launcher.shutdown()
            if run_id:
                await cp.adelete_thread(run_id)
