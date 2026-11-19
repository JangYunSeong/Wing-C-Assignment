"""인터뷰 보관 기간 (결정 4 [확정: 2026-11-19 사용자], #262) — 원문 임시 저장 최대 하루·제출하면 바로 삭제,
승인·폐기 없이 30일 방치되면 폐기로 보고 이름 대응표 삭제, 주인 없는 대응표 삭제. 가상 데이터만."""

from __future__ import annotations

from task_spec.nodes.pseudonymize.engine import RunMapping
from tests.interview.conftest import FACTS, PROJECT, Session, complete_output

DRAFT = {"values": {"attempted_fix": "재시도 금지라고 적다 만 원문"}}


async def save_drafts(session: Session, steps: dict, draft_revision: int):
    return await session.client.patch(
        f"/interviews/{session.interview_id}/drafts",
        json={"draft_revision": draft_revision, "steps": steps},
    )


async def test_drafts_expire_after_a_day_and_conflicts_are_detected(session: Session) -> None:
    await session.start()
    saved = await save_drafts(session, {"4": DRAFT}, 0)
    assert saved.status_code == 200 and saved.json() == {
        "ui_drafts": {"4": {**DRAFT, "applied": {}}},
        "draft_revision": 1,
    }
    stale = await save_drafts(session, {"4": DRAFT}, 0)
    assert stale.status_code == 409
    session.clock.advance(hours=23)
    assert (await session.client.get(f"/interviews/{session.interview_id}")).json()["ui_drafts"]
    session.clock.advance(hours=2)
    # 하루가 지나면 보이지 않고, 정리하면 지워진다
    assert (await session.client.get(f"/interviews/{session.interview_id}")).json()[
        "ui_drafts"
    ] == {}
    result = await session.service.sweep()
    assert result["drafts"] == 1
    assert session.store.rows[session.interview_id].ui_drafts == {}


async def test_submitting_a_step_clears_only_that_steps_draft(session: Session) -> None:
    await session.start()
    await save_drafts(session, {"3": {"values": {"message": "쓰다 만 답"}}, "4": DRAFT}, 0)
    session.push(complete_output())
    response = await session.post("project", text=PROJECT)
    assert response.status_code == 200, response.text
    drafts = response.json()["ui_drafts"]
    assert (
        "3" not in drafts and "4" in drafts
    )  # 사례 대화(3)를 제출 — 처음 시도한 대응(4) 임시 저장은 남음
    trapped = await session.post(
        "trap",
        attempted_fix=FACTS["naive_fix"],
        observed_result="복구가 막힘",
        why_failed=FACTS["why_naive_fails"],
    )
    assert trapped.status_code == 200 and trapped.json()["ui_drafts"] == {}


async def test_stale_interview_is_discarded_and_its_mapping_deleted(session: Session) -> None:
    await session.start()
    assert session.mapping_path().is_file()
    session.clock.advance(days=29)
    assert (await session.service.sweep())["stale"] == 0
    session.clock.advance(days=2)
    result = await session.service.sweep()
    assert result["stale"] == 1
    row = session.store.rows[session.interview_id]
    assert row.state.stage == "discarded" and row.state.discard_reason == "stale"
    assert not session.mapping_path().parent.exists()
    assert (await session.post("messages", text="다시")).status_code == 409


async def test_business_activity_keeps_the_mapping_but_drafts_do_not(session: Session) -> None:
    await session.start()
    session.clock.advance(days=20)
    assert (await session.post("project", text="")).status_code == 200  # 업무 저장 = 활동
    session.clock.advance(days=20)
    assert (await session.service.sweep())["stale"] == 0
    assert session.mapping_path().is_file()
    # 임시 저장만 계속 보내면 활동으로 세지 않는다 — 열어 둔 화면이 대응표를 무기한 살리지 않게 (M4)
    draft_revision = session.store.rows[session.interview_id].draft_revision
    for _ in range(3):
        session.clock.advance(days=8)
        saved = await save_drafts(session, {"4": DRAFT}, draft_revision)
        draft_revision = saved.json()["draft_revision"]
    assert (await session.service.sweep())["stale"] == 1
    assert not session.mapping_path().parent.exists()


async def test_orphan_mapping_is_deleted(session: Session) -> None:
    await session.start()
    orphan = "int-" + "f" * 32
    session.service.pseudo.save(orphan, RunMapping())
    assert (session.data_dir / "interviews" / orphan).exists()
    result = await session.service.sweep()
    assert result["orphan_mappings"] == 1
    assert not (session.data_dir / "interviews" / orphan).exists()
    assert session.mapping_path().is_file()  # 살아 있는 인터뷰의 대응표는 그대로
