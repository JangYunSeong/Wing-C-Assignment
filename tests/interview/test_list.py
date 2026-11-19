"""인터뷰 목록 GET /interviews — 회사·단계로 거르고, 본문은 싣지 않는다. 가상 데이터만.
Wing 이 시작한 인터뷰 id 를 브라우저에만 기억해 다른 브라우저·담당자가 시작한 인터뷰가 보이지 않았다 (2026-11-20)."""

from __future__ import annotations

from tests.interview.conftest import POSTING_ID, Session

ROW_KEYS = {
    "interview_id",
    "company",
    "company_id",
    "posting_id",
    "title",
    "stage",
    "revision",
    "created_at",
    "updated_at",
    "approval",
    "discarded_at",
    "discard_reason",
}


async def test_list_filters_by_company_and_has_no_bodies(session: Session) -> None:
    await session.start()
    # 회사는 id 또는 표기 — 법인 표기·공백·대소문자를 빼고 맞춘다
    for company in ("가상테크", "(주) 가상테크", "주식회사가상테크"):
        rows = (await session.client.get("/interviews", params={"company": company})).json()
        assert [r["interview_id"] for r in rows] == [session.interview_id], company
    row = rows[0]
    assert set(row) == ROW_KEYS  # 대화·사례·답안 칸이 없다
    assert row["posting_id"] == POSTING_ID and row["company_id"] == "가상테크"
    assert row["stage"] == "incident_interview" and row["approval"] is None
    assert (await session.client.get("/interviews", params={"company": "다른회사"})).json() == []
    assert (await session.client.get("/interviews", params={"company": "(주)"})).json() == []
    assert len((await session.client.get("/interviews")).json()) == 1  # 회사 없이 — 전부


async def test_list_orders_by_recent_progress_and_filters_stage(session: Session) -> None:
    await session.start()
    first = session.interview_id
    session.clock.advance(minutes=5)
    await session.start()
    second = session.interview_id
    rows = (await session.client.get("/interviews")).json()
    assert [r["interview_id"] for r in rows] == [second, first]
    assert (await session.client.get("/interviews", params={"limit": 1})).json()[0][
        "interview_id"
    ] == second
    session.clock.advance(minutes=5)
    assert (await session.client.post(f"/interviews/{first}/discard")).status_code == 200
    rows = (await session.client.get("/interviews")).json()
    assert [r["interview_id"] for r in rows] == [first, second]  # 폐기도 진행 — 맨 앞
    gone = (await session.client.get("/interviews", params={"stage": "discarded"})).json()
    assert [r["interview_id"] for r in gone] == [first]
    assert gone[0]["discard_reason"] == "recruiter" and gone[0]["discarded_at"]
    assert (await session.client.get("/interviews", params={"stage": "nope"})).status_code == 422


async def test_stale_interview_reads_as_discarded_before_the_sweep(session: Session) -> None:
    await session.start()
    session.clock.advance(days=31)
    rows = (await session.client.get("/interviews", params={"stage": "discarded"})).json()
    assert [r["interview_id"] for r in rows] == [session.interview_id]
    assert rows[0]["discard_reason"] == "stale" and rows[0]["discarded_at"]
    assert (
        await session.client.get("/interviews", params={"stage": "incident_interview"})
    ).json() == []
    # 정리 작업이 닫은 뒤에도 같게 보인다
    assert (await session.service.sweep())["stale"] == 1
    rows = (await session.client.get("/interviews")).json()
    assert rows[0]["stage"] == "discarded" and rows[0]["discard_reason"] == "stale"
