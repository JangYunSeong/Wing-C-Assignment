"""공고 신입·경력 API — PUT /postings/{id}/career-level·공고 프로필 (postings/career.py). 가상 데이터만."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote

from tests.api.conftest import AppFactory, FakeOntology, FakeProvisional


async def test_put_career_level_then_shown_and_cleared(make_client: AppFactory) -> None:
    client = await make_client()
    app = client._transport.app  # type: ignore[attr-defined]
    pid = "가상_공고#001"
    app.state.ontology = FakeOntology(
        {pid: "가상회사"}, ["가상회사"], careers={pid: ["가상 직무", "경력 2년 이상"]}
    )
    app.state.postings = SimpleNamespace(store=FakeProvisional({"prov-fake0001": "가상회사"}))
    url = f"/postings/{quote(pid, safe='')}/career-level"

    r = await client.put(url, json={"career_level": "신입", "set_by": "reviewer-fake"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "posting_id": pid,
        "career_level": "신입",
        "career_level_source": "confirmed",
    }
    # 지우면 다시 공고 문구 추정
    r = await client.put(url, json={"career_level": None, "set_by": "reviewer-fake"})
    assert r.json() == {
        "posting_id": pid,
        "career_level": "경력",
        "career_level_source": "posting_text",
    }

    prov = await client.put(
        "/postings/prov-fake0001/career-level",
        json={"career_level": "경력", "set_by": "reviewer-fake"},
    )
    assert prov.json()["career_level_source"] == "confirmed"

    for bad_url, body in [
        ("/postings/prov-fake9999/career-level", {"career_level": "신입", "set_by": "r"}),
        (
            f"/postings/{quote('없는_공고#001', safe='')}/career-level",
            {"career_level": "신입", "set_by": "r"},
        ),
    ]:
        resp = await client.put(bad_url, json=body)
        assert resp.status_code == 404 and resp.json()["error"]["code"] == "posting_not_found"
    for body in [
        {"career_level": "무관", "set_by": "r"},
        {"career_level": "신입", "set_by": ""},
        {"set_by": "r"},
    ]:
        assert (await client.put(url, json=body)).status_code == 422


async def test_put_career_level_without_posting_store(make_client: AppFactory) -> None:
    client = await make_client()  # 공고 저장소 없음
    r = await client.put(
        "/postings/prov-fake0001/career-level", json={"career_level": "신입", "set_by": "r"}
    )
    assert r.status_code == 503
