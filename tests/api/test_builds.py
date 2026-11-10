"""빌드 API — 지원자 README 조회. 메모리 빌드 저장소, 가상 데이터만."""

from __future__ import annotations

from datetime import UTC, datetime

from task_spec.schemas.build_record import README_PATH, StarterFile
from task_spec.starter.store import InMemoryBuildStore
from tests.api.conftest import AppFactory
from tests.starter.test_build_store import H_NEW, _outcome

AT = datetime(2026, 11, 10, tzinfo=UTC)
README_FLAG = (
    f"{README_PATH} 에 숨은 요구·운영 조건을 드러내는 말: 가상어 — 승인된 명세 문장에서 나옴"
)


async def test_readme(make_client: AppFactory) -> None:
    client = await make_client()
    r = await client.get("/builds/ct-0123456789ab-b1/readme")
    assert r.status_code == 503  # 저장소 미연결

    store = InMemoryBuildStore()
    client._transport.app.state.builds = store  # type: ignore[attr-defined]
    assert (await client.get("/builds/ct-0123456789ab-b1/readme")).status_code == 404

    bid = (await store.new_build("ct-0123456789ab", "onto-test", AT)).build_id
    await store.set_status(bid, "generating")
    readme = StarterFile.of(bid, "readme", README_PATH, "# 가상 과제\n")
    await store.put_files(
        bid, [readme, StarterFile.of(bid, "hidden", "T.java", "// HIDDEN-BODY-MARKER")]
    )
    r = await client.get(f"/builds/{bid}/readme")
    assert r.status_code == 404 and r.json()["error"]["code"] == "readme_not_available"  # 검증 전

    await store.set_status(bid, "verifying")
    outcome = _outcome(H_NEW).model_copy(
        update={"review_flags": ["가상 다른 검토 표시", README_FLAG]}
    )
    await store.save_outcome(bid, outcome)
    await store.set_status(bid, "review")
    r = await client.get(f"/builds/{bid}/readme")
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "# 가상 과제\n" and body["sha256"] == readme.sha256
    assert body["review_flags"] == [README_FLAG]  # README 표시만
    assert "HIDDEN-BODY-MARKER" not in r.text  # 다른 파일 본문은 내주지 않는다


async def test_readme_missing(make_client: AppFactory) -> None:
    client = await make_client()
    store = InMemoryBuildStore()
    client._transport.app.state.builds = store  # type: ignore[attr-defined]
    bid = (await store.new_build("ct-0123456789ab", "onto-test", AT)).build_id
    for s in ("generating", "verifying"):
        await store.set_status(bid, s)  # type: ignore[arg-type]
    await store.save_outcome(bid, _outcome(H_NEW))
    await store.set_status(bid, "review")
    r = await client.get(f"/builds/{bid}/readme")
    assert r.status_code == 404 and "저장한 README 없음" in r.json()["error"]["message"]
