"""리뷰 지적 회귀 테스트 (#262 — code-reviewer W1~W5, privacy-auditor H1·M1·L2). 가상 데이터만."""

from __future__ import annotations

import asyncio
import json

import pytest

from task_spec.interview.incident import edited_incident
from task_spec.llm.backend import Completion
from task_spec.schemas.interview_incident import IncidentInterviewOutput
from tests.interview.conftest import (
    FACTS,
    PROJECT,
    Session,
    case,
    complete_output,
)
from tests.interview.test_stream import events

ASK = IncidentInterviewOutput(missing_fields=["root_cause"], question="원인은 어떻게 확인했나요?")


def mapping_tokens(session: Session) -> dict[str, list[str]]:
    return json.loads(session.mapping_path().read_text())["tokens"]


async def test_concurrent_turns_one_wins_and_mapping_keeps_only_used_tokens(
    session: Session,
) -> None:
    """같은 판본으로 두 요청이 동시에 오면 하나만 저장되고, 진 쪽은 가명화 전에 판본 검사로 막힌다 (W1·W2)."""
    await session.start()
    session.push(ASK, ASK)
    url = f"/interviews/{session.interview_id}/messages"
    first, second = await asyncio.gather(
        session.client.post(url, json={"text": "a@example.com 으로 알림", "revision": 1}),
        session.client.post(url, json={"text": "b@example.com 으로 알림", "revision": 1}),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner = first if first.status_code == 200 else second
    stored = winner.json()["incident_messages"][-2]["text"]
    assert stored.startswith("[CONTACT_1]")
    # 진 쪽은 가명화 전에 막혀 대응표에 그 연락처가 없다 (공고 줄의 다른 토큰은 시작 때 생긴 것)
    contacts = {k: v for k, v in mapping_tokens(session).items() if k.startswith("[CONTACT_")}
    sent = "a@example.com" if winner is first else "b@example.com"
    assert contacts == {"[CONTACT_1]": [sent]}
    assert len(session.backend.prompts) == 1


async def test_mapping_is_saved_before_db_so_a_failed_save_never_blocks_later_turns(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB 저장이 실패(끊김·장애)해도 대응표에 토큰이 먼저 있으므로 다음 턴의 LLM 단계가 막히지 않는다 (W1)."""
    await session.start()
    real_save = session.store.save
    calls = {"n": 0}

    async def failing_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("가상 DB 장애")
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(session.store, "save", failing_once)
    session.push(ASK)
    with pytest.raises(
        ConnectionError
    ):  # 테스트 클라이언트는 앱 예외를 다시 던진다 (운영은 500 응답)
        await session.post("messages", text="ops@example.com 으로 알림이 왔다")
    assert session.revision == 1
    assert "[CONTACT_1]" in mapping_tokens(session)  # 쓰지 않는 토큰이 남을 뿐
    session.push(ASK, ASK)
    again = await session.post("messages", text="ops@example.com 으로 알림이 왔다")
    assert again.status_code == 200, again.text
    later = await session.post("messages", text="원인은 아직 모름")
    assert later.status_code == 200, later.text  # 저장된 토큰이 대응표에 있어 위조로 막히지 않는다


async def test_revise_project_keeps_new_tokens_and_note(session: Session) -> None:
    """프로젝트 수정 요청은 잠금 한 번 안에서 메모·설명·대화를 처리한다 — 새 토큰이 대응표에 남는다."""
    await session.start()
    session.push(ASK)
    response = await session.post(
        "revise", target="project", note="연락처 dev@example.com 정정", text=PROJECT
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_note"] == "연락처 [CONTACT_1] 정정"
    assert body["project"]["overview"] == "가상 배송 서비스"
    assert "[CONTACT_1]" in mapping_tokens(session)
    session.push(ASK)
    assert (await session.post("messages", text="원인은 아직 모름")).status_code == 200


async def test_unknown_trap_note_is_added_once_and_removed_for_a_real_attempt(
    session: Session,
) -> None:
    await session.start()
    session.push(complete_output())
    await session.post("project", text=PROJECT)
    note = "실제 실패 시도는 확인되지 않음"
    for _ in range(2):
        response = await session.post("trap", unknown=True)
        assert response.status_code == 200, response.text
        assert await session.post("revise", target="trap", note="다시")
    assert response.json()["incident_draft"]["notes"].count(note) == 1
    real = await session.post(
        "trap",
        attempted_fix=FACTS["naive_fix"],
        observed_result="복구가 막힘",
        why_failed=FACTS["why_naive_fails"],
    )
    assert real.status_code == 200
    assert note not in real.json()["incident_draft"]["notes"]


async def test_stream_does_not_show_unsafe_question_chunks(session: Session) -> None:
    await session.start()
    session.backend.push(
        Completion('{"question":"[PERSON_9] 님이 ops@example.com 으로 알렸나요?"}')
    )
    response = await session.client.post(
        f"/interviews/{session.interview_id}/messages/stream",
        json={"text": "중복 저장이 발생했다", "revision": session.revision},
    )
    result = events(response)
    shown = [data["text"] for name, data in result if name == "assistant"]
    assert not any("[PERSON_9]" in t or "@" in t for t in shown)
    assert result[-1][0] == "error"


async def test_model_evidence_is_checked_before_saving(session: Session) -> None:
    await session.start()
    leaked = IncidentInterviewOutput(
        missing_fields=["root_cause"],
        question="원인은요?",
        evidence={"symptom": ["ops@example.com 으로 알림"]},
    )
    session.push(leaked)
    response = await session.post("messages", text="중복 저장이 발생했다")
    assert response.status_code == 503
    assert session.revision == 1


async def test_provider_errors_become_503(session: Session) -> None:
    await session.start()
    session.backend.push(RuntimeError("가상 인증 오류"))
    response = await session.post("messages", text="중복 저장이 발생했다")
    assert response.status_code == 503 and response.json()["error"]["code"] == "service_unavailable"


async def test_old_drafts_do_not_revive_when_another_step_is_saved(session: Session) -> None:
    await session.start()
    url = f"/interviews/{session.interview_id}/drafts"
    first = await session.client.patch(
        url, json={"draft_revision": 0, "steps": {"4": {"values": {"a": "원문"}}}}
    )
    session.clock.advance(hours=25)
    revived = await session.client.patch(
        url,
        json={
            "draft_revision": first.json()["draft_revision"],
            "steps": {"5": {"values": {"b": "새 원문"}}},
        },
    )
    assert revived.status_code == 200
    assert list(revived.json()["ui_drafts"]) == ["5"]  # 하루 지난 단계 4 원문은 되살아나지 않는다
    # 23시간마다 다른 단계를 저장해도 처음 쓴 때 기준으로 하루가 지나면 모두 지운다
    session.clock.advance(hours=23)
    await session.client.patch(
        url,
        json={
            "draft_revision": revived.json()["draft_revision"],
            "steps": {"6": {"values": {"c": "x"}}},
        },
    )
    session.clock.advance(hours=2)
    assert (await session.client.get(f"/interviews/{session.interview_id}")).json()[
        "ui_drafts"
    ] == {}


def test_feature_and_templates_cannot_be_edited() -> None:
    draft = case().model_copy(
        update={
            "incident_id": "co:sw.backend:0123456789ab",
            "templates": ["backend/java-spring-boot3-jpa"],
        }
    )

    def pseudonymize(text: str) -> str:
        return text

    for update in ({"templates": ["가상 이름"]}, {"task_pattern": "qa_mutation"}):
        with pytest.raises(ValueError, match="변경할 수 없습니다"):
            edited_incident(draft.model_copy(update=update), draft, pseudonymize=pseudonymize)
