"""함정 틀 (#318) × ⑧ 고치기·승인 — 담당자가 과제 글을 고쳐도 그 공고의 틀은 바뀌지 않는다 (설계 검사 S-F1).

고친 글은 새 초안 판으로 저장되고 승인은 최신 판을 승인한 뒤 다른 초안 판을 지운다. 틀 적용 재료(`common_task.framed`)를
고친 판이 이어받지 않으면 틀을 가진 생성 판이 승인 때 지워져, 빌드가 원 재료로 돈다 (금지 저장 엔티티 검사 4 가 꺼지고 재료의
상태 대상 요구가 돌아온다). 가상 데이터만, LLM 없음 (가짜 백엔드).
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import task_spec.interview.trap_frame as tf
from task_spec.api.app import create_app
from task_spec.graph import make_checkpointer
from task_spec.graph.interview_approval import interview_approval_graph
from task_spec.interview.frame_leaks import FLAG_FRAME_LEAK
from task_spec.llm.backend import Completion
from task_spec.prompting import PromptRegistry
from task_spec.spec.company_incidents import InMemoryCompanyIncidents
from task_spec.spec.store import InMemoryCommonStore
from task_spec.starter.store import InMemoryBuildStore
from tests.api.conftest import make_settings
from tests.interview.conftest import REPO, Session, make_service
from tests.interview.test_material_analysis import _analysis_prompts
from tests.interview.test_task import FakeTaskMaker
from tests.interview.test_trap_frame import ANALYSIS, _frame, _generate_framed

ANSWER = "자료로 확인할 수 있는 범위까지만 알려 준다"


@pytest.fixture
async def framed_env(
    tmp_path: Path, detectors: Any
) -> AsyncIterator[tuple[Session, InMemoryCommonStore]]:
    """함정 틀 프롬프트(CURRENT)와 승인 그래프를 함께 둔 인터뷰 서비스."""
    _analysis_prompts(tmp_path)
    src, dst = REPO / "prompts" / tf.TASK, tmp_path / "prompts" / tf.TASK
    dst.mkdir()
    current = (src / "CURRENT").read_text().strip()
    shutil.copy(src / f"{current}.md", dst / f"{current}.md")
    (dst / "CURRENT").write_text(f"{current}\n")
    common = InMemoryCommonStore()
    service, backend, store, postings, clock = make_service(
        tmp_path,
        detectors,
        prompts=PromptRegistry(tmp_path / "prompts"),
        tasks=FakeTaskMaker(),
        common_store=common,
        incidents=InMemoryCompanyIncidents(),
        builds=InMemoryBuildStore(),
    )
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with make_checkpointer(kind="memory") as cp:
        app.state.interview_approval = interview_approval_graph(service, cp)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield Session(client, backend, service, store, postings, tmp_path, clock), common


async def _revise_title(session: Session, spec: dict[str, Any]) -> dict[str, Any]:
    task = {**spec["common_task"], "title": "가상 세션 유지 기능 (고침)"}
    revised = await session.post("revise", target="common", note="제목 다듬기", task=task)
    assert revised.status_code == 200, revised.text
    return dict(revised.json()["spec"])


async def _answer_targeted(session: Session, spec: dict[str, Any]) -> None:
    answers = {q["question_id"]: ANSWER for q in spec["targeted_questions"]}
    answered = await session.post("answers", answers=answers)
    assert answered.status_code == 200, answered.text


async def _approval(session: Session) -> dict[str, Any]:
    waiting = await session.client.get(f"/interviews/{session.interview_id}/approval")
    assert waiting.status_code == 200, waiting.text
    return dict(waiting.json())


async def test_a_revised_task_keeps_the_framed_material_through_approval(
    framed_env: tuple[Session, InMemoryCommonStore],
) -> None:
    """생성 → 제목만 고침 → 최신 판 승인 → 다른 초안 정리 — 승인한 행에 틀 적용 재료가 그대로 남는다 (빌드가 읽는 칸)."""
    session, common = framed_env
    spec = await _generate_framed(session, ANALYSIS, Completion(_frame().model_dump_json()))
    first_id = spec["common_task"]["task_id"]
    framed = await common.framed(first_id)
    assert framed is not None

    revised = await _revise_title(session, spec)
    revised_id = revised["common_task"]["task_id"]
    assert revised_id != first_id  # 고친 글은 새 초안 판
    assert await common.framed(revised_id) == framed

    await _answer_targeted(session, revised)
    payload = await _approval(session)
    assert payload["problems"] == [], payload["problems"]
    decided = await session.post(
        "approve", reviewer="가상 승인자", revision=session.revision, status="approved"
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["approval"]["common_id"] == revised_id
    assert all(r["id"] != first_id for r in common.rows)  # 생성 판은 승인 때 정리된다
    assert await common.framed(revised_id) == framed


async def test_approval_refuses_a_framed_task_whose_stored_row_lost_the_framed_material(
    framed_env: tuple[Session, InMemoryCommonStore],
) -> None:
    """틀이 있는 과제인데 저장본에 틀 적용 재료가 없으면(예전 판 등) 승인하지 않는다 — 원 재료로 조용히 빌드되는 길을 막는다."""
    session, common = framed_env
    spec = await _generate_framed(session, ANALYSIS, Completion(_frame().model_dump_json()))
    await _answer_targeted(session, spec)
    row = next(r for r in common.rows if r["id"] == spec["common_task"]["task_id"])
    row["framed"] = None  # 고치기가 틀을 옮기지 않던 판(설계 검사 S-F1)으로 저장된 경우
    problems = (await _approval(session))["problems"]
    assert any("틀 적용 재료" in p for p in problems), problems
    decided = await session.post(
        "approve", reviewer="가상 승인자", revision=session.revision, status="approved"
    )
    assert decided.status_code != 200 or decided.json().get("stage") != "approved", decided.text


# ── 고친 글의 재검사 (설계 검사 B-F5·B-F6) ─────────────────────────────


async def test_an_edit_that_leaves_one_design_for_a_hidden_requirement_is_refused(
    framed_env: tuple[Session, InMemoryCommonStore],
) -> None:
    """B-F5: 숨은 요구마다 허용 설계 2개 이상 — 생성 때 명세 검사의 규칙을 고친 글에도 (빌드의 허용 구현은 이 글로 만든다)."""
    session, _ = framed_env
    spec = await _generate_framed(session, ANALYSIS, Completion(_frame().model_dump_json()))
    trap = spec["common_task"]["traps"][0]["trap_id"]

    def design(i: int) -> dict[str, Any]:
        return {
            "design_id": f"d{i}",
            "summary": f"가상 설계 {i}",
            "conditions": "가상 조건",
            "trap_ids": [trap],
        }

    async def revise(designs: list[dict[str, Any]], current: dict[str, Any]) -> httpx.Response:
        task = {**current["common_task"], "accepted_designs": designs}
        return await session.post("revise", target="common", note="허용 설계 고침", task=task)

    # 예전 판이 이미 모자라도 늘리는 고침은 받는다 (0 → 1 → 2)
    one = await revise([design(1)], spec)
    assert one.status_code == 200, one.text
    two = await revise([design(1), design(2)], one.json()["spec"])
    assert two.status_code == 200, two.text
    # 2 → 1 은 고친 글이 새로 깨뜨린 것 — 막는다
    back = await revise([design(1)], two.json()["spec"])
    assert back.status_code == 422, back.text
    assert f"숨은 요구 {trap} 를 만족하는 허용 설계가 1개" in back.text


async def test_editing_the_hidden_requirement_under_a_frame_leaves_a_flag(
    framed_env: tuple[Session, InMemoryCommonStore],
) -> None:
    """B-F5: 숨은 요구 글을 고치면 빌드 계약 단계(틀 적용 재료)와 숨은 테스트(고친 글)가 갈린다 — 승인 화면에 남긴다.
    제목만 고치면 남기지 않고, 한 번 남은 표시는 다음 고치기에도 남는다."""
    session, _ = framed_env
    spec = await _generate_framed(session, ANALYSIS, Completion(_frame().model_dump_json()))
    titled = await _revise_title(session, spec)
    assert not any(f == tf.FLAG_FRAME_EDITED for f in titled["review_flags"])
    traps = [
        {**t, "description": t["description"] + " 가상 조건 하나 더."}
        for t in titled["common_task"]["traps"]
    ]
    task = {**titled["common_task"], "traps": traps}
    revised = await session.post("revise", target="common", note="숨은 요구 다듬기", task=task)
    assert revised.status_code == 200, revised.text
    edited = revised.json()["spec"]
    assert tf.FLAG_FRAME_EDITED in edited["review_flags"]
    again = await _revise_title(session, edited)
    assert tf.FLAG_FRAME_EDITED in again["review_flags"]


async def test_a_forbidden_storage_written_into_the_scenario_is_flagged_after_an_edit(
    framed_env: tuple[Session, InMemoryCommonStore],
) -> None:
    """B-F6: 검사 3 은 제목·상황·공개 요구·공개 수용 기준도 본다 — 담당자가 고친 상황 글에 틀이 막은 저장 꼴을 쓰면 표시."""
    session, _ = framed_env
    spec = await _generate_framed(session, ANALYSIS, Completion(_frame().model_dump_json()))
    scenario = spec["common_task"]["scenario"] + " 로그인하면 sessions 테이블에 한 줄을 남긴다."
    task = {**spec["common_task"], "scenario": scenario}
    revised = await session.post("revise", target="common", note="상황 보강", task=task)
    assert revised.status_code == 200, revised.text
    flags = revised.json()["spec"]["review_flags"]
    assert any(
        f.startswith(FLAG_FRAME_LEAK) and "상황" in f and "Session 저장" in f for f in flags
    ), flags
