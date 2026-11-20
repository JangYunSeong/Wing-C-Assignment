"""사례 인터뷰 D2a — 역량 선택·과제 생성·지원자 질문에 답할 범위·과제 수정 (#266). 가상 데이터만.

무거운 생성기(build_common → draft_feature_task → 요청 글 → 슬롯)는 가짜 TaskMaker 로 바꾸고, 기능형 재료 정리·답할 범위 초안은
가짜 LLM 으로 실제 프롬프트 조립을 거친다. 실제 생성기 경로는 D2 평가에서 실제 모델로 본다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import task_spec.interview.task as task_module
from task_spec.api.app import create_app
from task_spec.interview.errors import OutputRejected
from task_spec.interview.task import (
    FLAG_ANSWER_LEAK,
    FLAG_INTERVIEW_INCIDENT,
    FLAG_TOKEN_VISIBLE,
    CompetencyChoice,
    MadeTask,
    OntologyTaskMaker,
    TaskGenerationFailed,
    TaskRequest,
    answer_leak_flags,
    coding_template,
    make_task,
    prepare_coding_incident,
    pseudonymize_changes,
    questions_for,
    token_flags,
)
from task_spec.llm.backend import Completion
from task_spec.schemas import (
    Anchor,
    Artifact,
    BehavioralAnchors,
    Observable,
    PostingCompetency,
    PostingProfile,
    PublicIncident,
    TargetCompetency,
    TaskItem,
    TechEnv,
    Trap,
)
from task_spec.schemas.interview_answer import TargetedAnswerDraftOutput
from task_spec.schemas.interview_case import KnownIncidentCase
from task_spec.schemas.interview_state import TargetedQuestion
from task_spec.spec.common import FLAG_COMPANY_INCIDENT
from task_spec.spec.feature import seed_block
from task_spec.spec.incidents import load_incidents
from task_spec.spec.store import InMemoryCommonStore
from tests.api.conftest import make_settings
from tests.interview.conftest import (
    COMPETENCIES,
    FACTS,
    PROJECT,
    REPO,
    Session,
    complete_output,
    make_service,
    prompts_with_current,
)

ANSWER_LINE = "상태가 중복 저장되지 않아야 한다"  # conftest answer_output 의 기준 답안 줄 (공백·문장부호 빼면 14자)
KNOWN_ANSWER = f"[성공 기준] {ANSWER_LINE}\n[확인 방법] 같은 요청을 재시도하고 저장 결과를 대조한다"
SELECTED = ["comp:api", "comp:retry"]
PROFILE = PostingProfile(
    posting_id="p",
    ontology_version="o",
    competencies=[
        PostingCompetency(competency_id=cid, label=label, primary=True)
        for cid, label in COMPETENCIES[:2]
    ],
)


def slot_task(profile: PostingProfile, scenario: str) -> TaskItem:
    return TaskItem(
        task_id="s1",
        track="extension_slot",
        target_competencies=[
            TargetCompetency(competency_id=c.competency_id, label=c.label)
            for c in profile.competencies[:1]
        ],
        tech_env=TechEnv(languages=["Java"], basis="직군 기본 코딩 템플릿"),
        difficulty_target="medium",
        time_limit_min=20,
        title="가상 확장 문항",
        scenario=scenario,
    )


def fake_task(
    profile: PostingProfile,
    task_id: str,
    *,
    scenario: str,
    source_incident: str | None = None,
    slots: list[TaskItem] | None = None,
) -> TaskItem:
    return TaskItem(
        task_id=task_id,
        track="common",
        task_kind="feature",
        target_competencies=[
            TargetCompetency(competency_id=c.competency_id, label=c.label)
            for c in profile.competencies
        ],
        tech_env=TechEnv(languages=["Java"], basis="직군 기본 코딩 템플릿"),
        difficulty_target="medium",
        time_limit_min=60,
        title="가상 배송 상태 기록 기능",
        scenario=scenario,
        invariant="동일 요청을 다시 처리하지 않는다",
        artifacts=[
            Artifact(artifact_id="a1", title="재시도 기록", content="가상 기록", role="key"),
            Artifact(artifact_id="a2", title="저장 결과", content="가상 결과", role="support"),
            Artifact(artifact_id="a3", title="배포 메모", content="가상 메모", role="distractor"),
        ],
        observables=[Observable(kind="first_mention_of", value="요청 식별자 검증")],
        traps=[Trap(trap_id="t1", description="재시도 금지", why_ai_fails="복구를 막음")],
        behavioral_anchors=BehavioralAnchors(
            level_1=Anchor(behavior="가상 수준 1", criterion_ids=["c1"]),
            level_3=Anchor(behavior="가상 수준 3", criterion_ids=["c1"]),
            level_5=Anchor(behavior="가상 수준 5", criterion_ids=["c1"]),
        ),
        source_incident=source_incident,
        slots=list(slots or []),
        # 생성기는 co: 사례에 '공유 승인된 회사 사례' 표시를 붙인다 — 서비스가 인터뷰 사례 표시로 바꿔야 한다
        review_flags=[FLAG_COMPANY_INCIDENT] if source_incident else [],
    )


def known_incident(answer: str = KNOWN_ANSWER) -> PublicIncident:
    base = load_incidents(REPO / "incidents" / "public_v1")["sw.backend"]
    return base.model_copy(
        update={
            "incident_id": "co:sw.backend:0123456789ab",
            "known_incident_cases": [
                KnownIncidentCase(
                    case_id="00000000-0000-4000-8000-000000000001",
                    interview_text="가상 인터뷰 대화",
                    answer_text=answer,
                    is_trap=False,
                )
            ],
        }
    )


@dataclass
class FakeTaskMaker:
    scenario: str = "가상 배송 서비스에서 같은 상태가 두 번 기록된다는 문의가 들어왔다."
    slot_scenario: str | None = None
    fail: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def competencies(self, profile: PostingProfile) -> CompetencyChoice:
        return CompetencyChoice(
            available=list(profile.competencies),
            matches=[],
            suggested=[c.competency_id for c in profile.competencies[:2]],
        )

    async def feature(self, req: TaskRequest) -> MadeTask:
        self.calls.append(
            {
                "competencies": [c.competency_id for c in req.profile.competencies],
                "incident": req.coding_incident,
                "template": req.template.manifest.template_id,
                "task_id": req.task_id,
                "reviewer_note": req.reviewer_note,
            }
        )
        if self.fail is not None:
            raise self.fail
        task = fake_task(
            req.profile,
            req.task_id,
            scenario=self.scenario,
            source_incident=req.coding_incident.incident_id,
        )
        return MadeTask(task=task, flags=["가상 생성기 표시"])

    async def request(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        return made

    async def slots(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        if not self.slot_scenario:
            return made
        slots = [slot_task(req.profile, self.slot_scenario)]
        return MadeTask(
            task=made.task.model_copy(update={"slots": slots}),
            provenance=made.provenance,
            flags=made.flags,
        )


def feature_seed_output(**updates: Any) -> str:
    seed = load_incidents(REPO / "incidents" / "public_v1")["sw.backend"].feature
    assert seed is not None
    return seed.model_copy(update={"harness": [], "reviewed": True, **updates}).model_dump_json()


def draft(text: str) -> Completion:
    return Completion(TargetedAnswerDraftOutput(answer=text).model_dump_json())


@pytest.fixture
async def task_session(tmp_path: Path, detectors) -> Any:
    maker = FakeTaskMaker()
    common = InMemoryCommonStore()
    service, backend, store, postings, clock = make_service(
        tmp_path, detectors, tasks=maker, common_store=common
    )
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Session(client, backend, service, store, postings, tmp_path, clock), maker, common


async def confirmed(session: Session) -> dict[str, Any]:
    await session.start()
    session.push(complete_output())
    assert (await session.post("project", text=PROJECT)).status_code == 200
    trapped = await session.post(
        "trap",
        attempted_fix=FACTS["naive_fix"],
        observed_result="복구가 막힘",
        why_failed=FACTS["why_naive_fails"],
    )
    assert trapped.status_code == 200, trapped.text
    state = await session.success_step()
    response = await session.post("incident/confirm", incident=state["incident_draft"])
    assert response.status_code == 200, response.text
    return response.json()


async def generated(session: Session) -> dict[str, Any]:
    await confirmed(session)
    selected = await session.post("competencies", competency_ids=SELECTED)
    assert selected.status_code == 200, selected.text
    session.backend.push(Completion(feature_seed_output()))
    response = await session.post("generate", competency_ids=SELECTED)
    assert response.status_code == 200, response.text
    return response.json()


# ================================================================ 생성


async def test_generate_makes_an_interview_common_task_from_the_confirmed_case(
    task_session,
) -> None:
    session, maker, common = task_session
    state = await confirmed(session)
    assert state["stage"] == "competencies"
    preview = (await session.client.get(f"/interviews/{session.interview_id}/competencies")).json()
    assert preview["template_id"] == "backend/java-spring-boot3-jpa"
    assert [c["competency_id"] for c in preview["competencies"]] == [
        "comp:api",
        "comp:retry",
        "comp:test",
    ]
    assert preview["suggested_ids"] == SELECTED
    assert (
        await session.post("competencies", competency_ids=["comp:api", "comp:nope"])
    ).status_code == 422
    assert (await session.post("competencies", competency_ids=SELECTED)).status_code == 200
    session.backend.push(Completion(feature_seed_output()))
    calls_before = len(session.backend.prompts)
    response = await session.post("generate", competency_ids=SELECTED)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stage"] == "task_review" and body["review_note"] is None
    spec = body["spec"]
    assert (
        spec["competency_ids"] == SELECTED
        and spec["template_id"] == "backend/java-spring-boot3-jpa"
    )
    coding = PublicIncident.model_validate(spec["coding_incident"])
    assert coding.incident_id == body["incident_id"] and coding.feature is not None
    assert coding.templates == ["backend/java-spring-boot3-jpa"] and not coding.feature.reviewed
    assert (
        coding.known_incident_cases
        == PublicIncident.model_validate(spec["incident"]).known_incident_cases
    )
    # 기능형 재료 프롬프트에는 가명화한 사례와 기준 답안이 간다 (원문 회사명 없이)
    seed_prompt = session.backend.prompts[calls_before]
    assert (
        ANSWER_LINE in seed_prompt
        and "기준 답안 (채점자용)" in seed_prompt
        and "가상테크" not in seed_prompt
    )
    call = maker.calls[-1]
    assert call["competencies"] == SELECTED and call["template"] == "backend/java-spring-boot3-jpa"
    assert "90일 내 기대 성과" in call["reviewer_note"]
    assert [q["question"] for q in spec["general_questions"]] == [
        "원인을 어떻게 찾나요?",
        "어떤 자료를 먼저 봐야 하나요?",
    ]
    assert spec["targeted_questions"][0]["hidden_condition"] == "요청 식별자 검증"
    assert spec["hidden_requirements"] == ["동일 요청을 다시 처리하지 않는다", "요청 식별자 검증"]
    assert spec["evaluation_criteria"] == [f"수준 {n}: 가상 수준 {n}" for n in (1, 3, 5)]
    # 인터뷰 사례는 공유 승인 전 — '공유 승인된 회사 사례' 표시를 인터뷰 사례 표시로 바꾼다
    task_flags = spec["common_task"]["review_flags"]
    assert FLAG_INTERVIEW_INCIDENT in task_flags and FLAG_COMPANY_INCIDENT not in task_flags
    assert spec["common_task"]["source_incident"] == coding.incident_id
    assert "가상 생성기 표시" in spec["review_flags"]
    assert not any(
        f.startswith((FLAG_ANSWER_LEAK, FLAG_TOKEN_VISIBLE)) for f in spec["review_flags"]
    )
    # 공통 과제는 interview:{id} 키의 초안으로 저장 — 승인은 D2b 그래프
    stored = next(r for r in common.rows if r["id"] == spec["common_task"]["task_id"])
    assert (
        stored["key"].prompt_ref == f"interview:{session.interview_id}"
        and stored["status"] == "draft"
    )


@pytest.mark.parametrize(
    ("fail", "status", "code"),
    [
        (TaskGenerationFailed("선택 역량의 과제 근거가 부족합니다"), 422, "interview_invalid"),
        (OutputRejected("과제 초안을 만들지 못했습니다"), 503, "service_unavailable"),
        (ValueError("입력 조각이 담긴 라이브러리 오류"), 503, "service_unavailable"),
    ],
)
async def test_generate_separates_a_bad_selection_from_retryable_failures(
    task_session, fail: Exception, status: int, code: str
) -> None:
    session, maker, common = task_session
    await confirmed(session)
    assert (await session.post("competencies", competency_ids=SELECTED)).status_code == 200
    maker.fail = fail
    session.backend.push(Completion(feature_seed_output()))
    response = await session.post("generate", competency_ids=SELECTED)
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code and "입력 조각" not in error["message"]
    assert common.rows == []  # 실패하면 초안을 저장하지 않는다


async def test_feature_seed_outside_the_template_is_retried_not_saved(task_session) -> None:
    session, _, common = task_session
    await confirmed(session)
    assert (await session.post("competencies", competency_ids=SELECTED)).status_code == 200
    session.backend.push(Completion(feature_seed_output(reasons=["가상_없는_사유"])))
    response = await session.post("generate", competency_ids=SELECTED)
    assert response.status_code == 503 and response.json()["error"]["code"] == "service_unavailable"
    assert common.rows == [] and session.store.rows[session.interview_id].state.spec is None


async def test_posting_without_a_coding_template_cannot_choose_competencies(task_session) -> None:
    session, _, _ = task_session
    await confirmed(session)
    session.postings.sub = "sw.embedded"  # 기본 코딩 템플릿이 없는 직군 (가상)
    preview = await session.client.get(f"/interviews/{session.interview_id}/competencies")
    assert preview.status_code == 422 and "템플릿" in preview.json()["error"]["message"]
    assert (await session.post("competencies", competency_ids=SELECTED)).status_code == 422


def test_an_unreadable_template_is_reported_without_its_path(monkeypatch) -> None:
    def broken(template_id: str) -> Any:
        raise OSError("/srv/private/templates/manifest.yaml")

    monkeypatch.setattr(task_module, "load_template", broken)
    with pytest.raises(TaskGenerationFailed) as err:
        coding_template("sw.backend")
    assert "/srv" not in str(err.value)
    with pytest.raises(TaskGenerationFailed):
        coding_template("sw.embedded")


async def test_feature_seed_needs_confirmed_reference_answers() -> None:
    without = known_incident().model_copy(update={"known_incident_cases": []})
    blank = known_incident(answer=" ")
    for incident in (without, blank):
        with pytest.raises(TaskGenerationFailed):  # 모델을 부르기 전에 멈춘다
            await prepare_coding_incident(
                incident,
                coding_template("sw.backend"),
                interview=None,  # type: ignore[arg-type]
                llm=None,  # type: ignore[arg-type]
                prompts=None,  # type: ignore[arg-type]
                ontology_version="o",
            )


async def test_ontology_maker_gives_reference_answers_only_to_its_own_generation(
    monkeypatch,
) -> None:
    """실제 생성기 묶음의 배선 — 인터뷰 공고 자신의 생성만 기준 답안을 기능형 재료 블록에 넣고(with_known_answers),
    기술 환경은 직군 템플릿을 따르며, 고른 역량이 빠진 과제는 선택을 바꾸라는 오류다."""
    seen: dict[str, Any] = {}

    async def build_common(profile: PostingProfile, *args: Any, **kwargs: Any) -> Any:
        seen["incidents"] = kwargs["incidents"]
        task = fake_task(PROFILE, kwargs["task_id"], scenario="가상")
        return SimpleNamespace(task=task, ctx=SimpleNamespace(env=None), flags=["가상 골격"])

    async def draft_feature_task(ctx: Any, **kwargs: Any) -> Any:
        seen["answers"] = ctx.with_known_answers
        return SimpleNamespace(output=object(), provenance=[])

    async def draft_analysis_request(task: Any, ctx: Any, **kwargs: Any) -> Any:
        seen["request_ctx"] = (ctx.with_known_answers, ctx.env.basis)
        return SimpleNamespace(provenance=[])

    async def build_slots(profile: Any, matches: Any, task: Any, ctx: Any, **kwargs: Any) -> Any:
        seen["slots_ctx"] = (ctx.with_known_answers, ctx.env.basis)
        return SimpleNamespace(slots=[], provenance=[], flags=["가상 슬롯"])

    monkeypatch.setattr(task_module, "build_common", build_common)
    monkeypatch.setattr(task_module, "draft_feature_task", draft_feature_task)
    monkeypatch.setattr(task_module, "apply_feature", lambda task, result: task)
    monkeypatch.setattr(task_module, "draft_analysis_request", draft_analysis_request)
    monkeypatch.setattr(task_module, "apply_analysis", lambda task, analysis: task)
    monkeypatch.setattr(task_module, "build_slots", build_slots)
    maker = OntologyTaskMaker(ontology=None, llm=None, prompts=None)  # type: ignore[arg-type]
    template = coding_template("sw.backend")
    incident = known_incident()
    req = TaskRequest(
        profile=PROFILE.model_copy(update={"sub": "sw.backend"}),
        matches=[],
        coding_incident=incident,
        template=template,
        task_id="t",
    )
    made = await make_task(maker, req)
    assert seen["answers"] is True and seen["incidents"] == {"sw.backend": incident}
    assert made.task.tech_env.basis == "직군 기본 코딩 템플릿" and made.flags == [
        "가상 골격",
        "가상 슬롯",
    ]
    # 단계마다 다시 만든 맥락도 같은 설정 (기준 답안·템플릿 기술 환경) — 끊긴 단계부터 이어도 같은 입력
    assert seen["request_ctx"] == seen["slots_ctx"] == (True, "직군 기본 코딩 템플릿")
    three = PROFILE.model_copy(
        update={
            "sub": "sw.backend",
            "competencies": [
                PostingCompetency(competency_id=cid, label=label, primary=True)
                for cid, label in COMPETENCIES
            ],
        }
    )
    with pytest.raises(TaskGenerationFailed):  # 생성된 과제가 고른 역량(3개) 중 일부만 잼
        await maker.feature(TaskRequest(three, [], incident, template, task_id="t"))


async def test_selecting_the_same_competencies_again_keeps_the_task(task_session) -> None:
    session, _, _ = task_session
    spec = (await generated(session))["spec"]
    revision = session.revision
    again = await session.post("competencies", competency_ids=SELECTED)
    assert again.status_code == 200 and again.json()["spec"] == spec
    assert session.revision == revision  # 저장하지 않음 (두 번 누름 등)
    other = await session.post("competencies", competency_ids=["comp:api", "comp:test"])
    assert other.status_code == 200 and other.json()["spec"] is None


# ================================================================ 누설·토큰 검사


def test_leak_check_covers_every_visible_text_but_not_grading_fields() -> None:
    incident = known_incident()
    clean = fake_task(PROFILE, "t", scenario="가상 배송 서비스 문의")
    assert answer_leak_flags(clean, incident) == []
    # 채점·승인용 칸(유지할 구조·함정·행동 기준)은 지원자에게 보이지 않는다
    hidden = clean.model_copy(
        update={
            "invariant": ANSWER_LINE,
            "traps": [Trap(trap_id="t1", description=ANSWER_LINE, why_ai_fails="가상")],
        }
    )
    assert answer_leak_flags(hidden, incident) == []
    # 짧은 겹침(공백·문장부호 빼고 14자 미만)은 표시하지 않는다
    short = clean.model_copy(update={"scenario": "상태가 중복 저장되면 곤란하다"})
    assert answer_leak_flags(short, incident) == []
    # 확장 슬롯 — 줄바꿈이 끼어도 잡는다
    with_slot = clean.model_copy(
        update={"slots": [slot_task(PROFILE, "슬롯 상황: 상태가 중복\n저장되지 않아야 한다")]}
    )
    [flag] = answer_leak_flags(with_slot, incident)
    assert flag.startswith(FLAG_ANSWER_LEAK) and "slots[s1].scenario" in flag
    # 자료 내용·짚은 질문의 답 (지원자가 정확히 물으면 그대로 준다)
    in_artifact = clean.model_copy(
        update={
            "artifacts": [
                *clean.artifacts[:2],
                Artifact(
                    artifact_id="a3", title="메모", content=f"- {ANSWER_LINE}", role="support"
                ),
            ]
        }
    )
    [flag] = answer_leak_flags(in_artifact, incident)
    assert "artifacts[a3].content" in flag
    answered = [
        TargetedQuestion(
            question_id="q1",
            question="가상 질문",
            hidden_condition="가상 조건",
            answer=f"네, {ANSWER_LINE}",
        )
    ]
    [flag] = answer_leak_flags(clean, incident, answered)
    assert "targeted_questions[q1].answer" in flag
    # 기준 답안이 없는 사례는 검사할 것이 없다
    assert (
        answer_leak_flags(with_slot, incident.model_copy(update={"known_incident_cases": []})) == []
    )


def test_tokens_left_in_visible_text_are_flagged() -> None:
    task = fake_task(PROFILE, "t", scenario="[ORG_1] 의 배송 서비스")
    [flag] = token_flags(task)
    assert flag.startswith(FLAG_TOKEN_VISIBLE) and "scenario" in flag
    hidden_only = task.model_copy(update={"scenario": "가상 배송", "invariant": "[ORG_1] 규칙"})
    assert token_flags(hidden_only) == []  # 숨은 칸은 지원자에게 보이지 않는다


def test_seed_block_carries_reference_answers_only_when_asked() -> None:
    """기준 답안은 인터뷰 공고 자신의 생성에만 — 같은 사례를 다른 공고가 쓰는 기본 경로에는 없다."""
    incident = known_incident()
    assert ANSWER_LINE not in seed_block(incident)
    with_answers = seed_block(incident, answers=True)
    assert "기준 답안 (채점자용)" in with_answers and ANSWER_LINE in with_answers


def test_questions_follow_artifacts_and_hidden_conditions() -> None:
    task = fake_task(PROFILE, "t", scenario="가상")
    general, targeted = questions_for(task)
    assert [q.artifact_ids for q in general] == [["a1", "a2", "a3"], ["a1"]]
    assert [(q.question_id, q.hidden_condition) for q in targeted] == [("q1", "요청 식별자 검증")]
    assert questions_for(task.model_copy(update={"artifacts": []})) == ([], [])


def test_pseudonymize_changes_touches_only_changed_text() -> None:
    seen: list[str] = []

    def fake(text: str) -> str:
        seen.append(text)
        return f"<{text}>"

    old = {
        "title": "그대로",
        "artifacts": [
            {"artifact_id": "a1", "content": "자료 1"},
            {"artifact_id": "a2", "content": "자료 2"},
        ],
    }
    new = {
        "title": "그대로",
        "artifacts": [
            {"artifact_id": "a2", "content": "자료 2"},  # 순서만 바뀜 — id 로 짝짓는다
            {"artifact_id": "a1", "content": "고친 자료"},
            {"artifact_id": "a3", "content": "새 자료"},
        ],
        "새 칸": "값",
    }
    out = pseudonymize_changes(new, old, fake)
    assert out["title"] == "그대로" and out["artifacts"][0] == {
        "artifact_id": "a2",
        "content": "자료 2",
    }
    assert out["artifacts"][1] == {"artifact_id": "a1", "content": "<고친 자료>"}
    # 새 항목과 새 dict 키는 키까지 전부 가명화한다 (키도 글)
    assert out["artifacts"][2] == {"<artifact_id>": "<a3>", "<content>": "<새 자료>"}
    assert out["<새 칸>"] == "<값>"
    assert "그대로" not in seen and "자료 2" not in seen


# ================================================================ 답할 범위


async def test_answers_are_pseudonymized_and_checked_for_leaks(task_session) -> None:
    session, _, _ = task_session
    await generated(session)
    saved = await session.post(
        "answers", answers={"q1": "ops@example.com 담당에게 확인한 범위까지만 알려 준다"}
    )
    assert saved.status_code == 200, saved.text
    spec = saved.json()["spec"]
    answer = spec["targeted_questions"][0]["answer"]
    assert answer.startswith("[CONTACT_1]") and "@" not in answer
    # 답은 지원자에게 그대로 나간다 — 남은 토큰을 표시
    assert any(
        f.startswith(FLAG_TOKEN_VISIBLE) and "targeted_questions[q1].answer" in f
        for f in spec["review_flags"]
    )
    leaked = await session.post("answers", answers={"q1": f"네, {ANSWER_LINE}는 조건입니다"})
    flags = leaked.json()["spec"]["review_flags"]
    assert any(
        f.startswith(FLAG_ANSWER_LEAK) and "targeted_questions[q1].answer" in f for f in flags
    )
    assert not any(f.startswith(FLAG_TOKEN_VISIBLE) for f in flags)
    fixed = await session.post(
        "answers", answers={"q1": "자료로 확인할 수 있는 범위까지만 알려 준다"}
    )
    assert not any(
        f.startswith((FLAG_ANSWER_LEAK, FLAG_TOKEN_VISIBLE))
        for f in fixed.json()["spec"]["review_flags"]
    )
    assert "가상 생성기 표시" in fixed.json()["spec"]["review_flags"]
    assert (await session.post("answers", answers={"q9": "없는 질문"})).status_code == 422
    assert (await session.post("answers", answers={"q1": "가" * 1001})).status_code == 422
    assert (await session.post("answers", answers={})).status_code == 422


async def test_targeted_answer_draft_retries_on_artifact_ids_and_copied_answers(
    task_session,
) -> None:
    session, _, _ = task_session
    await generated(session)
    revision = session.revision
    question = "요청 식별자를 꼭 검증해야 하나요?"
    session.backend.push(draft("a2 자료를 보면 된다"), draft("저장 결과 자료로 확인할 수 있습니다"))
    first = await session.post("answers/q1/draft", question=question)
    assert first.status_code == 200, first.text
    assert first.json() == {"question_id": "q1", "answer": "저장 결과 자료로 확인할 수 있습니다"}
    session.backend.push(
        draft(f"{ANSWER_LINE}는 꼭 지켜야 합니다"), draft("요청 기록으로 확인합니다")
    )
    second = await session.post("answers/q1/draft", question=question)
    assert second.json()["answer"] == "요청 기록으로 확인합니다"
    session.backend.push(draft(ANSWER_LINE), draft(f"다시 말해 {ANSWER_LINE}"))
    failed = await session.post("answers/q1/draft", question=question)
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "service_unavailable"
    assert session.revision == revision  # 초안은 저장하지 않는다


# ================================================================ 과제·질문 고치기


async def test_revise_common_pseudonymizes_edits_and_recomputes_criteria(task_session) -> None:
    session, maker, common = task_session
    maker.slot_scenario = "가상 확장 문항 상황"
    spec = (await generated(session))["spec"]
    assert (
        await session.post("answers", answers={"q1": "자료로 확인할 수 있는 범위만"})
    ).status_code == 200
    task = copy.deepcopy(spec["common_task"])
    task["scenario"] = "ops@example.com 으로 들어온 가상 문의를 처리한다"
    task["behavioral_anchors"]["level_1"]["behavior"] = "가상 수준 1 (고침)"
    response = await session.post("revise", target="common", note="상황 수정", task=task)
    assert response.status_code == 200, response.text
    body = response.json()["spec"]
    edited = body["common_task"]
    assert (
        edited["scenario"].startswith("[CONTACT_1]")
        and edited["title"] == spec["common_task"]["title"]
    )
    assert body["evaluation_criteria"][0] == "수준 1: 가상 수준 1 (고침)"
    # 자료·숨은 조건이 그대로면 담당자가 정한 답과 질문을 둔다
    assert body["targeted_questions"][0]["answer"] == "자료로 확인할 수 있는 범위만"
    assert body["general_questions"] == spec["general_questions"]
    assert any(f.startswith(FLAG_TOKEN_VISIBLE) and "scenario" in f for f in body["review_flags"])
    stored = next(r for r in common.rows if r["id"] == edited["task_id"])
    assert TaskItem.model_validate(stored["task"]).scenario == edited["scenario"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("target_competencies", lambda t: t["target_competencies"][:1]),
        ("tech_env", lambda t: {**t["tech_env"], "languages": ["Kotlin"]}),
        ("source_incident", lambda t: "pub:sw.backend:gasang"),
        ("slots", lambda t: []),
        ("behavioral_anchors", lambda t: None),
    ],
)
async def test_revise_common_keeps_the_frame(task_session, name: str, value: Any) -> None:
    session, maker, common = task_session
    maker.slot_scenario = "가상 확장 문항 상황"
    spec = (await generated(session))["spec"]
    rows = len(common.rows)
    task = copy.deepcopy(spec["common_task"])
    task[name] = value(task)
    response = await session.post("revise", target="common", note="뼈대 변경", task=task)
    assert response.status_code == 422, response.text
    assert len(common.rows) == rows  # 새 판을 저장하지 않는다


async def test_revising_the_leaking_text_clears_the_leak_flag(task_session) -> None:
    session, maker, _ = task_session
    maker.scenario = f"지원자는 다음을 지켜야 한다: {ANSWER_LINE}."
    spec = (await generated(session))["spec"]
    assert any(f.startswith(FLAG_ANSWER_LEAK) and "scenario" in f for f in spec["review_flags"])
    task = {**spec["common_task"], "scenario": "가상 배송 서비스에서 상태 기록 문의가 들어왔다."}
    response = await session.post("revise", target="common", note="누설 문장 고침", task=task)
    assert response.status_code == 200, response.text
    flags = response.json()["spec"]["review_flags"]
    assert not any(f.startswith(FLAG_ANSWER_LEAK) for f in flags)
    assert "가상 생성기 표시" in flags and FLAG_INTERVIEW_INCIDENT in flags


async def test_an_edit_that_overflows_after_pseudonymization_names_only_the_field(
    task_session,
) -> None:
    session, _, _ = task_session
    spec = (await generated(session))["spec"]
    text = "가" * 293 + " x@y.io"  # 300자 — 가명화하면 [CONTACT_1] 로 늘어 305자
    task = {**spec["common_task"], "requirements": [{"req_id": "r1", "text": text}]}
    response = await session.post("revise", target="common", note="요구 추가", task=task)
    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert "requirements.0.text" in message and "x@y.io" not in message and "가가" not in message


async def test_revise_questions_resets_answers(task_session) -> None:
    session, _, _ = task_session
    spec = (await generated(session))["spec"]
    await session.post("answers", answers={"q1": f"네, {ANSWER_LINE}"})
    targeted = [{**spec["targeted_questions"][0], "question": "식별자 검증 범위를 물으면?"}]
    response = await session.post(
        "revise", target="questions", note="질문 다듬기", targeted_questions=targeted
    )
    assert response.status_code == 200, response.text
    body = response.json()["spec"]
    q = body["targeted_questions"][0]
    assert q["question"] == "식별자 검증 범위를 물으면?" and q["answer"] is None
    assert not any(
        f.startswith(FLAG_ANSWER_LEAK) for f in body["review_flags"]
    )  # 지운 답의 표시도 뺀다
    wrong = [{**spec["general_questions"][0], "artifact_ids": ["a9"]}]
    assert (
        await session.post("revise", target="questions", note="자료 바꿈", general_questions=wrong)
    ).status_code == 422


async def test_going_back_drops_the_generated_task(task_session) -> None:
    session, _, _ = task_session
    await generated(session)
    response = await session.post("revise", target="success", note="성공 기준 보완")
    assert response.status_code == 200
    body = response.json()
    assert (
        body["spec"] is None
        and body["competency_ids"] == []
        and body["stage"] == "success_interview"
    )


async def test_generate_is_not_ready_without_feature_seed_current(
    tmp_path: Path, detectors
) -> None:
    root = tmp_path / "prompts"
    prompts = prompts_with_current(tmp_path)
    (root / "interview_feature_seed" / "CURRENT").unlink()
    service, backend, store, postings, clock = make_service(
        tmp_path,
        detectors,
        prompts=prompts,
        tasks=FakeTaskMaker(),
        common_store=InMemoryCommonStore(),
    )
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = Session(client, backend, service, store, postings, tmp_path, clock)
        await confirmed(session)
        assert (await session.post("competencies", competency_ids=SELECTED)).status_code == 200
        response = await session.post("generate", competency_ids=SELECTED)
    assert response.status_code == 503 and response.json()["error"]["code"] == "interview_not_ready"
