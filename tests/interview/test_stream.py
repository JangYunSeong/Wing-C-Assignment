"""대화 스트림은 질문만 노출하며 검증·저장 후 최종 상태로 끝난다 — 포크 test_interview_stream.py 를 옮김 (#262). 가상 데이터만."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from task_spec.api.routers import interviews
from task_spec.interview.stream import QuestionStream
from task_spec.llm.backend import Completion
from tests.interview.conftest import FACTS, PROJECT, Session


def events(response: Any) -> list[tuple[str, Any]]:
    result = []
    for frame in response.text.replace("\r\n", "\n").split("\n\n"):
        lines = frame.splitlines()
        name = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
        data = "\n".join(line[5:].strip() for line in lines if line.startswith("data:"))
        if name and data:
            result.append((name, json.loads(data)))
    return result


async def test_question_chunks_handle_escapes_retry_and_never_send_incident_json() -> None:
    received: list[tuple[str, str]] = []

    async def emit(name: str, data: dict) -> None:
        received.append((name, data["text"]))

    stream = QuestionStream(emit)
    source = json.dumps(
        {
            "question": '문서 "완료" 표시와\n색인 기록은 어떻게 달랐나요?',
            "incident": {"private": "not a question"},
        },
        ensure_ascii=True,
    )
    await stream(None)
    for offset in range(0, len(source), 3):
        await stream(source[offset : offset + 3])
    assert len(received) > 3
    assert received[-1][1] == '문서 "완료" 표시와\n색인 기록은 어떻게 달랐나요?'
    assert not any("private" in text or "not a question" in text for _, text in received)
    await stream(None)
    await stream('{"question":"다른 질문인가요?"}')
    assert received[-2][1] == "" and received[-1][1] == "다른 질문인가요?"


async def test_stream_orders_receipt_partial_question_and_saved_state(session: Session) -> None:
    await session.start()
    question = "재시도 기록에서 같은 요청의 응답과 저장 결과를 어떻게 비교하셨나요?"
    session.backend.push(
        Completion(
            json.dumps(
                {
                    "question": question,
                    "incident": None,
                    "conditions": FACTS["conditions"],
                    "evidence": {
                        "symptom": [FACTS["symptom"]],
                        "conditions": [FACTS["conditions"]],
                    },
                },
                ensure_ascii=False,
            )
        )
    )
    body = {"text": PROJECT, "revision": session.revision, "message_id": str(uuid4())}
    url = f"/interviews/{session.interview_id}/messages/stream"
    response = await session.client.post(url, json=body)
    assert response.status_code == 200 and "text/event-stream" in response.headers["content-type"]
    result = events(response)
    assert result[0][0] == "accepted" and result[-1][0] == "done"
    partial = [data["text"] for name, data in result if name == "assistant" and data["text"]]
    assert len(partial) > 2 and partial[0] != question and partial[-1] == question
    final = result[-1][1]
    assert final["revision"] == body["revision"] + 1
    assert final["incident_messages"][-2]["message_id"] == body["message_id"]
    assert final["incident_messages"][-1]["text"] == question
    replay = await session.client.post(url, json=body)
    assert events(replay)[-1] == ("done", final)
    assert len(session.backend.prompts) == 1
    conflict = await session.client.post(url, json={**body, "text": "다른 내용"})
    assert conflict.status_code == 409


async def test_stream_accepts_pseudonymized_text(session: Session) -> None:
    await session.start()
    session.backend.push(Completion('{"question":"원인은요?","missing_fields":["root_cause"]}'))
    response = await session.client.post(
        f"/interviews/{session.interview_id}/messages/stream",
        json={"text": "ops@example.com 으로 알림이 왔다", "revision": session.revision},
    )
    accepted = events(response)[0]
    assert accepted[0] == "accepted" and "[CONTACT_1]" in accepted[1]["text"]
    assert "@" not in accepted[1]["text"]


async def test_stream_failure_does_not_save_and_stale_requests_fail_before_stream(
    session: Session,
) -> None:
    await session.start()
    before = session.store.rows[session.interview_id]
    session.backend.push(Completion('{"question":"임시 질문", broken'), Completion("bad JSON"))
    url = f"/interviews/{session.interview_id}/messages/stream"
    response = await session.client.post(
        url, json={"text": "가상 증상", "revision": session.revision}
    )
    result = events(response)
    assert result[-1][0] == "error" and not any(name == "done" for name, _ in result)
    assert result[-1][1]["code"] == "service_unavailable"
    after = session.store.rows[session.interview_id]
    assert after.revision == before.revision and after.state == before.state
    stale = await session.client.post(url, json={"text": "가상 증상", "revision": 99})
    assert stale.status_code == 409 and "text/event-stream" not in stale.headers["content-type"]
    secret = await session.client.post(
        url, json={"text": "api_key=sk-abcdefghijklmnop1234", "revision": session.revision}
    )
    assert secret.status_code == 422 and "text/event-stream" not in secret.headers["content-type"]


async def test_disconnecting_stops_the_model_task_without_saving(
    session: Session, monkeypatch
) -> None:
    await session.start()
    before = session.store.rows[session.interview_id]
    cancelled = asyncio.Event()

    async def waiting(*args: Any, emit: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            await emit("assistant", {"text": ""})
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {}

    monkeypatch.setattr(session.service, "message", waiting)
    body = interviews.MessageRequest(text="가상 답변", revision=session.revision)
    stream = interviews.stream_message(session.interview_id, body, session.service, "가상 답변")
    assert (await anext(stream)).event == "accepted"
    assert (await anext(stream)).event == "assistant"
    await asyncio.wait_for(stream.aclose(), timeout=1)
    assert cancelled.is_set()
    after = session.store.rows[session.interview_id]
    assert after.revision == before.revision and after.state == before.state
