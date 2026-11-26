"""함정 틀 3단계 검사 (#318, docs/design/trap-frame.md §5) — 검사 1 재현 근거 · 검사 2 설계 질문 · 검사 3 공개 중립성 · 보강 질문 한도.

가상 데이터만, LLM 없음. 서비스를 거치는 흐름(질문 → 답 → 다시 만들기)은 tests/interview/test_trap_frame.py.
"""

from __future__ import annotations

from typing import Any

from task_spec.interview.frame_leaks import FLAG_FRAME_LEAK, frame_leak_flags, storage_pattern
from task_spec.interview.material_analysis import (
    DESIGN_CHOICES,
    MAX_REINFORCE,
    design_choice,
    reinforce_questions,
)
from task_spec.interview.task import with_content_flags
from task_spec.interview.trap_frame import (
    FLAG,
    apply_frame,
    design_questions,
    evidence_gaps,
    match_design,
)
from task_spec.schemas import MaterialAnalysis, TaskItem
from tests.interview.test_task import known_incident
from tests.interview.test_trap_frame import JAVA, SEED, _frame

THIRD = "[설계] 가상 세 번째 설계 — 조건: 가상 조건"


def _analysis(*params: str) -> MaterialAnalysis:
    return MaterialAnalysis.model_validate(
        {
            "mechanism": "가상 기전",
            "reason": "pool_acquire_timeout",
            "reproducible": "yes",
            "split_params": list(params),
        }
    )


# ── 검사 1 재현 근거 ──────────────────────────────────────────────


def test_check1_reads_the_condition_of_the_frame_reason_from_the_template() -> None:
    """근거 표는 템플릿 실패 사유의 성립 조건(feasible.when) — 재료가 적은 조절값 ∪ 분석이 짚은 갈림 조절값과 대조한다."""
    gaps = evidence_gaps(
        "pool_acquire_timeout", JAVA, SEED.model_copy(update={"harness": []}), None
    )
    assert len(gaps) == 1 and gaps[0].flag.startswith(FLAG)
    assert "'concurrency >= pool_size' 중 concurrency, pool_size 근거 없음" in gaps[0].flag
    assert (
        "동시 요청 수" in gaps[0].question and "DB 연결 풀" in gaps[0].question
    )  # 조절값 설명으로 묻는다
    declared = SEED.model_copy(update={"harness": ["pool_size", "concurrency"]})
    assert evidence_gaps("pool_acquire_timeout", JAVA, declared, None) == []
    analysed = SEED.model_copy(update={"harness": None})  # 재료가 적지 않음 — 분석만 근거
    assert (
        evidence_gaps("pool_acquire_timeout", JAVA, analysed, _analysis("concurrency", "pool_size"))
        == []
    )
    half = evidence_gaps(
        "pool_acquire_timeout", JAVA, SEED.model_copy(update={"harness": ["pool_size"]}), None
    )
    assert len(half) == 1 and half[0].flag.endswith("중 concurrency 근거 없음")
    # 조절값이 아닌 이름(rows — 시드 행 수)은 보지 않는다 · 성립 조건이 없는 사유 · 템플릿에 없는 사유
    timed = SEED.model_copy(update={"harness": ["query_delay_ms", "request_timeout_s"]})
    assert evidence_gaps("request_timeout", JAVA, timed, None) == []
    assert evidence_gaps("server_error", JAVA, SEED, None) == []
    assert evidence_gaps("nope", JAVA, SEED, None) == []


# ── 검사 2 설계 질문 · 보강 질문 ────────────────────────────────────


def test_check2_asks_whether_to_remove_designs_that_cannot_keep_the_business_need() -> None:
    frame = _frame()  # d0 yes · d1 no ('접근할 때 만료 연장')
    [(question, design)] = design_questions(frame, SEED)
    assert design == SEED.accepted_designs[1]  # 재료 원문 — 다시 만들 때 이 설계를 뺀다
    assert "업무 요구 '접근할 때 만료 연장'" in question and "(뺀다/남긴다)" in question
    assert "대신 인정할 설계" in question  # 둘뿐인 재료 — 빼면 하나만 남는다
    three = SEED.model_copy(update={"accepted_designs": [*SEED.accepted_designs, THIRD]})
    [(question3, design3)] = design_questions(frame, three)
    assert design3 == SEED.accepted_designs[1] and "대신 인정할 설계" not in question3
    # yes 설계가 하나도 없으면 묻지 않는다 (다 빼면 과제가 성립하지 않는다) · 재료 설계와 못 맞추면 묻지 않는다
    none_hold = _frame(
        design_matrix=[{"design": d, "holds": "no", "why": "가상"} for d in SEED.accepted_designs]
    )
    assert design_questions(none_hold, SEED) == []
    stray = _frame(
        design_matrix=[
            {"design": SEED.accepted_designs[0], "holds": "yes", "why": "가상"},
            {"design": "[설계] 재료에 없는 설계", "holds": "partial", "why": "가상"},
        ]
    )
    assert design_questions(stray, SEED) == []


def test_questions_put_designs_first_and_stop_at_three() -> None:
    """설계 질문 → 빈칸(재현 근거 → 틀 → 분석 순으로 seed 단계가 모은 것), 합쳐 셋 [사용자 결정 D6]."""
    qs = reinforce_questions(["빈칸 1", "빈칸 2", "빈칸 3"], [("설계 질문", THIRD)])
    assert MAX_REINFORCE == 3 and [q.question_id for q in qs] == ["r1", "r2", "r3"]
    assert (qs[0].kind, qs[0].design, qs[0].choices) == ("design", THIRD, list(DESIGN_CHOICES))
    assert [q.question for q in qs[1:]] == ["빈칸 1", "빈칸 2"]
    assert all(q.kind == "gap" and q.design is None and q.choices == [] for q in qs[1:])


def test_design_answers_are_read_only_when_they_start_with_a_choice() -> None:
    assert design_choice("뺀다") == "remove"
    assert design_choice("  뺀다 — 세션을 메모리 저장소에 두는 설계") == "remove"
    assert design_choice("남긴다") == "keep"
    assert (
        design_choice("글쎄요, 빼는 게 좋겠어요") is None
    )  # 고정 답이 아니면 남긴다 (표시로 확인)
    assert design_choice(None) is None and design_choice("") is None


def test_removed_designs_match_by_their_front_even_if_the_material_was_rewritten() -> None:
    """다시 만든 재료는 설계 문구가 조금 바뀔 수 있다 — 앞부분으로 맞춰 뺀다. 둘 미만이 되면 빼지 않고 표시만."""
    three = [*SEED.accepted_designs, THIRD]
    rewritten = "[설계]  가상 세 번째  설계 — 조건: 바뀐 문구"
    assert match_design(rewritten, three) == THIRD
    assert match_design("[설계] 재료에 없는 설계", three) is None
    seed3 = SEED.model_copy(update={"accepted_designs": three})
    framed, notes = apply_frame(seed3, _frame(), [rewritten])
    assert framed.accepted_designs == three[:2] and any(
        "허용 설계 1개를 뺌 (사람 결정)" in n for n in notes
    )
    kept, notes = apply_frame(SEED, _frame(), ["[설계] 재료에 없는 설계"])
    assert kept.accepted_designs == SEED.accepted_designs
    assert any("다시 만든 재료에 없음" in n for n in notes)
    two, notes = apply_frame(SEED, _frame(), [SEED.accepted_designs[1]])
    assert two.accepted_designs == SEED.accepted_designs
    assert any("둘 미만이라 그대로 둠 — 대신 인정할 설계 필요" in n for n in notes)


# ── 검사 3 공개 중립성 ─────────────────────────────────────────────


def test_storage_patterns_catch_storage_shapes_but_not_public_fields() -> None:
    session = storage_pattern("Session")
    assert session is not None
    for shown in (
        "CREATE TABLE sessions (id BIGINT)",
        "SELECT * FROM user_sessions WHERE id = ?",
        "sessions 테이블에 저장",
        '@Table(name = "session")',
        "public class Session {",
        "SessionRepository",
        "repo.findSessionById(id)",
    ):
        assert session.search(shown), shown
    for fine in (
        "응답 칸: sessionId",
        "세션 저장소(메모리)",
        "class SessionFilter",
        "로그인 세션 값",
    ):
        assert not session.search(fine), fine
    usage = storage_pattern("RequestUsage")
    assert usage is not None and usage.search("INSERT INTO request_usage VALUES")
    assert usage.search("request_usages 테이블") and usage.search("RequestUsageRepository")
    assert (
        storage_pattern("회원별 요청 사용량") is None
    )  # 영문 식별자가 아니면 저장 꼴을 만들 수 없다


def _task(**over: Any) -> TaskItem:
    base: dict[str, Any] = {
        "task_id": "ct-가상",
        "track": "common",
        "task_kind": "feature",
        "target_competencies": [{"competency_id": "comp:a", "label": "가상 역량"}],
        "tech_env": {"basis": "가상"},
        "difficulty_target": "medium",
        "time_limit_min": 60,
        "artifacts": [
            {
                "artifact_id": "a1",
                "title": "기존 조회 코드",
                "content": "public interface SessionRepository extends JpaRepository<Session, Long> {}",
                "role": "distractor",
            },
            {
                "artifact_id": "a2",
                "title": "운영 설정",
                "content": "maximum-pool-size: 20\n피크 동시 요청 120",
                "role": "key",
            },
            {
                "artifact_id": "a3",
                "title": "조회 API 명세",
                "content": "응답 칸: sessionId (문자열) · 감사 기록 조회",
                "role": "support",
            },
        ],
        "clarifications": [
            {"clarification_id": "c1", "topic": "인증 방식", "answer": "JWT 는 쓰지 않습니다"}
        ],
        "facts": [
            {
                "fact_id": "f1",
                "covers": ["r1"],
                "topic": "저장",
                "ask_when": "저장 위치를 물으면",
                "answer": "지금은 세션 테이블에 둡니다",
            }
        ],
        "debug_stage": {
            "time_limit_min": 20,
            "scenario": "가상 증상",
            "trap_ids": ["t1"],
            "deliverables": ["가상 진단"],
            "artifacts": [
                {
                    "artifact_id": "a9",
                    "title": "DB 조회 로그",
                    "content": "SELECT * FROM sessions WHERE id = ?",
                    "role": "key",
                }
            ],
        },
    }
    return TaskItem.model_validate(base | over)


# 업무 요구에 나온 말(감사 기록)·운영 조건 낱말(동시 요청)은 공개 쪽에서 막을 수 없다 — 낱말 대조에서 뺀다
LEAKY = _frame(forbidden_terms=["JWT", "세션 테이블", "감사 기록", "동시 요청"])


def test_check3_flags_what_the_frame_blocked_in_stage1_text_only() -> None:
    flags = frame_leak_flags(_task(), LEAKY, SEED)
    assert all(f.startswith(FLAG_FRAME_LEAK) for f in flags)
    assert any(
        "자료 a1(방해) '기존 조회 코드': Session 저장 'SessionRepository'" in f for f in flags
    )
    assert any(f.endswith("추가 요구 c1 답: JWT") for f in flags)
    assert any(f.endswith("사실 f1 답: 세션 테이블") for f in flags)
    assert (
        len(flags) == 3
    )  # 운영 설정(a2)·공개 칸 sessionId(a3)·2단계 조회 로그(a9)는 걸리지 않는다
    assert frame_leak_flags(_task(), None, SEED) == []


def test_check3_is_recomputed_whenever_the_task_text_changes() -> None:
    """글에서 다시 계산하는 표시 — 담당자가 고쳐 걸린 글이 없어지면 표시도 없어진다. 틀이 없는 예전 과제는 표시가 없다."""
    incident = known_incident().model_copy(update={"feature": SEED})
    first = with_content_flags(["가상 다른 표시"], _task(), incident, frame=LEAKY)
    assert "가상 다른 표시" in first and sum(f.startswith(FLAG_FRAME_LEAK) for f in first) == 3
    clean = _task(artifacts=[], clarifications=[], facts=[])
    again = with_content_flags(first, clean, incident, frame=LEAKY)
    assert "가상 다른 표시" in again and not any(f.startswith(FLAG_FRAME_LEAK) for f in again)
    old = with_content_flags(first, _task(), incident)
    assert not any(f.startswith(FLAG_FRAME_LEAK) for f in old)
