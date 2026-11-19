"""공고의 회사 정하기 [확정: 2026-11-19 사용자 — 인증 전에는 입력한 회사] — PUT /postings/{id}/company·실행 만들기의 company.

회사 결정적 사건은 공고의 회사(사람이 정한 값이 먼저)의 공유 사례에서 고른다 (spec/company_incidents.py). 가상 데이터만.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote

import httpx

from tests.api.conftest import FAKE_FILE, AppFactory, FakeOntology, FakeProvisional, wait_status


async def test_put_company_then_shown_and_cleared(make_client: AppFactory) -> None:
    client = await make_client()
    app = client._transport.app  # type: ignore[attr-defined]
    pid = "가상_공고#001"
    app.state.ontology = FakeOntology({pid: "가상회사"}, ["가상회사"])
    store = FakeProvisional({"prov-fake0001": None})
    app.state.postings = SimpleNamespace(store=store)
    url = f"/postings/{quote(pid, safe='')}/company"

    r = await client.put(url, json={"company": "(주) 다른 가상회사", "set_by": "reviewer-fake"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "posting_id": pid,
        "company_id": "다른가상회사",
        "company": "(주) 다른 가상회사",
        "company_known": False,
        "company_source": "entered",
    }
    # 지우면 다시 공고 데이터의 회사
    r = await client.put(url, json={"company": None, "set_by": "reviewer-fake"})
    assert r.json()["company_source"] == "posting" and r.json()["company_id"] == "가상회사"

    # 회사 칸이 빈 임시 공고에도 정할 수 있다
    prov = await client.put(
        "/postings/prov-fake0001/company", json={"company": "가상회사", "set_by": "reviewer-fake"}
    )
    assert prov.json()["company_id"] == "가상회사" and prov.json()["company_known"] is True
    cleared = await client.put(
        "/postings/prov-fake0001/company", json={"company": None, "set_by": "reviewer-fake"}
    )
    assert cleared.json() == {
        "posting_id": "prov-fake0001",
        "company_id": None,
        "company": None,
        "company_known": None,
        "company_source": None,
    }

    bad = await client.put(url, json={"company": "(주)", "set_by": "r"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "posting_company_invalid"
    gone = await client.put("/postings/prov-fake9999/company", json={"company": "x", "set_by": "r"})
    assert gone.status_code == 404
    assert (await client.put(url, json={"company": "가상회사", "set_by": ""})).status_code == 422


async def test_create_run_sets_company_once_and_refuses_another(make_client: AppFactory) -> None:
    client = await make_client()
    app = client._transport.app  # type: ignore[attr-defined]
    store = FakeProvisional({"prov-fake0001": None})
    app.state.postings = SimpleNamespace(store=store)

    async def run(**data: str) -> httpx.Response:
        return await client.post(
            "/runs",
            data={"posting_id": "prov-fake0001", **data},
            files=[("resumes", FAKE_FILE)],
        )

    r = await run(career_level="신입", company="(주)가상회사")
    assert r.status_code == 202, r.text
    await wait_status(client, r.json()["run_id"], "awaiting_approval")
    assert store.entered == {"prov-fake0001": "(주)가상회사"}
    assert store.set_by["company:prov-fake0001"] == f"run:{r.json()['run_id']}"

    same = await run(company="가상 회사")  # 같은 규칙으로 맞추면 같은 회사
    assert same.status_code == 202, same.text
    await wait_status(client, same.json()["run_id"], "awaiting_approval")
    clash = await run(company="다른회사")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "posting_company_conflict"
    none = await run()  # 회사는 필수가 아니다
    assert none.status_code == 202
    await wait_status(client, none.json()["run_id"], "awaiting_approval")
    assert store.entered == {"prov-fake0001": "(주)가상회사"}


async def test_list_subs_for_selection(make_client: AppFactory) -> None:
    """직군 선택 칸 — 회사 사례 등록 화면이 쓴다. /postings/{id} 보다 먼저 잡힌다."""
    client = await make_client()
    app = client._transport.app  # type: ignore[attr-defined]
    lists = SimpleNamespace(
        subs={
            "sw.frontend": {"label": "프론트엔드", "in_pilot": False},
            "sw.backend": {"label": "백엔드", "in_pilot": True},
        }
    )
    app.state.postings = SimpleNamespace(store=FakeProvisional({}), lists=lists)
    r = await client.get("/postings/subs")
    assert r.status_code == 200, r.text
    assert r.json() == [
        {"id": "sw.backend", "label": "백엔드", "in_pilot": True},
        {"id": "sw.frontend", "label": "프론트엔드", "in_pilot": False},
    ]
