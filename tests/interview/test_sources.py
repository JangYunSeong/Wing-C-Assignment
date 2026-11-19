"""사례 인터뷰 D2c — 인터뷰 대신 공개 사례·같은 회사의 공유 승인된 사례로 과제 만들기 (#266). 가상 데이터만.

포크 POST /incident/source 와 같은 경로. 공개·회사 사례는 기능형 재료를 그대로 쓰고(LLM 없이) 고치지 않으며,
기준 답안은 싣지 않는다. 승인할 때 새로 등록하지 않는다 (이미 있는 사례).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from task_spec.api.app import create_app
from task_spec.graph import make_checkpointer
from task_spec.graph.interview_approval import interview_approval_graph
from task_spec.interview.task import FLAG_INTERVIEW_INCIDENT
from task_spec.schemas import PublicIncident
from task_spec.spec.company_incidents import (
    InMemoryCompanyIncidents,
    content_sha256,
)
from task_spec.spec.incidents import load_incidents
from task_spec.spec.store import CommonKey, InMemoryCommonStore
from task_spec.starter.store import InMemoryBuildStore
from tests.api.conftest import make_settings
from tests.interview.conftest import REPO, SUCCESS, Session, make_service
from tests.interview.test_task import (
    PROFILE,
    SELECTED,
    FakeTaskMaker,
    fake_task,
    known_incident,
)

AT = datetime(2026, 11, 19, tzinfo=UTC)
PUBLIC_DEFAULT = "pub:sw.backend:pool-hold-and-wait-deadlock"
PUBLIC_OTHER = "pub:sw.backend:retry-double-processing"


@dataclass
class Env:
    session: Session
    common: InMemoryCommonStore
    incidents: InMemoryCompanyIncidents


@pytest.fixture
async def env(tmp_path: Path, detectors) -> AsyncIterator[Env]:
    common, incidents = InMemoryCommonStore(), InMemoryCompanyIncidents()
    service, backend, store, postings, clock = make_service(
        tmp_path,
        detectors,
        tasks=FakeTaskMaker(),
        common_store=common,
        incidents=incidents,
        builds=InMemoryBuildStore(),
    )
    service.public_incidents = load_incidents(REPO / "incidents" / "public_v1")
    service.company_incidents = incidents
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with make_checkpointer(kind="memory") as cp:
        app.state.interview_approval = interview_approval_graph(service, cp)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield Env(
                Session(client, backend, service, store, postings, tmp_path, clock),
                common,
                incidents,
            )


async def company_case(
    env: Env,
    suffix: str,
    *,
    company: str = "가상테크",
    shared: bool = True,
    feature: bool = True,
    **update: Any,
) -> PublicIncident:
    base = known_incident()
    inc = base.model_copy(
        update={
            "incident_id": f"co:sw.backend:{suffix}",
            "title": f"가상 회사 사례 {suffix}",
            **({} if feature else {"feature": None, "templates": []}),
            **update,
        }
    )
    await env.incidents.submit(company, inc, source={"kind": "manual"}, created_by="가상 등록자")
    if shared:
        await env.incidents.decide(
            inc.incident_id, content_sha256(inc), "shared", "가상 승인자", AT, None
        )
    return inc


async def choose(s: Session, source: str, incident_id: str | None = None) -> httpx.Response:
    return await s.post("incident/source", source=source, incident_id=incident_id)


async def task_review(s: Session, source: str, incident_id: str | None = None) -> dict[str, Any]:
    """출처 고르기 → 확인 → 역량 → 과제 생성 (기능형 재료 LLM 없이) → 짚은 질문 답."""
    await s.start()
    chosen = await choose(s, source, incident_id)
    assert chosen.status_code == 200, chosen.text
    body = chosen.json()
    assert body["stage"] == "incident_review" and body["incident_source"] == source
    confirmed = await s.post("incident/confirm", incident=body["incident_draft"])
    assert confirmed.status_code == 200, confirmed.text
    assert (await s.post("competencies", competency_ids=SELECTED)).status_code == 200
    calls = len(s.backend.prompts)
    generated = await s.post("generate", competency_ids=SELECTED)
    assert generated.status_code == 200, generated.text
    assert (
        len(s.backend.prompts) == calls
    )  # 기능형 재료를 그대로 쓴다 — interview_feature_seed 를 부르지 않음
    answered = await s.post("answers", answers={"q1": "자료로 확인할 수 있는 범위까지만"})
    assert answered.status_code == 200, answered.text
    return answered.json()


async def approve(s: Session) -> httpx.Response:
    return await s.post("approve", status="approved", reviewer="가상 승인자")


async def test_sources_list_public_cases_and_this_companys_shared_cases(env) -> None:
    await company_case(env, "aaaa00000001")
    await company_case(env, "aaaa00000002", feature=False)  # 기능형 재료 없음 — 보이지만 못 씀
    await company_case(env, "aaaa00000003", shared=False)  # 공유 승인 전 — 안 보임
    await company_case(env, "aaaa00000004", company="다른회사")  # 다른 회사 — 안 보임
    s = env.session
    await s.start()
    body = (await s.client.get(f"/interviews/{s.interview_id}/incident/sources")).json()
    assert body["company_id"] == "가상테크" and body["incident_source"] == "known_problem"
    public = {p["incident_id"]: p["usable"] for p in body["public"]}
    assert public[PUBLIC_DEFAULT] and public[PUBLIC_OTHER]
    company = {c["incident_id"]: c for c in body["company"]}
    assert set(company) == {"co:sw.backend:aaaa00000001", "co:sw.backend:aaaa00000002"}
    assert company["co:sw.backend:aaaa00000001"]["usable"] is True
    assert company["co:sw.backend:aaaa00000002"]["usable"] is False
    assert company["co:sw.backend:aaaa00000001"]["used_by_postings"] == 0


async def test_a_public_case_makes_the_task_and_is_approved_without_registering(env) -> None:
    s = env.session
    body = await task_review(s, "public_fixed")  # incident_id 없으면 직군 기본 사례 (포크와 같음)
    spec = body["spec"]
    assert spec["incident_source"] == "public_fixed"
    assert spec["coding_incident"]["incident_id"] == PUBLIC_DEFAULT
    assert (
        "known_incident_cases" not in spec["coding_incident"]
    )  # 기준 답안 없음 (빈 칸은 직렬화에서 빠진다)
    assert FLAG_INTERVIEW_INCIDENT not in spec["common_task"]["review_flags"]
    decided = await approve(s)
    assert decided.status_code == 200, decided.text
    record = decided.json()["approval"]
    assert record["incident_id"] == PUBLIC_DEFAULT and record["incident_registered"] is False
    assert env.incidents.rows == {}  # 공개 사례는 회사 사례로 등록하지 않는다
    assert not s.mapping_path().exists()


async def test_a_company_case_is_used_without_its_answers_and_flags_reuse(env) -> None:
    legacy = await company_case(env, "bbbb00000001")  # 예전 등록본처럼 기준 답안이 들어 있어도
    assert legacy.known_incident_cases
    other = fake_task(PROFILE, "t", scenario="가상", source_incident=legacy.incident_id)
    used = await env.common.save_draft(
        CommonKey("prov-other0000001", "onto-test", "draft_task@1"), other, []
    )
    await env.common.approve(used.id, "가상", AT)  # 다른 공고가 이미 쓰는 사례
    s = env.session
    body = await task_review(s, "company", legacy.incident_id)
    spec = body["spec"]
    assert spec["incident_source"] == "company"
    assert (
        "known_incident_cases" not in spec["coding_incident"]
    )  # 다른 공고의 기준 답안은 생성 입력으로 가지 않는다
    assert "known_incident_cases" not in spec["incident"]
    assert any("다른 공고 1곳" in f for f in spec["review_flags"])
    before = dict(env.incidents.rows)
    decided = await approve(s)
    assert decided.status_code == 200, decided.text
    assert decided.json()["approval"]["incident_registered"] is False
    assert env.incidents.rows == before  # 새로 등록하지 않는다


async def test_a_company_case_retired_after_choosing_cannot_be_used(env) -> None:
    case = await company_case(env, "cccc00000001")
    s = env.session
    await task_review(s, "company", case.incident_id)
    await env.incidents.retire(case.incident_id, "가상", AT, "밖으로 샘")
    payload = (await s.client.get(f"/interviews/{s.interview_id}/approval")).json()
    assert any("고른 회사 사례를 더 쓸 수 없습니다" in p for p in payload["problems"])
    assert (await approve(s)).status_code == 422
    regenerate = await s.post("generate", competency_ids=SELECTED)
    assert regenerate.status_code == 422


@pytest.mark.parametrize(
    "setup",
    ["other_company", "pending", "missing", "no_feature", "public_of_other_sub"],
)
async def test_only_this_companys_usable_cases_can_be_chosen(env, setup: str) -> None:
    ids = {
        "other_company": (await company_case(env, "dddd00000001", company="다른회사")).incident_id,
        "pending": (await company_case(env, "dddd00000002", shared=False)).incident_id,
        "missing": "co:sw.backend:dddd00000099",
        "no_feature": (await company_case(env, "dddd00000003", feature=False)).incident_id,
        "public_of_other_sub": "pub:sw.frontend:offline-replay",
    }
    s = env.session
    await s.start()
    source = "public_fixed" if setup == "public_of_other_sub" else "company"
    refused = await choose(s, source, ids[setup])
    assert refused.status_code == 422, refused.text
    assert (await s.client.get(f"/interviews/{s.interview_id}")).json()[
        "stage"
    ] == "incident_interview"


async def test_a_chosen_case_is_not_edited_and_has_no_interview_steps(env) -> None:
    s = env.session
    await s.start()
    body = (await choose(s, "public_fixed", PUBLIC_OTHER)).json()
    edited = {**body["incident_draft"], "title": "고친 제목"}
    assert (await s.post("incident/confirm", incident=edited)).status_code == 422
    assert (await s.post("incident/validate", incident=body["incident_draft"])).status_code == 422
    assert (await s.post("revise", target="trap", note="처음 대응 보완")).status_code == 422
    # 처음 대응·성공 기준·사례 대화도 인터뷰 사례만 — 고른 사례를 바꾸거나 말없이 인터뷰 사례로 돌리지 않는다
    trap = await s.post(
        "trap", attempted_fix="가상 대응", observed_result="가상 결과", why_failed="가상 이유"
    )
    assert trap.status_code == 422, trap.text
    assert (await s.post("success", **SUCCESS)).status_code == 422
    assert (await s.post("messages", text="원인은 가상 설정이었다")).status_code == 422
    after = (await s.client.get(f"/interviews/{s.interview_id}")).json()
    assert (
        after["incident_source"] == "public_fixed"
        and after["incident_draft"] == body["incident_draft"]
    )
    confirmed = await s.post("incident/confirm", incident=body["incident_draft"])
    assert confirmed.status_code == 200 and confirmed.json()["stage"] == "competencies"


async def test_confirm_compares_with_the_stored_case_not_the_state(env) -> None:
    """상태의 draft 가 어떤 길로든 바뀌었으면 확인하지 않는다 — 원본(공개 사례 파일·회사 사례 DB)과 비교."""
    s = env.session
    await s.start()
    body = (await choose(s, "public_fixed", PUBLIC_OTHER)).json()
    row = s.store.rows[s.interview_id]
    tampered = row.state.model_copy(deep=True)
    assert tampered.incident_draft is not None
    tampered.incident_draft = tampered.incident_draft.model_copy(update={"naive_fix": "바뀐 대응"})
    s.store.rows[s.interview_id] = replace(row, state=tampered)
    sent = {**body["incident_draft"], "naive_fix": "바뀐 대응"}
    assert (await s.post("incident/confirm", incident=sent)).status_code == 422


async def test_choosing_the_interview_again_keeps_the_work_in_progress(env) -> None:
    s = env.session
    started = await s.start()
    again = await choose(s, "known_problem")
    assert again.status_code == 200
    assert (
        again.json()["revision"] == started["revision"]
    )  # 이미 인터뷰 사례 — 아무것도 지우지 않는다


async def test_switching_back_to_the_interview_keeps_the_conversation(env) -> None:
    s = env.session
    started = await s.start()
    messages = started["incident_messages"]
    row = s.store.rows[s.interview_id]  # 앞 사례로 만든 과제의 수정 요청이 남아 있었다고 치고
    s.store.rows[s.interview_id] = replace(
        row, state=row.state.model_copy(update={"review_note": "이전 과제 수정 요청"})
    )
    picked = (await choose(s, "public_fixed")).json()
    assert picked["review_note"] is None
    assert picked["incident_messages"] == messages
    back = await choose(s, "known_problem")
    assert back.status_code == 200, back.text
    body = back.json()
    assert body["stage"] == "incident_interview" and body["incident_source"] == "known_problem"
    assert body["incident_draft"] is None and body["incident_messages"] == messages
    assert body["spec"] is None and body["competency_ids"] == [] and body["review_note"] is None
