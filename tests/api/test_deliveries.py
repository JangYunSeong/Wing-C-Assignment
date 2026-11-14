"""SFTP 전달 요청 API (#116 2단계) — 메모리 저장소 + 가짜 전송 함수. 가상 데이터만."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from task_spec.api.app import create_app
from task_spec.delivery.sender import DeliverySender
from task_spec.delivery.sftp import SendResult
from task_spec.delivery.target import SftpTarget
from task_spec.schemas.build_record import README_PATH, StarterFile
from task_spec.schemas.package import ArtifactFile, ReviewDecision
from task_spec.starter.store import InMemoryBuildStore, TaskLabel
from tests.api.conftest import make_settings
from tests.starter.test_build_store import AT, FLAG, H_NEW, _artifact, _outcome, finalize

CID = "ct-0123456789ab"
TARGET = "grader-test"
FAKE_TARGET = SftpTarget(
    host=SecretStr("sftp.invalid"),
    user="grader-fake",
    key_path=Path("/nonexistent/key"),
    known_hosts=Path("/nonexistent/known_hosts"),
    remote_dir="/inbox",
)


class FakeSend:
    def __init__(self, *results: SendResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[Path, list[str], str]] = []

    async def __call__(
        self,
        target: SftpTarget,
        local: Path,
        files: Sequence[ArtifactFile],
        sub: str,
        timeout_s: float,
    ) -> SendResult:
        self.calls.append((local, [f.name for f in files], sub))
        return self.results.pop(0)


async def _approved_build(store: InMemoryBuildStore, revision: int | None = 3) -> str:
    bid = (await store.new_build(CID, "onto-test", AT)).build_id
    await store.set_status(bid, "generating")
    await store.set_status(bid, "verifying")
    await store.save_outcome(bid, _outcome(H_NEW))
    await store.set_status(bid, "review")
    readme = StarterFile.of(bid, "readme", README_PATH, "# 가상 과제\n")
    await store.backfill_readme(readme)
    await store.put_artifact(_artifact(bid, "candidate", H_NEW, readme))
    await store.put_artifact(_artifact(bid, "grader", H_NEW))
    ok = ReviewDecision.of(FLAG, "accepted_risk", "reviewer-fake", note="가상 메모")
    await store.approve(bid, "reviewer-fake", AT, [ok])
    await finalize(store, bid, H_NEW)
    await store.add_target(TARGET, "grader")
    store.labels[CID] = TaskLabel(title="가상 과제", posting_id="가상_공고#001", revision=revision)
    return bid


def _client(
    tmp_path: Path, store: InMemoryBuildStore | None, send: FakeSend | None
) -> httpx.AsyncClient:
    app = create_app(settings=make_settings(tmp_path))
    if store is not None:
        app.state.builds = store
        if send is not None:
            app.state.delivery = DeliverySender(
                store, tmp_path / "images", {TARGET: FAKE_TARGET}, 30.0, send=send
            )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(**kw: Any) -> dict[str, Any]:
    return {"role": "grader", "target": TARGET, "requested_by": "operator-fake", **kw}


async def _settled(c: httpx.AsyncClient, bid: str) -> list[dict[str, Any]]:
    async with asyncio.timeout(5):
        while True:
            rows: list[dict[str, Any]] = (await c.get(f"/builds/{bid}/deliveries")).json()
            if all(r["status"] != "requested" for r in rows):
                return rows
            await asyncio.sleep(0.01)


async def test_request_sends_in_background_then_published(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _approved_build(store)
    send = FakeSend(SendResult(ok=True, bytes_sent=10, attempts=1))
    async with _client(tmp_path, store, send) as c:
        r = await c.post(f"/builds/{bid}/deliveries", json=_body())
        assert r.status_code == 202 and r.json()["status"] == "requested"
        rows = await _settled(c, bid)
        assert [(x["status"], x["error"]) for x in rows] == [("sent", None)]
        assert (await c.get(f"/builds/{bid}")).json()["status"] == "published"
    local, names, sub = send.calls[0]
    assert local == tmp_path / "images" / bid / "grader"
    assert sub == f"{CID}-r3/grader"  # <common_id>-r<판>/<role>
    assert names == [f.name for f in (await store.deliveries(bid))[0].files]


async def test_resend_is_409_but_failed_can_be_retried(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _approved_build(store)
    send = FakeSend(
        SendResult(ok=False, attempts=1, error="host_key"),
        SendResult(ok=True, bytes_sent=10, attempts=1),
    )
    async with _client(tmp_path, store, send) as c:
        assert (await c.post(f"/builds/{bid}/deliveries", json=_body())).status_code == 202
        rows = await _settled(c, bid)
        assert [(x["status"], x["error"]) for x in rows] == [("failed", "host_key")]
        assert (await c.get(f"/builds/{bid}")).json()["status"] == "approved"
        assert (await c.post(f"/builds/{bid}/deliveries", json=_body())).status_code == 202
        assert [x["status"] for x in await _settled(c, bid)] == ["failed", "sent"]
        again = await c.post(f"/builds/{bid}/deliveries", json=_body())
        assert again.status_code == 409 and "이미 보냈다" in again.json()["error"]["message"]
    assert len(send.calls) == 2


async def test_target_without_settings_is_409(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _approved_build(store)
    await store.add_target("grader-other", "grader")  # DB 에는 있지만 API 설정에 접속 정보 없음
    async with _client(tmp_path, store, FakeSend()) as c:
        r = await c.post(f"/builds/{bid}/deliveries", json=_body(target="grader-other"))
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "delivery_target_not_configured"
        assert (await c.get(f"/builds/{bid}/deliveries")).json() == []


async def test_rules_are_409_and_nothing_is_sent(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _approved_build(store)
    send = FakeSend()
    async with _client(tmp_path, store, send) as c:
        wrong_role = await c.post(f"/builds/{bid}/deliveries", json=_body(role="candidate"))
        assert wrong_role.status_code == 409
        assert wrong_role.json()["error"]["code"] == "build_rule_violation"
        review = (await store.new_build(CID, "onto-test", AT)).build_id
        assert (await c.post(f"/builds/{review}/deliveries", json=_body())).status_code == 409
        missing = await c.post(f"/builds/{CID}-b9/deliveries", json=_body())
        assert missing.status_code == 404
        bad = await c.post(f"/builds/{bid}/deliveries", json=_body(target="sftp://u:p@h"))
        assert bad.status_code == 422
    assert send.calls == []


async def test_unknown_revision_is_409(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _approved_build(store, revision=None)
    async with _client(tmp_path, store, FakeSend()) as c:
        r = await c.post(f"/builds/{bid}/deliveries", json=_body())
        assert r.status_code == 409 and "판" in r.json()["error"]["message"]


async def test_no_sender_is_503(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _approved_build(store)
    async with _client(tmp_path, store, None) as c:
        r = await c.post(f"/builds/{bid}/deliveries", json=_body())
        assert r.status_code == 503


async def test_crash_in_sender_marks_failed_internal(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _approved_build(store)

    async def boom(*_: Any) -> SendResult:
        raise RuntimeError("가상 오류 /secret/path")

    sender = DeliverySender(store, tmp_path, {TARGET: FAKE_TARGET}, 30.0, send=boom)
    d = await store.request_delivery(bid, "grader", TARGET, "operator-fake")
    sender.launch(d, CID, 3)
    async with asyncio.timeout(5):
        while True:
            [row] = await store.deliveries(bid)
            if row.status != "requested":
                break
            await asyncio.sleep(0.01)
    assert (row.status, row.error) == ("failed", "internal")


async def test_recover_fails_leftover_requested(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _approved_build(store)
    await store.request_delivery(bid, "grader", TARGET, "operator-fake")
    sender = DeliverySender(store, tmp_path, {TARGET: FAKE_TARGET}, 30.0, send=FakeSend())
    assert await sender.recover() == 1
    [row] = await store.deliveries(bid)
    assert (row.status, row.error) == ("failed", "interrupted")
