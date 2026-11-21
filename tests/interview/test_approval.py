"""사례 인터뷰 D2b — 과제 승인은 interrupt 그래프로만 (결정 2, 불변식 9). 가상 데이터만 (#266).

승인 확정 = 인터뷰 판본 확정 + 사례 co: 등록(공유 대기) + 공통 과제 승인(교체는 replaces) + 다른 초안 정리 — 한 묶음.
커밋 뒤 대응표 삭제(결정 4). 반려는 사유(가명화)를 수정 요청으로 남긴다. 빌드는 승인과 따로 요청한다.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from task_spec.api.app import create_app
from task_spec.api.interview_generation import InterviewGenerationRunner
from task_spec.graph import make_checkpointer
from task_spec.graph.interview_approval import interview_approval_graph, thread_id
from task_spec.interview.approval import ApprovalCommit, MemoryApprovalCommitter, blocked_findings
from task_spec.interview.errors import OutputRejected
from task_spec.interview.task import TaskGenerationFailed
from task_spec.llm.backend import Completion
from task_spec.postings.company import company_id
from task_spec.spec.company_incidents import InMemoryCompanyIncidents
from task_spec.spec.store import InMemoryCommonStore
from task_spec.starter.store import CommonRef, InMemoryBuildStore
from tests.api.conftest import make_settings
from tests.interview.conftest import Session, make_service
from tests.interview.test_task import (
    SELECTED,
    FakeTaskMaker,
    confirmed,
    feature_seed_output,
    generated,
    known_incident,
)

ANSWER = "자료로 확인할 수 있는 범위까지만 알려 준다"


@dataclass
class Env:
    session: Session
    maker: FakeTaskMaker
    common: InMemoryCommonStore
    incidents: InMemoryCompanyIncidents
    builds: InMemoryBuildStore
    graph: Any

    def other(self) -> Session:
        """같은 서비스·공고의 두 번째 인터뷰."""
        s = self.session
        return Session(s.client, s.backend, s.service, s.store, s.postings, s.data_dir, s.clock)

    def rows(self, interview_id: str) -> list[dict[str, Any]]:
        return [r for r in self.common.rows if r["key"].prompt_ref == f"interview:{interview_id}"]


@pytest.fixture
async def env(tmp_path: Path, detectors) -> AsyncIterator[Env]:
    maker = FakeTaskMaker()
    common, incidents, builds = (
        InMemoryCommonStore(),
        InMemoryCompanyIncidents(),
        InMemoryBuildStore(),
    )
    service, backend, store, postings, clock = make_service(
        tmp_path, detectors, tasks=maker, common_store=common, incidents=incidents, builds=builds
    )
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with make_checkpointer(kind="memory") as cp:
        graph = interview_approval_graph(service, cp)
        app.state.interview_approval = graph
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            session = Session(client, backend, service, store, postings, tmp_path, clock)
            yield Env(session, maker, common, incidents, builds, graph)


async def ready(session: Session) -> dict[str, Any]:
    """과제 생성 → 한 번 고쳐 초안 판 2개 → 짚은 질문 답까지 (승인할 수 있는 상태)."""
    spec = (await generated(session))["spec"]
    task = {**spec["common_task"], "title": "가상 배송 상태 기록 기능 (고침)"}
    revised = await session.post("revise", target="common", note="제목 다듬기", task=task)
    assert revised.status_code == 200, revised.text
    answered = await session.post("answers", answers={"q1": ANSWER})
    assert answered.status_code == 200, answered.text
    return answered.json()


async def approval(session: Session) -> httpx.Response:
    return await session.client.get(f"/interviews/{session.interview_id}/approval")


async def decide(session: Session, **body: Any) -> httpx.Response:
    return await session.post("approve", reviewer="가상 승인자", **body)


# ================================================================ 승인


async def test_approval_registers_the_case_approves_the_task_and_deletes_the_mapping(env) -> None:
    s = env.session
    body = await ready(s)
    spec = body["spec"]
    common_id, incident_id = spec["common_task"]["task_id"], spec["coding_incident"]["incident_id"]
    assert len(env.rows(s.interview_id)) == 2  # 생성 + 고친 판
    waiting = await approval(s)
    assert waiting.status_code == 200, waiting.text
    payload = waiting.json()
    assert payload["state"] == "awaiting" and payload["problems"] == []
    assert payload["common_id"] == common_id and payload["incident_id"] == incident_id
    assert payload["current"] is None and payload["company_id"] == "가상테크"
    # 대기 내용(체크포인트)에 인터뷰·과제 본문을 싣지 않는다
    text = json.dumps(payload, ensure_ascii=False)
    assert spec["common_task"]["scenario"] not in text and ANSWER not in text

    decided = await decide(s, revision=s.revision, status="approved")
    assert decided.status_code == 200, decided.text
    closed = decided.json()
    assert closed["state"] == "closed" and closed["stage"] == "approved"
    record = closed["approval"]
    assert record["common_id"] == common_id and record["incident_id"] == incident_id
    assert record["reviewer"] == "가상 승인자" and record["replaced"] is None
    # 사례는 co: 로 등록 — 공유 승인 대기, 출처는 이 인터뷰 판본
    rec = env.incidents.rows[incident_id]
    assert rec.status == "pending" and rec.company_id == company_id("(주)가상테크")
    assert rec.source == {
        "kind": "interview",
        "interview_id": s.interview_id,
        "revision": record["revision"],
    }
    # 기능형 재료는 넣고, 기준 답안·인터뷰 본문은 넣지 않는다 (채점 근거 — 인터뷰 상태에만)
    assert rec.incident.feature is not None and rec.incident.known_incident_cases == []
    assert closed["approval"]["incident_id"] == incident_id
    # 공통 과제 승인, 같은 인터뷰의 다른 초안 판은 지움
    [row] = env.rows(s.interview_id)
    assert row["id"] == common_id and row["status"] == "approved"
    assert row["approved_by"] == "가상 승인자"
    # 결정 4 — 승인하면 이름 대응표 삭제, 더 고치지 않는다
    assert not s.mapping_path().exists()
    blocked = await s.post("answers", answers={"q1": ANSWER})
    assert blocked.status_code == 409 and "새 인터뷰로 교체" in blocked.json()["error"]["message"]
    assert (await approval(s)).json()["state"] == "closed"
    assert (await decide(s, revision=s.revision, status="approved")).status_code == 409


async def test_replacing_an_approved_interview_task_needs_explicit_replaces(env) -> None:
    first = env.session
    await ready(first)
    assert (await decide(first, revision=first.revision, status="approved")).status_code == 200
    first_task = env.rows(first.interview_id)[0]["id"]

    second = env.other()
    body = await ready(second)
    second_task = body["spec"]["common_task"]["task_id"]
    payload = (await approval(second)).json()
    assert payload["current"]["common_id"] == first_task  # 교체 대상을 보여 준다
    before = (len(env.incidents.rows), copy.deepcopy(env.common.rows))
    refused = await decide(second, revision=second.revision, status="approved")
    assert refused.status_code == 422 and first_task in refused.json()["error"]["message"]
    # 아무것도 바뀌지 않는다 — 사례 등록·과제 상태·인터뷰 단계·대응표
    assert len(env.incidents.rows) == before[0] and env.common.rows == before[1]
    assert (await second.client.get(f"/interviews/{second.interview_id}")).json()[
        "stage"
    ] == "task_review"
    assert second.mapping_path().exists()

    replaced = await decide(
        second, revision=second.revision, status="approved", replaces=first_task
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["approval"]["replaced"] == first_task
    status = {r["id"]: r["status"] for r in env.common.rows}
    assert status[first_task] == "superseded" and status[second_task] == "approved"


async def test_problems_block_approval_but_a_rejection_goes_back_to_review(env) -> None:
    s = env.session
    await generated(s)  # 짚은 질문의 답이 없다
    payload = (await approval(s)).json()
    assert "짚은 질문의 답을 모두 정해 주세요" in payload["problems"]
    refused = await decide(s, revision=s.revision, status="approved")
    assert refused.status_code == 422 and "짚은 질문의 답" in refused.json()["error"]["message"]
    assert env.incidents.rows == {}
    no_reason = await decide(s, revision=s.revision, status="rejected")
    assert no_reason.status_code == 422 and "사유" in no_reason.json()["error"]["message"]

    revision = s.revision
    rejected = await decide(
        s, revision=revision, status="rejected", comment="ops@example.com 의견: 자료가 부족합니다"
    )
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["stage"] == "task_review" and body["revision"] == revision + 1
    assert body["review_note"].startswith("[CONTACT_1]") and "@" not in body["review_note"]
    # 체크포인트의 결정에도 원문 사유가 남지 않는다
    cfg: RunnableConfig = {"configurable": {"thread_id": thread_id(s.interview_id, revision)}}
    snap = await env.graph.aget_state(cfg)
    assert snap.values["result"] == "rejected" and "@" not in snap.values["decision"].comment
    assert s.mapping_path().exists()  # 반려는 닫지 않는다
    # 새 판본은 새 승인 thread
    assert (await approval(s)).json()["revision"] == revision + 1


async def test_a_blocked_approval_is_not_replayed_after_the_problem_is_fixed(env) -> None:
    """승인 불가 사유로 막힌 '승인'이 사유가 풀린 뒤 다음 결정(반려) 대신 적용되면 안 된다 (code review C1 —
    한 노드 안에서 interrupt 를 되풀이하면 resume 이 값 목록을 처음부터 재생한다)."""
    s = env.session
    await ready(s)
    s.postings.company_name = None  # 공고에 회사가 없다 — 판본은 그대로
    payload = (await approval(s)).json()
    assert any("공고에 회사가 없어" in p for p in payload["problems"])
    blocked = await decide(s, revision=s.revision, status="approved")
    assert blocked.status_code == 422
    s.postings.company_name = "(주)가상테크"  # 사유가 풀림 (인터뷰 판본은 그대로 — 같은 thread)
    revision = s.revision
    rejected = await decide(s, revision=revision, status="rejected", comment="자료가 부족합니다")
    assert rejected.status_code == 200, rejected.text
    assert (
        rejected.json()["stage"] == "task_review"
        and rejected.json()["review_note"] == "자료가 부족합니다"
    )
    assert env.incidents.rows == {} and s.mapping_path().exists()
    cfg: RunnableConfig = {"configurable": {"thread_id": thread_id(s.interview_id, revision)}}
    assert (await env.graph.aget_state(cfg)).values["result"] == "rejected"


async def test_a_double_click_approves_once_and_both_screens_see_it(env) -> None:
    s = env.session
    await ready(s)
    revision = s.revision
    first, second = await asyncio.gather(
        decide(s, revision=revision, status="approved"),
        decide(s, revision=revision, status="approved"),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes[0] == 200 and codes[1] in (200, 409), (first.text, second.text)
    assert [r.json()["state"] for r in (first, second) if r.status_code == 200][0] == "closed"
    assert len(env.incidents.rows) == 1
    assert [r["status"] for r in env.rows(s.interview_id)] == ["approved"]


async def test_an_old_screen_cannot_approve_a_newer_revision(env) -> None:
    s = env.session
    await ready(s)
    old = s.revision
    assert (await approval(s)).json()["revision"] == old
    assert (await s.post("answers", answers={"q1": "다른 범위"})).status_code == 200  # 판본이 바뀜
    assert (await decide(s, revision=old, status="approved")).status_code == 409
    assert (
        await s.service.approval_context(s.interview_id, old) is None
    )  # 옛 판본은 기다리지 않는다
    # 옛 thread 를 직접 resume 해도 적용되지 않는다 (stale)
    cfg: RunnableConfig = {"configurable": {"thread_id": thread_id(s.interview_id, old)}}
    decision = {"status": "approved", "reviewer": "x", "at": datetime.now(UTC).isoformat()}
    await env.graph.ainvoke(Command(resume=decision), cfg)
    assert (await env.graph.aget_state(cfg)).values["result"] == "stale"
    assert (await s.client.get(f"/interviews/{s.interview_id}")).json()["stage"] == "task_review"
    assert env.incidents.rows == {}


async def test_a_concurrent_edit_does_not_bring_the_mapping_back(env, monkeypatch) -> None:
    """확정은 인터뷰 잠금 안에서 — 그 사이 들어온 수정은 승인 뒤 닫힌 인터뷰를 보고 대응표를 다시 만들지 않는다."""
    s = env.session
    await ready(s)
    committer = s.service.approvals
    assert isinstance(committer, MemoryApprovalCommitter)
    entered, release = asyncio.Event(), asyncio.Event()
    original = committer.commit

    async def slow(c: ApprovalCommit) -> bool:
        entered.set()
        await release.wait()
        return await original(c)

    monkeypatch.setattr(committer, "commit", slow)
    approving = asyncio.create_task(decide(s, revision=s.revision, status="approved"))
    await entered.wait()
    editing = asyncio.create_task(s.post("answers", answers={"q1": "가상테크 기준까지만"}))
    await asyncio.sleep(0.05)
    release.set()
    approved, edited = await approving, await editing
    assert approved.status_code == 200, approved.text
    assert edited.status_code == 409
    assert not s.mapping_path().exists()


# ================================================================ 빌드·정리


async def test_build_is_requested_separately_after_approval(env) -> None:
    s = env.session
    await ready(s)
    early = await s.post("builds")
    assert early.status_code == 409  # 승인 전
    assert (await decide(s, revision=s.revision, status="approved")).status_code == 200
    common_id = env.rows(s.interview_id)[0]["id"]
    env.builds.commons[common_id] = CommonRef("approved", "feature", "onto-test")
    first = await s.post("builds")
    assert first.status_code == 202, first.text
    assert first.json()["created"] is True and first.json()["build_id"].startswith(common_id)
    again = await s.post("builds", requested_by="다른 담당자")
    assert again.status_code == 200 and again.json()["build_id"] == first.json()["build_id"]
    [build] = await env.builds.builds(common_id)
    assert build.requested_by == "가상 승인자"  # 요청자가 없으면 승인자


async def test_discarding_drops_the_interview_drafts(env) -> None:
    s = env.session
    await generated(s)
    assert env.rows(s.interview_id)
    assert (await s.client.post(f"/interviews/{s.interview_id}/discard")).status_code == 200
    assert env.rows(s.interview_id) == []


async def _threads(saver: Any, prefix: str) -> list[str]:
    return [
        t.config["configurable"]["thread_id"]
        async for t in saver.alist(None)
        if t.config["configurable"]["thread_id"].startswith(prefix)
    ]


async def test_deleting_a_discarded_interview_removes_it_and_its_approval_threads(env) -> None:
    """삭제 — 행이 사라져 목록·상세에 없고, 반려·폐기한 판의 승인 대기 thread(가명화한 과제)도 남지 않는다."""
    s = env.session
    await ready(s)
    assert (await approval(s)).status_code == 200  # 승인 대기 thread 가 생김
    assert await _threads(env.graph.checkpointer, f"interview-{s.interview_id}-r")
    company = s.store.rows[s.interview_id].state.company
    assert (await s.client.post(f"/interviews/{s.interview_id}/discard")).status_code == 200
    deleted = await s.client.delete(f"/interviews/{s.interview_id}")
    assert deleted.status_code == 204, deleted.text
    assert (await s.client.get(f"/interviews/{s.interview_id}")).status_code == 404
    listed = await s.client.get("/interviews", params={"company": company})
    assert s.interview_id not in [r["interview_id"] for r in listed.json()]
    assert not await _threads(env.graph.checkpointer, f"interview-{s.interview_id}-r")
    assert (await s.client.delete(f"/interviews/{s.interview_id}")).status_code == 404


async def test_deleting_an_open_interview_runs_the_discard_cleanup_first(env) -> None:
    """진행 중(과제 만들기 실패)인 인터뷰를 바로 지우면 — 대응표·초안 판·실패한 생성 thread 까지 지운다."""
    s = env.session
    await ready(s)
    env.maker.fail = OutputRejected("가상")
    s.backend.push(Completion(feature_seed_output()))
    failed = (await s.generate(competency_ids=SELECTED)).json()
    assert failed["generation"]["status"] == "failed"
    gen_thread = failed["generation"]["thread_id"]
    saver = runner_of(s)._graph.checkpointer
    assert saver is not None and await _threads(saver, gen_thread)
    assert s.mapping_path().is_file() and env.rows(s.interview_id)
    assert (await s.client.delete(f"/interviews/{s.interview_id}")).status_code == 204
    assert s.interview_id not in s.store.rows
    assert not s.mapping_path().parent.exists()
    assert env.rows(s.interview_id) == []
    assert not await _threads(saver, gen_thread)


async def test_a_failed_cleanup_keeps_the_interview_so_it_can_be_deleted_again(env) -> None:
    """정리를 먼저 하고 행은 마지막에 — 정리가 실패하면 500, 인터뷰가 남아 다시 지울 수 있다 (개인정보 점검 M3)."""
    s = env.session
    await ready(s)
    real = env.common.drop_interview_drafts

    async def boom(ref: str, **kwargs: Any) -> None:
        raise RuntimeError("가상 저장소 오류")

    env.common.drop_interview_drafts = boom  # type: ignore[method-assign]
    failed = await s.client.delete(f"/interviews/{s.interview_id}")
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "interview_delete_failed"
    assert s.interview_id in s.store.rows  # 행이 남는다 (폐기된 채)
    env.common.drop_interview_drafts = real  # type: ignore[method-assign]
    assert (await s.client.delete(f"/interviews/{s.interview_id}")).status_code == 204
    assert s.interview_id not in s.store.rows and env.rows(s.interview_id) == []


async def test_an_approval_racing_a_delete_does_not_leave_its_thread(env) -> None:
    """반려 요청이 사유를 가명화한 뒤 삭제가 끼면 resume 이 지운 승인 thread 를 다시 만든다 — 404 와 함께 지운다 (개인정보 점검 M4)."""
    s = env.session
    await ready(s)
    service = s.service
    real_note = service.pseudonymize_note

    async def note_then_delete(interview_id: str, text: str) -> str:
        out = await real_note(interview_id, text)
        deleted = await s.client.delete(
            f"/interviews/{interview_id}"
        )  # 사유 가명화(잠금) 뒤, resume 전에 삭제
        assert deleted.status_code == 204, deleted.text
        return out

    service.pseudonymize_note = note_then_delete  # type: ignore[method-assign]
    raced = await decide(s, revision=s.revision, status="rejected", comment="가상 반려 사유")
    assert raced.status_code == 404, raced.text
    assert raced.json()["error"]["code"] == "interview_not_found"
    assert not await _threads(env.graph.checkpointer, f"interview-{s.interview_id}-r")
    assert s.interview_id not in s.store.rows


async def test_an_approved_interview_is_not_deleted(env) -> None:
    s = env.session
    await ready(s)
    assert (await decide(s, revision=s.revision, status="approved")).status_code == 200
    refused = await s.client.delete(f"/interviews/{s.interview_id}")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "interview_closed"
    assert "근거" in refused.json()["error"]["message"]
    assert s.store.rows[s.interview_id].state.stage == "approved"
    [row] = env.rows(s.interview_id)
    assert row["status"] == "approved"


async def test_an_interview_is_not_deleted_while_its_task_is_being_made(env) -> None:
    s = env.session
    await ready(s)
    env.maker.gate, env.maker.entered = asyncio.Event(), asyncio.Event()
    s.backend.push(Completion(feature_seed_output()))
    started = await s.post("generate", competency_ids=SELECTED)
    assert started.status_code == 202, started.text
    await env.maker.entered.wait()
    blocked = await s.client.delete(f"/interviews/{s.interview_id}")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "interview_generating"
    env.maker.gate.set()
    await runner_of(s).wait(started.json()["generation"]["thread_id"])
    assert s.interview_id in s.store.rows


async def test_an_approved_interview_is_not_discarded(env) -> None:
    s = env.session
    await ready(s)
    assert (await decide(s, revision=s.revision, status="approved")).status_code == 200
    refused = await s.client.post(f"/interviews/{s.interview_id}/discard")
    assert refused.status_code == 409 and "교체" in refused.json()["error"]["message"]
    [row] = env.rows(s.interview_id)
    assert row["status"] == "approved"


# ================================================================ 회사 사례 등록 검사 (연락처·링크)


def test_links_in_llm_fields_block_registration() -> None:
    incident = known_incident()
    assert incident.feature is not None
    clean = blocked_findings(incident)
    assert clean == []
    linked = incident.model_copy(
        update={
            "feature": incident.feature.model_copy(
                update={"business_need": "https://gasang.example.com/orders 주문 기록을 남긴다"}
            )
        }
    )
    [finding] = blocked_findings(linked)
    assert finding.field == "feature" and finding.category == "LINK"


async def test_a_feature_seed_with_a_link_is_rejected_and_a_linked_case_needs_editing(env) -> None:
    s = env.session
    await confirmed(s)
    assert (await s.post("competencies", competency_ids=SELECTED)).status_code == 200
    seed = json.loads(feature_seed_output())
    seed["business_need"] = "https://gasang.example.com/orders 주문 기록을 남긴다"
    s.backend.push(Completion(json.dumps(seed, ensure_ascii=False)))
    response = await s.generate(competency_ids=SELECTED)
    assert response.json()["generation"]["status"] == "failed" and env.common.rows == []
    # 확인한 사례 쪽 칸이면 다시 받아도 같으므로 사례를 고치라고 한다 (422)
    service = s.service
    incident = known_incident()
    service._registrable(incident)  # 깨끗하면 통과
    with pytest.raises(OutputRejected):
        service._registrable(
            incident.model_copy(
                update={
                    "feature": incident.feature.model_copy(  # type: ignore[union-attr]
                        update={"trigger": "ops@example.com 으로 알림"}
                    )
                }
            )
        )
    with pytest.raises(TaskGenerationFailed):
        service._registrable(
            incident.model_copy(update={"symptom": "http://gasang.example.com 접속 실패"})
        )


def runner_of(s: Session) -> InterviewGenerationRunner:
    runner = s.service.generation
    assert isinstance(runner, InterviewGenerationRunner)
    return runner


async def test_approval_waits_while_the_task_is_being_made_again(env) -> None:
    """과제 검토 단계에서 다시 만드는 중이면 승인 대기·결정 모두 409 (#280) — 끝나면 판본이 바뀌어 지금 과제는 낡는다."""
    s = env.session
    await ready(s)
    env.maker.gate, env.maker.entered = asyncio.Event(), asyncio.Event()
    s.backend.push(Completion(feature_seed_output()))
    started = await s.post("generate", competency_ids=SELECTED)
    assert started.status_code == 202, started.text
    await env.maker.entered.wait()
    for blocked in (await approval(s), await decide(s, status="approved")):
        assert (
            blocked.status_code == 409 and blocked.json()["error"]["code"] == "interview_generating"
        )
    env.maker.gate.set()
    runner = s.service.generation
    assert isinstance(runner, InterviewGenerationRunner)
    await runner.wait(started.json()["generation"]["thread_id"])
    assert (await approval(s)).status_code == 200


async def test_approving_drops_a_failed_regeneration_copy(env) -> None:
    """과제 검토 중 다시 만들기가 실패해도 지금 과제는 승인할 수 있고, 실패한 생성의 산출물 사본은 남기지 않는다 (개인정보 점검 L1)."""
    s = env.session
    await ready(s)
    env.maker.fail = OutputRejected("가상")
    s.backend.push(Completion(feature_seed_output()))
    failed = (await s.generate(competency_ids=SELECTED)).json()
    assert failed["generation"]["status"] == "failed" and failed["spec"] is not None
    assert (await approval(s)).status_code == 200
    decided = await decide(s, status="approved")
    assert decided.status_code == 200, decided.text
    state = s.store.rows[s.interview_id].state
    assert state.stage == "approved" and state.generation is None
    saver = runner_of(s)._graph.checkpointer  # 버린 생성의 thread 도 지운다 (#280 I2)
    assert saver is not None
    assert not [
        t
        async for t in saver.alist(None)
        if t.config["configurable"]["thread_id"] == failed["generation"]["thread_id"]
    ]
