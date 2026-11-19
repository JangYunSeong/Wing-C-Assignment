"""회사 결정적 사건 API [확정: 2026-11-19 사용자] — 등록·조회·공유 승인(resume 만)·보관. ASGI 앱 + 메모리 저장소, 가상 데이터만."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from task_spec.api.app import create_app
from task_spec.graph import build_graph, make_checkpointer
from task_spec.graph.incident_share import incident_share_graph
from task_spec.spec.company_incidents import InMemoryCompanyIncidents
from task_spec.spec.store import InMemoryCommonStore
from tests.api.conftest import make_settings
from tests.spec.test_company_incidents import _public, _used_by

NOW = datetime(2026, 11, 19, tzinfo=UTC).isoformat()


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, InMemoryCommonStore]]:
    store, common = InMemoryCompanyIncidents(), InMemoryCommonStore()
    async with make_checkpointer(kind="memory") as cp:
        app = create_app(settings=make_settings(tmp_path), graph=build_graph(cp))
        app.state.company_incidents = store
        app.state.common_store = common
        app.state.incident_share = incident_share_graph(store, cp)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, common
        await app.state.launcher.shutdown()


def _body(**over: Any) -> dict[str, Any]:
    inc = _public()
    body = {
        "company": "(주)가상회사",
        "sub": "sw.backend",
        "title": "가상 회사 사례",
        "symptom": inc.symptom,
        "naive_fix": inc.naive_fix,
        "root_cause": inc.root_cause,
        "why_naive_fails": inc.why_naive_fails,
        "key_insight": inc.key_insight,
        "deep_structure": inc.deep_structure,
        "surface": inc.surface,
        "accepted_fixes": inc.accepted_fixes,
        "submitted_by": "담당자-가상",
    }
    body.update(over)
    return body


async def _submit(client: httpx.AsyncClient, **over: Any) -> dict[str, Any]:
    resp = await client.post("/incidents", json=_body(**over))
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def test_submit_list_share_and_retire(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],
) -> None:
    client, common = api
    rec = await _submit(client)
    iid = rec["incident_id"]
    assert iid.startswith("co:sw.backend:") and rec["status"] == "pending"
    assert rec["incident"]["org"] == "" and rec["source"] == "manual"
    await _submit(client, company="다른회사")
    listed = (await client.get("/incidents", params={"company": "가상회사"})).json()
    assert [r["incident_id"] for r in listed] == [iid]  # 표기를 같은 규칙으로 맞춰 고른다

    view = (await client.get(f"/incidents/{iid}/share")).json()
    assert view["state"] == "awaiting" and view["status"] == "pending"
    resp = await client.post(
        f"/incidents/{iid}/share",
        json={"status": "approved", "reviewer": "승인자-가상", "at": NOW},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "incident_id": iid,
        "status": "shared",
        "state": "closed",
        "flags": [],
        "error": None,
    }
    again = await client.post(
        f"/incidents/{iid}/share",
        json={"status": "approved", "reviewer": "승인자-가상", "at": NOW},
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "incident_not_pending"

    await _used_by(common, "가상공고-1", iid)
    detail = (await client.get(f"/incidents/{iid}")).json()
    assert (detail["status"], detail["used_by"]) == ("shared", 1)

    retire = {"reviewer": "승인자-가상", "reason": "밖으로 샘", "at": NOW}
    resp = await client.post(f"/incidents/{iid}/retire", json=retire)
    assert resp.status_code == 200 and resp.json()["status"] == "retired"
    resp = await client.post(f"/incidents/{iid}/retire", json=retire)
    assert resp.status_code == 409


async def test_contact_or_link_in_text_is_not_registered(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],
) -> None:
    client, _ = api
    resp = await client.post(
        "/incidents", json=_body(symptom="장애 알림은 oncall@example.com 으로 왔다")
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "incident_content_blocked" and "symptom" in err["message"]
    assert "oncall@example.com" not in err["message"]  # 값은 돌려주지 않는다
    assert (await client.get("/incidents")).json() == []


async def test_reject_without_reason_is_422_and_unknown_is_404(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],
) -> None:
    client, _ = api
    iid = (await _submit(client))["incident_id"]
    resp = await client.post(
        f"/incidents/{iid}/share",
        json={"status": "rejected", "reviewer": "승인자-가상", "at": NOW},
    )
    assert resp.status_code == 422
    assert (await client.get(f"/incidents/{iid}")).json()["status"] == "pending"
    assert (await client.get("/incidents/co:sw.backend:000000000000")).status_code == 404
    bad = await client.post("/incidents", json=_body(company="(주)"))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "incident_invalid"


async def test_decision_not_applied_when_retired_meanwhile_is_409(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],
) -> None:
    """공유 승인을 기다리는 사이 보관되면 승인 요청은 적용되지 않는다 — 200 으로 돌려주면 승인된 줄 안다 (리뷰 제안)."""
    client, _ = api
    iid = (await _submit(client))["incident_id"]
    assert (await client.get(f"/incidents/{iid}/share")).json()["state"] == "awaiting"
    retire = {"reviewer": "승인자-가상", "reason": "등록 취소", "at": NOW}
    assert (await client.post(f"/incidents/{iid}/retire", json=retire)).status_code == 200
    resp = await client.post(
        f"/incidents/{iid}/share",
        json={"status": "approved", "reviewer": "승인자-가상", "at": NOW},
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "incident_not_pending"
    assert (await client.get(f"/incidents/{iid}")).json()["status"] == "retired"
