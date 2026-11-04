"""공고 찾기·공고 프로필 API — 온톨로지·저장소가 없을 때 (실제 검색은 tests/postings/test_postings_pg). 가상 데이터만."""

from __future__ import annotations

from urllib.parse import quote

from tests.api.conftest import AppFactory


async def test_search_and_profile_without_ontology(make_client: AppFactory) -> None:
    client = await make_client()  # 그래프만 주입 — 온톨로지·공고 저장소 없음
    r = await client.get("/postings", params={"q": "네이버"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "service_unavailable"
    assert (await client.get(f"/postings/{quote('naver_2026#001', safe='')}")).status_code == 404
    assert (await client.get("/postings/prov-000000000000")).status_code == 503
    assert (await client.get("/postings", params={"limit": 0})).status_code == 422
