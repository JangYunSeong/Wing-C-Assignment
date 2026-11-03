"""API 시나리오 (httpx + ASGITransport, memory 체크포인터, 가상 데이터)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from tests.api.conftest import (
    FAKE_FILE,
    FAKE_OPENAI_KEY,
    FAKE_OPENCODE_KEY,
    AppFactory,
    reviewer_decision,
    wait_status,
)
from tests.conftest import failing_pseudonymizer


async def _create(client: httpx.AsyncClient, **extra: object) -> str:
    resp = await client.post(
        "/runs",
        data={"posting_id": "posting-fake-001"},
        files=[("resumes", FAKE_FILE), ("cover_letters", ("cl.txt", b"fake", "text/plain"))],
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "running"
    # USE_COVER_LETTERS=false (기본): 자기소개서는 받되 저장·처리하지 않고 알린다 [확정: 2026-11-03]
    assert body["ignored"] == ["cover_letter#1"]
    return str(body["run_id"])


async def test_create_approve_done_and_spec(client: httpx.AsyncClient, tmp_path: Path) -> None:
    run_id = await _create(client)
    saved = sorted(p.name for p in (tmp_path / "raw" / run_id).iterdir())
    assert saved == ["resume-1.txt"]  # 자기소개서는 저장하지 않음

    awaiting = await wait_status(client, run_id, "awaiting_approval")
    assert awaiting["spec"]["revision"] == 1
    assert awaiting["spec"]["ontology_version"] == "onto-v2026.11.02"
    assert awaiting["current_node"] == "approval"
    assert awaiting["approval_summary"]["total_time_min"] >= 0  # 승인 요약이 함께 온다
    assert (await client.get(f"/runs/{run_id}/spec")).status_code == 404  # 초안은 확정본 아님

    resp = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 202
    done = await wait_status(client, run_id, "done")
    assert done["spec"] is None

    spec = await client.get(f"/runs/{run_id}/spec")
    assert spec.status_code == 200
    assert spec.json()["approval"]["status"] == "approved"
    assert spec.json()["approval"]["reviewer"] == "reviewer-fake"


async def test_revise_then_rejected(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")

    resp = await client.post(
        f"/runs/{run_id}/approve", json=reviewer_decision("revise", "범위를 좁혀 주세요")
    )
    assert resp.status_code == 202
    # 재개 직후 스냅샷이 아직 옛 interrupt 일 수 있으므로 revision 으로 구분한다.
    async with asyncio.timeout(5):
        while True:
            body = await wait_status(client, run_id, "awaiting_approval")
            if body["spec"]["revision"] == 2:
                break
            await asyncio.sleep(0.02)

    await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("rejected"))
    rejected = await wait_status(client, run_id, "rejected")
    assert rejected["revision"] == 2
    assert (await client.get(f"/runs/{run_id}/spec")).status_code == 404


async def test_run_without_documents_still_needs_approval(client: httpx.AsyncClient) -> None:
    resp = await client.post("/runs", data={"posting_id": "posting-fake-001"})
    assert resp.status_code == 202
    body = await wait_status(client, resp.json()["run_id"], "awaiting_approval")
    assert body["spec"]["revision"] == 1


async def test_approve_when_not_pending_is_409(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")
    await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    await wait_status(client, run_id, "done")

    resp = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "approval_not_pending"
    assert resp.json()["error"]["run_id"] == run_id


async def test_unknown_run_is_404(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("get", "/runs/nope"),
        ("get", "/runs/nope/spec"),
        ("get", "/runs/nope/events"),
    ]:
        resp = await getattr(client, method)(path)
        assert resp.status_code == 404, path
        assert resp.json()["error"]["code"] == "run_not_found"
    resp = await client.post("/runs/nope/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 404


async def test_invalid_decision_is_422_and_does_not_resume(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")
    bad = [
        {**reviewer_decision("revise"), "comment": None},  # revise 는 comment 필수
        {**reviewer_decision("approved"), "reviewer": ""},
        {**reviewer_decision("maybe")},
    ]
    for body in bad:
        resp = await client.post(f"/runs/{run_id}/approve", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "awaiting_approval"


async def test_disallowed_extension_rejected(client: httpx.AsyncClient, tmp_path: Path) -> None:
    resp = await client.post(
        "/runs",
        data={"posting_id": "p"},
        files=[("resumes", ("evil.exe", b"fake", "application/octet-stream"))],
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_file_type"
    assert not (tmp_path / "raw").exists()


async def test_oversize_file_rejected(client: httpx.AsyncClient, tmp_path: Path) -> None:
    resp = await client.post(
        "/runs",
        data={"posting_id": "p"},
        files=[("resumes", ("big.txt", b"x" * 2048, "text/plain"))],  # 상한 1024
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"
    assert not (tmp_path / "raw").exists()


async def test_missing_posting_id_is_422(client: httpx.AsyncClient) -> None:
    resp = await client.post("/runs", files=[("resumes", FAKE_FILE)])
    assert resp.status_code == 422


async def test_residual_pii_run_is_failed(make_client: AppFactory) -> None:
    client = await make_client(pseudonymizer=failing_pseudonymizer)
    run_id = await _create(client)
    body = await wait_status(client, run_id, "failed")
    assert [e["code"] for e in body["errors"]] == ["residual_pii"]  # 이력서 1건만 처리
    assert body["spec"] is None
    assert (await client.get(f"/runs/{run_id}/spec")).status_code == 404
    resp = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 409


async def test_healthz_exposes_no_key(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ontology_version"] == "onto-v2026.11.02"
    assert body["llm_keys"] == {"openai": True, "opencode": True}
    assert FAKE_OPENAI_KEY not in resp.text
    assert FAKE_OPENCODE_KEY not in resp.text


async def test_responses_do_not_echo_upload_content(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    body = await wait_status(client, run_id, "awaiting_approval")
    assert "가상 데이터" not in json.dumps(body, ensure_ascii=False)


async def test_sse_streams_until_terminal(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")

    async def consume() -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        async with client.stream("GET", f"/runs/{run_id}/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line.removeprefix("data:").strip()))
        return events

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    async with asyncio.timeout(5):
        events = await task
    statuses = [e["status"] for e in events]
    assert statuses[0] == "awaiting_approval"
    assert statuses[-1] == "done"
