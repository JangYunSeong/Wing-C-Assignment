"""빌드·전달 묶음 조회 API — 메모리 저장소. 응답에 계약·숨은 계획·오버레이 본문이 없는지 본다. 가상 데이터만."""

from __future__ import annotations

from pathlib import Path

import httpx

from task_spec.api.app import create_app
from task_spec.schemas.build_record import StarterFile
from task_spec.schemas.package import flag_id
from task_spec.starter.store import InMemoryBuildStore, TaskLabel
from tests.api.conftest import make_settings
from tests.starter.test_build_store import AT, FLAG, H_OLD, _artifact, _outcome

CID = "ct-0123456789ab"
SECRET = "가상 참조 구현 본문 SECRET-REF"


async def _client(tmp_path: Path, store: InMemoryBuildStore | None) -> httpx.AsyncClient:
    app = create_app(settings=make_settings(tmp_path))
    if store is not None:
        app.state.builds = store
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _review_build(store: InMemoryBuildStore) -> str:
    b = await store.new_build(CID, "onto-test", AT)
    await store.set_status(b.build_id, "generating")
    await store.put_files(
        b.build_id,
        [
            StarterFile.of(b.build_id, "starter", "src/App.java", "class App {}"),
            StarterFile.of(b.build_id, "ref_accepted", "src/Svc.java", SECRET, variant="d1"),
        ],
    )
    await store.save_outcome(b.build_id, _outcome(H_OLD))
    await store.set_status(b.build_id, "verifying")
    await store.set_status(b.build_id, "review")
    for role in ("candidate", "grader"):
        await store.put_artifact(_artifact(b.build_id, role, H_OLD))  # type: ignore[arg-type]
    return b.build_id


async def test_build_views_hide_contract_plan_and_file_bodies(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _review_build(store)
    async with await _client(tmp_path, store) as c:
        builds = (await c.get("/builds", params={"common_id": CID})).json()
        assert [b["build_id"] for b in builds] == [bid]
        r = await c.get(f"/builds/{bid}")
        body = r.json()
        assert r.status_code == 200 and body["status"] == "review"
        assert body["files"] == {"starter": 1, "ref_accepted": 1}
        assert body["review_flags"] == [{"flag_id": flag_id(FLAG), "text": FLAG, "verdict": None}]
        assert body["verification"]["starter-only"]["hidden_total"] == 1
        assert not {"contract", "hidden_plan", "outcome"} & body.keys()
        arts = (await c.get(f"/builds/{bid}/artifacts")).json()
        assert [a["role"] for a in arts] == ["candidate", "grader"]
        assert (await c.get(f"/builds/{bid}/deliveries")).json() == []
        everything = r.text + str(arts) + str(builds)
        assert SECRET not in everything and "class App" not in everything


async def test_recent_builds_without_common_id_have_titles(tmp_path: Path) -> None:
    """화면 목록용 — common_id 없이 최근 빌드, 공통 과제 제목·공고 id·템플릿 (명세 본문은 없음)."""
    store = InMemoryBuildStore()
    bid = await _review_build(store)
    other = await store.new_build("ct-ba9876543210", "onto-test", AT)
    store.labels[CID] = TaskLabel(title="가상 과제 제목", posting_id="가상_공고#001")
    async with await _client(tmp_path, store) as c:
        rows = (await c.get("/builds")).json()
        assert {r["build_id"] for r in rows} == {bid, other.build_id}
        mine = next(r for r in rows if r["build_id"] == bid)
        assert (mine["title"], mine["posting_id"]) == ("가상 과제 제목", "가상_공고#001")
        assert mine["template_ref"] == "data/python-duckdb@v1"
        assert next(r for r in rows if r["build_id"] == other.build_id)["title"] is None
        assert len((await c.get("/builds", params={"limit": 1})).json()) == 1
        assert (await c.get("/builds", params={"limit": 0})).status_code == 422
        detail = (await c.get(f"/builds/{bid}")).json()
        assert (
            detail["title"] == "가상 과제 제목"
            and detail["template_ref"] == "data/python-duckdb@v1"
        )


async def test_missing_and_malformed_ids(tmp_path: Path) -> None:
    async with await _client(tmp_path, InMemoryBuildStore()) as c:
        r = await c.get(f"/builds/{CID}-b9")
        assert r.status_code == 404 and r.json()["error"]["code"] == "build_not_found"
        assert (await c.get(f"/builds/{CID}-b9/artifacts")).status_code == 404
        assert (await c.get("/builds/../etc")).status_code in (404, 422)
        assert (await c.get("/builds/ct-xyz-b1")).status_code == 422
        assert (await c.get("/builds", params={"common_id": "eval-x"})).status_code == 422


async def test_delivery_targets_register_once_per_role(tmp_path: Path) -> None:
    async with await _client(tmp_path, InMemoryBuildStore()) as c:
        r = await c.post("/delivery-targets", json={"name": "guild-sftp", "role": "candidate"})
        assert r.status_code == 201 and r.json()["role"] == "candidate"
        again = await c.post("/delivery-targets", json={"name": "guild-sftp", "role": "candidate"})
        assert again.status_code == 201  # 같은 역할로 다시 등록은 그대로
        clash = await c.post("/delivery-targets", json={"name": "guild-sftp", "role": "grader"})
        assert clash.status_code == 409 and clash.json()["error"]["code"] == "build_rule_violation"
        bad = await c.post("/delivery-targets", json={"name": "sftp://u:p@host", "role": "grader"})
        assert bad.status_code == 422
        assert [t["name"] for t in (await c.get("/delivery-targets")).json()] == ["guild-sftp"]


async def test_no_store_is_503(tmp_path: Path) -> None:
    async with await _client(tmp_path, None) as c:
        r = await c.get("/delivery-targets")
        assert r.status_code == 503 and r.json()["error"]["code"] == "service_unavailable"
