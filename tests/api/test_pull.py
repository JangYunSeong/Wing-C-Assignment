"""받는 쪽이 가져가는 전달 (/pull) — 토큰·역할 분리·승인·확정된 빌드만·SHA256SUMS 는 DB 기록으로·done 한 번. 가상 데이터만."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from task_spec.api.app import create_app
from task_spec.starter.store import InMemoryBuildStore, sha256sums
from tests.api.conftest import make_settings
from tests.api.test_deliveries import CID, TARGET, _approved_build
from tests.starter.test_build_store import AT

GRADER_TOKEN = "g" * 40
KIT_TOKEN = "k" * 40
KIT = "kit-test"


def _client(tmp_path: Path, store: InMemoryBuildStore) -> httpx.AsyncClient:
    app = create_app(
        settings=make_settings(
            tmp_path,
            images_dir=tmp_path / "images",
            pull_tokens={TARGET: SecretStr(GRADER_TOKEN), KIT: SecretStr(KIT_TOKEN)},
        )
    )
    app.state.builds = store
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _with_files(tmp_path: Path, store: InMemoryBuildStore) -> str:
    """승인·확정된 빌드 + 채점 묶음 파일을 디스크에 (기록된 크기대로)."""
    bid = await _approved_build(store)
    await store.add_target(KIT, "eval_kit")
    d = tmp_path / "images" / bid / "grader"
    d.mkdir(parents=True)
    for f in next(a for a in await store.artifacts(bid) if a.role == "grader").files:
        (d / f.name).write_bytes(b"x" * f.size_bytes)
    return bid


async def test_token_required_and_mapped_to_db_target(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    await _with_files(tmp_path, store)
    async with _client(tmp_path, store) as c:
        assert (await c.get("/pull/builds")).status_code == 401
        assert (await c.get("/pull/builds", headers=_auth("z" * 40))).status_code == 401
        r = await c.get("/pull/builds", headers={"Authorization": f"Basic {GRADER_TOKEN}"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "pull_unauthorized"
    # 설정에만 있고 DB 에 등록되지 않은 이름은 거부
    store2 = InMemoryBuildStore()
    async with _client(tmp_path, store2) as c:
        assert (await c.get("/pull/builds", headers=_auth(GRADER_TOKEN))).status_code == 401


async def test_list_download_and_done_records_published(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _with_files(tmp_path, store)
    files = next(a for a in await store.artifacts(bid) if a.role == "grader").files
    async with _client(tmp_path, store) as c:
        rows = (await c.get("/pull/builds", headers=_auth(GRADER_TOKEN))).json()
        assert [(r["build_id"], r["role"], r["delivered"]) for r in rows] == [
            (bid, "grader", False)
        ]
        sums = await c.get(f"/pull/builds/{bid}/files/SHA256SUMS", headers=_auth(GRADER_TOKEN))
        assert sums.text == sha256sums(files)  # 디스크가 아니라 DB 기록
        h = hashlib.sha256(sums.content).hexdigest()
        assert rows[0]["sha256sums_sha256"] == h
        f = files[0]
        got = await c.get(f"/pull/builds/{bid}/files/{f.name}", headers=_auth(GRADER_TOKEN))
        assert got.status_code == 200 and len(got.content) == f.size_bytes
        # 다른 해시로 done → 거부, 맞는 해시 → sent·published, 다시 done 은 같은 기록
        bad = await c.post(
            f"/pull/builds/{bid}/done",
            json={"sha256sums_sha256": "0" * 64},
            headers=_auth(GRADER_TOKEN),
        )
        assert bad.status_code == 409
        ok = await c.post(
            f"/pull/builds/{bid}/done", json={"sha256sums_sha256": h}, headers=_auth(GRADER_TOKEN)
        )
        assert ok.status_code == 200 and ok.json()["status"] == "sent"
        assert ok.json()["requested_by"] == f"pull:{TARGET}"
        again = await c.post(
            f"/pull/builds/{bid}/done", json={"sha256sums_sha256": h}, headers=_auth(GRADER_TOKEN)
        )
        assert again.json()["delivery_id"] == ok.json()["delivery_id"]
        assert (await c.get(f"/builds/{bid}")).json()["status"] == "published"
        rows = (await c.get("/pull/builds", headers=_auth(GRADER_TOKEN))).json()
        assert rows[0]["delivered"] is True  # 다시 내려받기는 막지 않는다
        got = await c.get(f"/pull/builds/{bid}/files/{f.name}", headers=_auth(GRADER_TOKEN))
        assert got.status_code == 200


async def test_an_older_approved_build_stays_listed_past_many_newer_builds(tmp_path: Path) -> None:
    """#235: 목록이 최근 빌드 200개(recent(200))에서 승인·확정 빌드를 걸러, 더 새 빌드(실패·eval 적재 포함)가 200개 이상이면
    승인·확정 빌드가 받는 쪽 목록에서 조용히 빠졌다 — id 로 직접 받기는 됐다. 받는 쪽은 알림 없이 이 목록만 본다 (deploy.md 2-12)."""
    store = InMemoryBuildStore()
    bid = await _with_files(tmp_path, store)
    for i in range(200):
        await store.new_build(CID, "onto-test", AT + timedelta(minutes=1 + i))
    async with _client(tmp_path, store) as c:
        rows = (await c.get("/pull/builds", headers=_auth(GRADER_TOKEN))).json()
    assert [(r["build_id"], r["role"]) for r in rows] == [(bid, "grader")]


async def test_role_is_separated_and_unknown_or_unapproved_is_404(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _with_files(tmp_path, store)
    grader_file = next(a for a in await store.artifacts(bid) if a.role == "grader").files[-1]
    async with _client(tmp_path, store) as c:
        # 평가 키트 토큰은 채점 파일을 못 받는다 (그 역할 묶음의 이름만)
        kit = (await c.get("/pull/builds", headers=_auth(KIT_TOKEN))).json()
        assert [r["role"] for r in kit] == ["eval_kit"]
        r = await c.get(f"/pull/builds/{bid}/files/{grader_file.name}", headers=_auth(KIT_TOKEN))
        assert r.status_code == 404 and r.json()["error"]["code"] == "pull_not_available"
        # 경로·없는 이름·없는 빌드
        assert (
            await c.get(f"/pull/builds/{bid}/files/..%2Fcandidate", headers=_auth(GRADER_TOKEN))
        ).status_code in (404, 422)
        assert (
            await c.get(f"/pull/builds/{bid}/files/nope.bin", headers=_auth(GRADER_TOKEN))
        ).status_code == 404
        assert (
            await c.get(f"/pull/builds/{CID}-b9/files/SHA256SUMS", headers=_auth(GRADER_TOKEN))
        ).status_code == 404
    # 승인 전 빌드는 목록에도 없고 내려받기도 404
    store2 = InMemoryBuildStore()
    b = (await store2.new_build(CID, "onto-test", AT)).build_id
    await store2.add_target(TARGET, "grader")
    async with _client(tmp_path, store2) as c:
        assert (await c.get("/pull/builds", headers=_auth(GRADER_TOKEN))).json() == []
        r = await c.get(f"/pull/builds/{b}/files/SHA256SUMS", headers=_auth(GRADER_TOKEN))
        assert r.status_code == 404


async def test_file_changed_on_disk_is_not_served(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _with_files(tmp_path, store)
    f = next(a for a in await store.artifacts(bid) if a.role == "grader").files[0]
    (tmp_path / "images" / bid / "grader" / f.name).write_bytes(b"x" * (f.size_bytes + 5))
    async with _client(tmp_path, store) as c:
        r = await c.get(f"/pull/builds/{bid}/files/{f.name}", headers=_auth(GRADER_TOKEN))
        assert r.status_code == 404


def test_short_token_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, pull_tokens={"grader-test": SecretStr("short")})
