"""사례 인터뷰 D1 흐름 (#262) — 포크 test_interview_incident.py 의 시나리오를 이 레포 서비스·가명화에 맞춰 옮겼다. 가상 데이터만."""

from __future__ import annotations

import copy
import os
import stat
from pathlib import Path

import httpx

from task_spec.api.app import create_app
from task_spec.interview.incident import incident_digest
from task_spec.interview.text import CASE_OPENING, SCOPE_QUESTIONS
from task_spec.llm.backend import Completion
from task_spec.prompting import PromptRegistry
from task_spec.schemas import PublicIncident
from task_spec.schemas.interview_incident import IncidentInterviewOutput
from tests.api.conftest import make_settings
from tests.interview.conftest import (
    FACTS,
    JD_LINES,
    PROJECT,
    REPO,
    Session,
    complete_output,
    make_service,
)


async def test_start_from_posting_pseudonymizes_jd_and_skips_known_team_context(
    session: Session,
) -> None:
    state = await session.start()
    assert state["stage"] == "incident_interview"  # 공고에 90일 성과·판단 기준이 있다
    assert state["scope_fields"] == [] and state["team_context"]["decision_situation"]
    assert state["incident_messages"] == [
        {"role": "assistant", "text": CASE_OPENING, "message_id": None}
    ]
    assert state["company"] == "(주)가상테크" and state["posting_context"]["sub"] == "sw.backend"
    # 회사 이름은 표기 변형까지 ORG 토큰으로 — 공고 줄도 가명화한 글만 저장한다
    assert "[ORG_1] 물류팀" in state["jd_text"] and "가상테크" not in state["jd_text"]
    assert state["revision"] == 1 and state["ui_drafts"] == {}
    path = session.mapping_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


async def test_scope_questions_only_for_missing_team_context(session: Session) -> None:
    session.postings.posting_lines = list(JD_LINES)  # 팀 맥락 줄 없음
    state = await session.start()
    assert state["stage"] == "scope_interview"
    assert state["scope_messages"][-1]["text"] == SCOPE_QUESTIONS["expected_outcome_90_days"]
    first = await session.post("messages", text="입사 90일 안에 중복 저장 장애를 0건으로 만든다")
    assert first.status_code == 200, first.text
    assert first.json()["scope_messages"][-1]["text"] == SCOPE_QUESTIONS["decision_situation"]
    second = await session.post("messages", text="미정")
    assert second.status_code == 200
    body = second.json()
    assert body["stage"] == "incident_interview"
    assert body["team_context"]["expected_outcome_90_days"].startswith("입사 90일")
    assert body["team_context"]["decision_situation"] is None  # '미정' 은 맥락으로 쓰지 않는다
    assert not session.backend.prompts  # 고정 질문 단계는 LLM 을 부르지 않는다


async def test_followup_then_confirmation_and_invalidation(session: Session) -> None:
    await session.start()
    session.push(
        IncidentInterviewOutput(missing_fields=["root_cause"], question="원인은 어떻게 확인했나요?")
    )
    first = await session.post("project", text=PROJECT.replace(FACTS["root_cause"], ""))
    assert first.status_code == 200, first.text
    assert first.json()["stage"] == "incident_interview"
    assert first.json()["incident_messages"][-1]["text"] == "원인은 어떻게 확인했나요?"
    session.push(complete_output())
    reply = await session.post("messages", text=FACTS["root_cause"])
    assert reply.status_code == 200, reply.text
    assert reply.json()["stage"] == "trap_interview"
    trapped = await session.post(
        "trap",
        attempted_fix=FACTS["naive_fix"],
        observed_result="복구가 막힘",
        why_failed=FACTS["why_naive_fails"],
    )
    assert trapped.status_code == 200, trapped.text
    assert trapped.json()["stage"] == "success_interview"
    state = await session.success_step()
    assert state["stage"] == "incident_review" and state["incident_confirmed"] is None
    draft = PublicIncident.model_validate(state["incident_draft"])
    # 인터뷰 사례는 회사 사례 id — 인터뷰 동안 고정, D2 에서 그대로 등록
    assert draft.incident_id == state["incident_id"] and draft.incident_id.startswith(
        "co:sw.backend:"
    )
    assert draft.org == "[ORG_1]" and draft.url == draft.published == "" and not draft.verified
    assert FACTS["conditions"] in draft.symptom
    assert [c.is_trap for c in draft.known_incident_cases] == [False, True]
    assert all(c.answer_text for c in draft.known_incident_cases)
    assert len(state["incident_provenance"]) == 2
    assert state["incident_evidence"]["root_cause"] == [FACTS["root_cause"]]
    assert state["answer_validation"]["passed"] is True
    tampered = {**state["incident_draft"], "sub": "sw.frontend"}
    assert (
        await session.post("incident/confirm", incident=tampered, reviewer="확인자")
    ).status_code == 422
    assert (
        await session.post("incident/confirm", incident=state["incident_draft"], reviewer=" ")
    ).status_code == 422
    stale_revision = session.revision
    confirmed = await session.post(
        "incident/confirm", incident=state["incident_draft"], reviewer="확인자"
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["stage"] == "competencies"
    assert not confirmed.json()["incident_confirmed"]["verified"]
    stale = await session.client.post(
        f"/interviews/{session.interview_id}/incident/confirm",
        json={
            "revision": stale_revision,
            "incident": state["incident_draft"],
            "reviewer": "확인자",
        },
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "interview_conflict"
    revised = await session.post("revise", target="incident", note="원인 설명 정정")
    assert revised.status_code == 200 and revised.json()["incident_confirmed"] is None
    assert revised.json()["stage"] == "incident_review"
    assert revised.json()["review_note"] == "원인 설명 정정"


async def test_continue_without_reviewer_keeps_validation_and_records_the_version(
    session: Session,
) -> None:
    await session.start()
    session.push(complete_output())
    assert (await session.post("project", text=PROJECT)).status_code == 200
    assert (await session.post("trap", unknown=True)).status_code == 200
    state = await session.success_step(trap=False)
    draft = state["incident_draft"]
    assert [c["is_trap"] for c in draft["known_incident_cases"]] == [False]
    edited = copy.deepcopy(draft)
    edited["known_incident_cases"][0]["answer_text"] += "\n추가로 쓴 기준"
    # 고친 답안은 다시 검증해야 확인할 수 있다
    assert (await session.post("incident/confirm", incident=edited)).status_code == 422
    response = await session.post("incident/confirm", incident=draft)
    assert response.status_code == 200, response.text
    selected = response.json()
    assert selected["stage"] == "competencies" and selected["incident_confirmed"] == draft
    record = selected["incident_confirmation"]
    assert record["reviewer"] is None and record["revision"] == state["revision"]
    assert record["sha256"] == incident_digest(PublicIncident.model_validate(draft))


async def test_contacts_are_pseudonymized_before_the_model_and_secrets_never_reach_it(
    session: Session,
) -> None:
    await session.start()
    rejected = await session.post("project", text="password=private")
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "interview_invalid"
    assert not session.backend.prompts
    session.push(IncidentInterviewOutput(missing_fields=["root_cause"], question="원인은요?"))
    reply = await session.post(
        "messages", text="가상테크 물류팀 alert@example.com 으로 중복 저장 알림이 왔다"
    )
    assert reply.status_code == 200, reply.text
    sent = session.backend.prompts[-1]
    assert "alert@example.com" not in sent and "[CONTACT_1]" in sent
    assert "가상테크" not in sent and "[ORG_1]" in sent
    stored = reply.json()["incident_messages"][-2]["text"]
    assert "[CONTACT_1]" in stored and "@" not in stored


async def test_bad_model_output_does_not_advance_or_save(session: Session) -> None:
    await session.start()
    before = copy.deepcopy(session.store.rows[session.interview_id])
    session.backend.push(Completion("broken-json"), Completion("broken-again"))
    response = await session.post("messages", text="중복 저장이 발생했다")
    assert response.status_code == 503 and response.json()["error"]["code"] == "service_unavailable"
    after = session.store.rows[session.interview_id]
    assert after.revision == before.revision and after.state == before.state


async def test_model_output_with_contact_or_unknown_token_is_rejected(session: Session) -> None:
    await session.start()
    session.push(
        IncidentInterviewOutput(
            missing_fields=["root_cause"], question="ops@example.com 으로 문의했나요?"
        )
    )
    leaked = await session.post("messages", text="중복 저장이 발생했다")
    assert leaked.status_code == 503
    session.push(
        IncidentInterviewOutput(
            missing_fields=["root_cause"], question="[PERSON_7] 님이 확인했나요?"
        )
    )
    forged = await session.post("messages", text="중복 저장이 발생했다")
    assert forged.status_code == 503
    assert session.revision == 1


async def test_message_id_replay_is_idempotent_and_conflicts_on_different_text(
    session: Session,
) -> None:
    await session.start()
    session.push(IncidentInterviewOutput(missing_fields=["root_cause"], question="원인은요?"))
    body = {"text": "중복 저장이 발생했다", "message_id": "8a8b0c1e-0000-4000-8000-000000000001"}
    first = await session.post("messages", **body)
    assert first.status_code == 200
    calls = len(session.backend.prompts)
    replay = await session.client.post(
        f"/interviews/{session.interview_id}/messages", json={**body, "revision": 1}
    )
    assert replay.status_code == 200 and replay.json() == first.json()
    assert len(session.backend.prompts) == calls
    conflict = await session.post("messages", **{**body, "text": "다른 내용"})
    assert conflict.status_code == 409


async def test_stage_and_revision_guards(session: Session) -> None:
    await session.start()
    assert (await session.post("trap", unknown=True)).status_code == 409  # 아직 사례 대화 단계
    stale = await session.client.post(
        f"/interviews/{session.interview_id}/messages", json={"text": "가상", "revision": 99}
    )
    assert stale.status_code == 409
    missing = await session.client.get("/interviews/int-" + "0" * 32)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "interview_not_found"
    unknown_posting = await session.client.post(
        "/interviews", json={"posting_id": "prov-ffffffffffff"}
    )
    assert (
        unknown_posting.status_code == 404
        and unknown_posting.json()["error"]["code"] == "posting_not_found"
    )


async def test_not_ready_without_current_prompts(tmp_path: Path, detectors) -> None:
    service, backend, *_ = make_service(
        tmp_path, detectors, prompts=PromptRegistry(REPO / "prompts")
    )
    assert not service.ready()
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/interviews", json={"posting_id": "prov-0123456789ab"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "interview_not_ready"
    assert not backend.prompts


async def test_discard_deletes_mapping_and_closes_the_interview(session: Session) -> None:
    await session.start()
    assert session.mapping_path().is_file()
    response = await session.client.post(f"/interviews/{session.interview_id}/discard")
    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "discarded" and body["discard_reason"] == "recruiter"
    assert not session.mapping_path().parent.exists()
    closed = await session.post("messages", text="계속")
    assert closed.status_code == 409 and closed.json()["error"]["code"] == "interview_closed"
    viewed = await session.client.get(f"/interviews/{session.interview_id}")
    assert viewed.status_code == 200 and viewed.json()["stage"] == "discarded"
