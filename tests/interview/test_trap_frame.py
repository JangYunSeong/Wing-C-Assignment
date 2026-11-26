"""함정 틀 (#318, interview/trap_frame) — 허용 종류·틀 입력 블록·틀 적용 재료·틀 검사와 표시. 가상 데이터만, LLM 없음.

틀 적용 재료는 틀이 바꾸는 칸만 덮어쓴 재료 사본이다 (docs/design/trap-frame.md §3.2) — 명세 초안은 숨은 요구·허용 설계를 재료에서
코드로 복사하고(fill_from_seed_v4), 계약 단계는 재료의 사유·필요 요소·금지 낱말을 쓰므로 재료만 바꾸면 둘 다 따라온다.
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
from task_spec.interview.frame_leaks import FLAG_FRAME_LEAK
from task_spec.interview.trap_frame import (
    FLAG,
    allowed_kinds,
    apply_frame,
    frame_block,
    frame_flags,
    frame_problems,
    keeps_hidden,
)
from task_spec.llm.backend import Completion
from task_spec.prompting import PromptRegistry
from task_spec.schemas import FeatureSeed, MaterialAnalysis, TrapFrame
from task_spec.schemas.interview_state import ReinforceAnswer
from task_spec.spec.store import InMemoryCommonStore
from task_spec.starter.templates import load_template
from tests.api.conftest import make_settings
from tests.interview.conftest import REPO, Session, make_service
from tests.interview.test_material_analysis import _analysis_prompts
from tests.interview.test_task import (
    SELECTED,
    FakeTaskMaker,
    confirmed,
    feature_seed_output,
    known_incident,
)

JAVA = load_template("backend/java-spring-boot3-jpa")
SEED = FeatureSeed.model_validate_json(feature_seed_output())
# design 틀의 숨은 요구 = 재료 숨은 요구 그대로 + 설계 조건 한 문장 (trap_frame v2)
HIDDEN = SEED.hidden_requirement + " 세션 확인 경로는 요청마다 DB 연결을 쥐지 않는다."


def _frame(**over: Any) -> TrapFrame:
    base: dict[str, Any] = {
        "kind": "design",
        "hidden_requirement": HIDDEN,
        "public_rules": ["DB 연결 풀은 20개, 피크에 동시 요청 100건"],
        "forbidden_terms": ["세션 테이블", "커넥션 풀"],
        "forbidden_storage": [{"entity": "Session", "why": "흔한 구현이 세션을 DB 테이블에 둔다"}],
        "stateful_target_forbidden": True,
        "stateful_target_why": "세션은 로그인 요청이 만든다 — 미리 채운 테이블이 아니다",
        "design_matrix": [
            {"design": SEED.accepted_designs[0], "holds": "yes", "why": "가상 이유"},
            {
                "design": SEED.accepted_designs[1],
                "holds": "no",
                "requirement": "접근할 때 만료 연장",
                "why": "토큰에 만료가 고정됨",
            },
        ],
        "reason": "pool_acquire_timeout",
        "evidence": ["요청마다 DB 연결을 쥔 채 세션을 조회"],
        "gaps": ["요청 하나가 DB 에 가는 횟수"],
    }
    return TrapFrame.model_validate(base | over)


def test_only_design_frames_until_the_template_can_measure_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D3: 틀은 하나, 종류는 코드가 템플릿을 보고 허용한다. v5 에는 B 를 잴 사유가 없어 design 만 — v6 에 생기면 B 도."""
    assert allowed_kinds(JAVA) == ["design"]
    monkeypatch.setattr(tf, "BEHAVIOR_REASONS", frozenset({"server_error"}))
    assert allowed_kinds(JAVA) == ["design", "behavior"]


def test_the_frame_input_adds_business_need_needs_analysis_and_answers() -> None:
    analysis = MaterialAnalysis.model_validate(
        {
            "mechanism": "요청마다 연결을 쥔 채 기다림",
            "reason": "pool_acquire_timeout",
            "reproducible": "yes",
            "gaps": ["가상 빈칸"],
        }
    )
    answers = [ReinforceAnswer(question_id="r1", question="가상 질문", answer="가상 답")]
    coding = known_incident().model_copy(update={"feature": SEED})
    block = frame_block(coding, analysis, answers)
    assert block["business_need"] == SEED.business_need
    assert {n["kind"] for n in block["contract_needs"]} >= {"stateful_target"}
    assert block["analysis"]["reason"] == "pool_acquire_timeout"
    assert block["reinforcement"] == [{"question": "가상 질문", "answer": "가상 답"}]
    shared = frame_block(coding, analysis, answers, shared=True)
    assert (
        "notes" not in shared and "reinforcement" not in shared
    )  # 공유 회사 사례 — 등록 규칙의 LLM 입력 칸만


def test_the_framed_material_overwrites_only_what_the_frame_decides() -> None:
    """§3.2: 숨은 요구 ← 틀 · 사유 ← 틀 사유(조절값은 템플릿 필수값) · 상태 대상 요구를 뺌(D4, 전이 규칙도) · 금지 낱말을 더함.
    업무 요구·2단계 칸·허용 설계는 그대로 (사람이 빼지 않았다)."""
    framed, notes = apply_frame(SEED, _frame())
    assert framed.hidden_requirement == HIDDEN
    assert framed.reasons == ["pool_acquire_timeout"] and framed.harness is None
    assert {n.kind for n in framed.contract_needs} == {"empty_record_entity"}
    assert framed.leak_terms[: len(SEED.leak_terms)] == SEED.leak_terms
    assert "세션 테이블" in framed.leak_terms and framed.leak_terms.count("커넥션 풀") == 1
    assert (
        framed.business_need == SEED.business_need and framed.stage2_symptom == SEED.stage2_symptom
    )
    assert framed.accepted_designs == SEED.accepted_designs and framed.trigger == SEED.trigger
    assert any("상태 대상 요구를 뺌" in n for n in notes) and any(
        "실패 사유를 틀 것" in n for n in notes
    )
    # 상태 대상이 흔한 구현의 저장 방식이 아니면 재료 요구 그대로
    kept, _ = apply_frame(SEED, _frame(stateful_target_forbidden=False))
    assert kept.contract_needs == SEED.contract_needs


def test_a_design_frame_cannot_drop_the_conditions_of_the_material_hidden_requirement() -> None:
    """2026-11-25 trap_frame v1 평가: 평범한 재료의 숨은 요구를 다시 쓰며 조건을 뺐다 — design 틀은 재료 숨은 요구를 글자 그대로
    담아야 틀 것을 쓴다 (띄어쓰기·문장 부호 차이는 같게). 아니면 재료 것을 그대로 두고 검토 표시로 남긴다."""
    first = SEED.hidden_requirement.split(".")[0]
    dropped = _frame(
        hidden_requirement=first + ". 세션 확인 경로는 요청마다 DB 연결을 쥐지 않는다."
    )
    assert not keeps_hidden(SEED, dropped)
    framed, notes = apply_frame(SEED, dropped)
    assert framed.hidden_requirement == SEED.hidden_requirement
    assert any("숨은 요구를 재료 것으로 유지" in n for n in notes)
    assert any("재료 조건 누락" in f for f in frame_flags(dropped, notes))
    spaced = _frame(hidden_requirement=SEED.hidden_requirement.replace(" ", "  ").replace("—", "-"))
    assert keeps_hidden(SEED, spaced)
    assert apply_frame(SEED, spaced)[0].hidden_requirement == spaced.hidden_requirement
    # behavior 틀은 숨길 동작 자체를 바꾼다 — 재료 숨은 요구를 담지 않아도 틀 것
    moved = _frame(kind="behavior", hidden_requirement="의존 서비스가 실패하면 503 을 준다")
    assert apply_frame(SEED, moved)[0].hidden_requirement == "의존 서비스가 실패하면 503 을 준다"


def test_behavior_frames_rewrite_the_trigger_and_people_remove_designs() -> None:
    framed, _ = apply_frame(
        SEED,
        _frame(kind="behavior", trigger="가상 운영 조건", why_naive_breaks="가상 깨지는 이유"),
    )
    assert (framed.trigger, framed.why_naive_breaks) == ("가상 운영 조건", "가상 깨지는 이유")
    designs = [*SEED.accepted_designs, "[설계] 가상 세 번째 설계 — 조건: x — 함정: y"]
    seed3 = SEED.model_copy(update={"accepted_designs": designs})
    fewer, notes = apply_frame(seed3, _frame(), removed_designs=[designs[2][:12]])
    assert fewer.accepted_designs == designs[:2] and any("허용 설계 1개를 뺌" in n for n in notes)
    same, notes = apply_frame(SEED, _frame(), removed_designs=[SEED.accepted_designs[0]])
    assert same.accepted_designs == SEED.accepted_designs and any("둘 미만" in n for n in notes)


def test_frames_the_template_cannot_measure_are_not_used_and_flags_show_what_matters() -> None:
    assert frame_problems(_frame(), JAVA) == []
    assert any("틀 종류 behavior" in p for p in frame_problems(_frame(kind="behavior"), JAVA))
    assert any("실패 사유 nope" in p for p in frame_problems(_frame(reason="nope"), JAVA))
    _, notes = apply_frame(SEED, _frame())
    flags = frame_flags(_frame(), notes)
    assert all(f.startswith(FLAG) for f in flags)
    assert any(
        "허용 설계가 업무 요구 '접근할 때 만료 연장' 를 못 지킬 수 있음 (no)" in f for f in flags
    )
    assert any("공개 계약에 두지 않을 엔티티 — Session" in f for f in flags)
    assert any("근거 빈칸 — 요청 하나가 DB 에 가는 횟수" in f for f in flags)


# ── 인터뷰 과제 만들기 연결 ─────────────────────────────────────────


@pytest.fixture
async def frame_env(
    tmp_path: Path, detectors: Any
) -> AsyncIterator[tuple[Session, FakeTaskMaker, InMemoryCommonStore]]:
    """재료 분석·함정 틀 프롬프트를 CURRENT 로 둔 인터뷰 서비스 — 인터뷰 테스트 레지스트리(prompts_with_current)는 인터뷰
    프롬프트만 담아 틀 단계를 건너뛰므로, 레포 CURRENT 판을 따로 넣는다 (trap_frame 첫 CURRENT v3, 2026-11-26)."""
    _analysis_prompts(tmp_path)
    src, dst = REPO / "prompts" / tf.TASK, tmp_path / "prompts" / tf.TASK
    dst.mkdir()
    current = (src / "CURRENT").read_text().strip()
    shutil.copy(src / f"{current}.md", dst / f"{current}.md")
    (dst / "CURRENT").write_text(f"{current}\n")
    maker, common = FakeTaskMaker(), InMemoryCommonStore()
    service, backend, store, postings, clock = make_service(
        tmp_path,
        detectors,
        prompts=PromptRegistry(tmp_path / "prompts"),
        tasks=maker,
        common_store=common,
    )
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Session(client, backend, service, store, postings, tmp_path, clock), maker, common


async def _generate_framed(session: Session, *outputs: Completion | Exception) -> dict[str, Any]:
    """기능형 재료(LLM) → 재료 분석 → 함정 틀 → 명세(가짜 생성기). 출력은 분석·틀 순서."""
    await confirmed(session)
    assert (await session.post("competencies", competency_ids=SELECTED)).status_code == 200
    session.backend.push(Completion(feature_seed_output()), *outputs)
    response = await session.generate(competency_ids=SELECTED)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stage"] == "task_review", body
    return dict(body["spec"])


ANALYSIS = Completion(
    MaterialAnalysis.model_validate(
        {
            "mechanism": "요청마다 연결을 쥔 채 기다림",
            "reason": "pool_acquire_timeout",
            "reproducible": "yes",
        }
    ).model_dump_json()
)


async def test_the_draft_uses_the_framed_material_and_the_common_task_keeps_it(
    frame_env: Any,
) -> None:
    """#318: 명세 초안은 틀 적용 재료로 (숨은 요구 ← 틀, 상태 대상 요구 뺌), 등록할 재료(spec.coding_incident)는 원 재료 그대로,
    틀 적용 재료와 틀은 공통 과제 행에 — 빌드 계약 단계가 쓴다 (D1). 틀의 판단은 검토 표시로 보인다."""
    session, maker, common = frame_env
    spec = await _generate_framed(session, ANALYSIS, Completion(_frame().model_dump_json()))
    used = maker.calls[-1]["incident"].feature
    assert (
        maker.calls[-1]["frame"] == _frame()
    )  # 공개 쪽은 명세 초안 입력으로 (draft_feature_task v13)
    assert used.hidden_requirement == _frame().hidden_requirement
    assert {n.kind for n in used.contract_needs} == {"empty_record_entity"}
    assert spec["coding_incident"]["feature"]["hidden_requirement"] == SEED.hidden_requirement
    stored = await common.framed(spec["common_task"]["task_id"])
    assert (
        stored is not None
        and stored.seed == used
        and stored.frame.forbidden_storage[0].entity == "Session"
    )
    assert any(
        f.startswith(FLAG) and "공개 계약에 두지 않을 엔티티 — Session" in f
        for f in spec["review_flags"]
    )
    assert any(p["prompt"].startswith(f"{tf.TASK}@") for p in spec["generation_provenance"])
    sent = next(p for p in session.backend.prompts if "고를 수 있는 틀 종류" in p)
    assert (
        "고를 수 있는 틀 종류: design" in sent
        and SEED.business_need in sent
        and "가상테크" not in sent
    )


async def test_a_frame_the_template_cannot_measure_falls_back_to_the_material(
    frame_env: Any,
) -> None:
    """템플릿(v5)이 잴 수 없는 틀 종류(behavior)면 원 재료로 만들고 표시만 — 틀을 저장하지 않는다."""
    session, maker, common = frame_env
    spec = await _generate_framed(
        session, ANALYSIS, Completion(_frame(kind="behavior").model_dump_json())
    )
    assert maker.calls[-1]["incident"].feature == FeatureSeed.model_validate(
        spec["coding_incident"]["feature"]
    )
    assert any("이 템플릿이 잴 수 없는 틀 종류 behavior" in f for f in spec["review_flags"])
    assert await common.framed(spec["common_task"]["task_id"]) is None


async def test_a_failed_frame_does_not_stop_the_task(frame_env: Any) -> None:
    session, maker, common = frame_env
    spec = await _generate_framed(session, ANALYSIS, ValueError("가상 틀 실패"))
    assert tf.FAILED in spec["review_flags"]
    assert maker.calls[-1]["incident"].feature == FeatureSeed.model_validate(
        spec["coding_incident"]["feature"]
    )
    assert await common.framed(spec["common_task"]["task_id"]) is None


# ── 3단계: 검사 1·2·3·5 (#318 §5) ──────────────────────────────────

THIRD = "[설계] 가상 세 번째 설계 — 조건: 가상 조건"


async def _answer_all(
    session: Session, spec: dict[str, Any], **first: str | None
) -> dict[str, Any]:
    """보강 질문에 답한다 — 첫 질문만 first 의 답, 나머지는 '모름'."""
    questions = spec["reinforce_questions"]
    answers = {q["question_id"]: None for q in questions} | {
        questions[0]["question_id"]: first["answer"]
    }
    answered = await session.post("reinforce", answers=answers)
    assert answered.status_code == 200, answered.text
    return dict(answered.json())


async def test_a_design_question_comes_first_and_removing_it_reaches_the_framed_material(
    frame_env: Any,
) -> None:
    """검사 2: 업무 요구를 못 지킬 수 있는 허용 설계를 ⑧ 에서 먼저 묻고(빈칸과 합쳐 셋), '뺀다' 면 다시 만든 틀 적용 재료와
    명세 초안에서 그 설계가 빠진다 — 공통 과제 행(빌드 계약 단계가 읽음)에도. 두 번째는 묻지 않는다."""
    session, maker, common = frame_env
    designs = [*SEED.accepted_designs, THIRD]
    matrix = [{"design": d, "holds": "yes", "why": "가상 이유"} for d in designs[:2]]
    matrix.append(
        {"design": THIRD, "holds": "no", "requirement": "가상 업무 요구", "why": "가상 기전"}
    )
    seed3 = Completion(feature_seed_output(accepted_designs=designs))
    frame = Completion(_frame(design_matrix=matrix).model_dump_json())
    await confirmed(session)
    assert (await session.post("competencies", competency_ids=SELECTED)).status_code == 200
    session.backend.push(seed3, ANALYSIS, frame)
    spec = (await session.generate(competency_ids=SELECTED)).json()["spec"]
    first = spec["reinforce_questions"][0]
    assert (first["kind"], first["design"], first["choices"]) == (
        "design",
        THIRD,
        ["뺀다", "남긴다"],
    )
    # 설계 1 + 재현 근거 1(재료 harness 비어 있고 분석 갈림 조절값 없음) + 틀 빈칸 1 — 셋까지
    assert [q["kind"] for q in spec["reinforce_questions"]] == ["design", "gap", "gap"]
    body = await _answer_all(session, spec, answer="뺀다")
    stored = body["reinforce_answers"][0]
    assert (stored["kind"], stored["choice"], stored["design"]) == ("design", "remove", THIRD)

    session.backend.push(seed3, ANALYSIS, frame)
    regenerated = await session.generate(competency_ids=SELECTED)
    assert regenerated.status_code == 200, regenerated.text
    spec2 = regenerated.json()["spec"]
    assert maker.calls[-1]["incident"].feature.accepted_designs == designs[:2]
    framed = await common.framed(spec2["common_task"]["task_id"])
    assert framed is not None and framed.seed.accepted_designs == designs[:2]
    assert spec2["coding_incident"]["feature"]["accepted_designs"] == designs  # 등록 재료는 그대로
    assert any("허용 설계 1개를 뺌 (사람 결정)" in f for f in spec2["review_flags"])
    assert spec2["reinforce_questions"] == []


async def test_removing_one_of_two_designs_sends_the_substitute_to_the_next_material(
    frame_env: Any,
) -> None:
    """허용 설계는 2개 이상이어야 한다 — 둘뿐이면 '뺀다 — <대신 인정할 설계>' 를 받아 그 글을 다음 재료 만들기 입력에 넣고,
    다시 만든 재료가 여전히 둘이면 빼지 않고 표시한다 (사람 확인)."""
    session, _, common = frame_env
    spec = await _generate_framed(session, ANALYSIS, Completion(_frame().model_dump_json()))
    first = spec["reinforce_questions"][0]
    assert first["kind"] == "design" and first["design"] == SEED.accepted_designs[1]
    assert "대신 인정할 설계" in first["question"]
    await _answer_all(session, spec, answer="뺀다 — 가상 대신 설계")
    calls = len(session.backend.prompts)
    session.backend.push(
        Completion(feature_seed_output()), ANALYSIS, Completion(_frame().model_dump_json())
    )
    spec2 = (await session.generate(competency_ids=SELECTED)).json()["spec"]
    seed_prompt = session.backend.prompts[calls]
    assert "허용 설계 확인" in seed_prompt and "가상 대신 설계" in seed_prompt
    framed = await common.framed(spec2["common_task"]["task_id"])
    assert framed is not None and framed.seed.accepted_designs == SEED.accepted_designs
    assert any("둘 미만이라 그대로 둠" in f for f in spec2["review_flags"])


async def test_an_unread_design_answer_keeps_the_design(frame_env: Any) -> None:
    session, _, common = frame_env
    spec = await _generate_framed(session, ANALYSIS, Completion(_frame().model_dump_json()))
    body = await _answer_all(session, spec, answer="글쎄요, 팀과 상의해 보겠습니다")
    assert body["reinforce_answers"][0]["choice"] is None
    session.backend.push(
        Completion(feature_seed_output()), ANALYSIS, Completion(_frame().model_dump_json())
    )
    spec2 = (await session.generate(competency_ids=SELECTED)).json()["spec"]
    framed = await common.framed(spec2["common_task"]["task_id"])
    assert framed is not None and framed.seed.accepted_designs == SEED.accepted_designs
    assert any("읽지 못해 설계를 남김" in f for f in spec2["review_flags"])


async def test_check1_and_check3_reach_the_review_flags(frame_env: Any) -> None:
    """검사 1: 틀 사유의 성립 조건 근거가 없으면 표시 · 검사 3: 명세 1단계 자료 본문에 틀이 막은 낱말이 있으면 표시 —
    틀은 명세에 남겨 담당자가 글을 고칠 때 검사 3 을 다시 계산한다 (가짜 생성기의 방해 자료 a3 본문 '가상 메모')."""
    session, _, _ = frame_env
    leaky = _frame(forbidden_terms=["가상 메모"])
    spec = await _generate_framed(session, ANALYSIS, Completion(leaky.model_dump_json()))
    flags = spec["review_flags"]
    assert any(
        f.startswith(f"{FLAG}재현 근거 빈칸") and "concurrency, pool_size" in f for f in flags
    )
    assert any(
        f.startswith(FLAG_FRAME_LEAK) and "자료 a3(방해)" in f and "가상 메모" in f for f in flags
    )
    assert spec["trap_frame"]["forbidden_terms"] == ["가상 메모"]


def test_frame_words_already_public_or_operating_conditions_are_not_added() -> None:
    """설계 검사 #332 S-F6: 틀 금지 낱말 가운데 공개된 업무 요구(감사 기록)·운영 조건 낱말(동시 요청 — 1단계 자료에 보여야 하는 단서)에
    걸리는 말은 leak_terms 에 넣지 않는다 — 검사 3 과 같은 예외. 예외 없이 합치면 명세 검사가 고칠 수 없는 재요청을 냈다."""
    framed, _ = apply_frame(SEED, _frame(forbidden_terms=["감사 기록", "동시 요청", "세션 테이블"]))
    assert "세션 테이블" in framed.leak_terms
    assert "감사 기록" not in framed.leak_terms and "동시 요청" not in framed.leak_terms


def test_removed_designs_are_named_in_the_notes() -> None:
    """설계 검사 #332 S-F4: 사람이 뺀 설계를 다시 만든 재료에서 못 찾으면 개수만이 아니라 어느 설계인지 보인다 (무엇을 확인할지),
    뺀 설계도 무엇과 맞췄는지 남긴다."""
    designs = [*SEED.accepted_designs, "[설계] 가상 세 번째 설계 — 조건: x — 함정: y"]
    seed3 = SEED.model_copy(update={"accepted_designs": designs})
    _, notes = apply_frame(
        seed3, _frame(), removed_designs=["[설계] 다시 만들며 사라진 가상 설계 — 조건: z"]
    )
    assert any(
        "다시 만든 재료에 없음" in n and "다시 만들며 사라진 가상 설계" in n for n in notes
    ), notes
    _, notes = apply_frame(seed3, _frame(), removed_designs=[designs[2][:12]])
    assert any("허용 설계 1개를 뺌 (사람 결정): 가상 세 번째 설계" in n for n in notes), notes
