"""D2 새 프롬프트 평가 채점기 자기 검증 (#266) — 모범 출력이면 만점, 어긋난 출력은 잡는다. LLM 없음, 가상 데이터만."""

from __future__ import annotations

import json

from eval.interview_task.run_eval import (
    CRITERIA,
    AnswerResult,
    SeedResult,
    score_answer,
    score_seed,
    summarize,
)
from eval.interview_task.samples import ANSWER_CASES, SEED_CASES, answer_cases, seed_cases
from task_spec.schemas import FeatureSeed
from task_spec.starter.templates import TEMPLATE_FOR_SUB, load_template
from tests.interview.test_task import known_incident

SEED = SEED_CASES[0]
ANSWER = ANSWER_CASES[0]


def good_seed(**update: object) -> FeatureSeed:
    base = known_incident().feature
    assert base is not None
    return base.model_copy(
        update={
            "business_need": "가상 쇼핑몰 주문을 받아 저장하는 기능",
            "hidden_requirement": "같은 요청이 두 번 와도 주문은 한 건만 남아야 한다",
            "accepted_designs": ["[설계] 가 — 조건: 가", "[설계] 나 — 조건: 나"],
            "reasons": sorted(SEED.reasons),
            "harness": [],
            **update,
        }
    )


def test_samples_are_split_and_planted_reasons_exist_in_the_templates() -> None:
    assert len(seed_cases("dev")) == len(seed_cases("test")) == 6
    assert len(answer_cases("dev")) == len(answer_cases("test")) == 6
    for case in SEED_CASES:
        codes = {r.code for r in load_template(TEMPLATE_FOR_SUB[case.sub]).manifest.failure_reasons}
        assert case.reasons and case.reasons <= codes, case.id
    ids = [c.id for c in (*SEED_CASES, *ANSWER_CASES)]
    assert len(ids) == len(set(ids))


def test_a_good_seed_scores_full_marks() -> None:
    r = score_seed(SEED, good_seed(), known_incident())
    assert r.valid and r.reason_hit and r.concept_hit and r.harness_ok
    assert r.need_leak == [] and r.designs == 2 and r.blocked == 0


def test_seed_problems_are_caught() -> None:
    bad = good_seed(
        business_need="요청 식별자로 중복 주문을 막는 기능",
        hidden_requirement="응답이 빨라야 한다",
        reasons=["server_error"],
        harness=["pool_size"],
        accepted_designs=["[설계] 하나뿐"],
    )
    r = score_seed(SEED, bad, known_incident())
    assert not r.reason_hit and not r.concept_hit and not r.harness_ok
    assert r.need_leak == ["요청 식별자"] and r.designs == 1


def test_a_good_answer_scores_full_marks_and_bad_ones_are_caught() -> None:
    inputs = "\n".join(
        [ANSWER.question, ANSWER.hidden_condition, json.dumps(ANSWER.task, ensure_ascii=False)]
    )
    good = score_answer(
        ANSWER, "주문 요청 기록에서 같은 내용이 반복된 줄을 확인할 수 있습니다.", inputs
    )
    assert good.valid and good.pointer_hit and not good.solution_leak
    assert not good.artifact_id and good.invented_numbers == []
    bad = score_answer(ANSWER, "a2 자료처럼 멱등 키를 쓰고 30초 안에 다시 보내세요.", inputs)
    assert bad.solution_leak == ["멱등"] and bad.artifact_id and bad.invented_numbers == ["30"]
    assert not bad.pointer_hit


def test_criteria_are_fixed_and_summary_judges_them() -> None:
    assert CRITERIA["seed_need_leak"] == ("<=", 0) and CRITERIA["answer_solution_leak"] == ("<=", 0)
    perfect_seed = score_seed(SEED, good_seed(), known_incident())
    perfect_answer = score_answer(ANSWER, "주문 요청 기록을 보면 됩니다.", ANSWER.question)
    ok = summarize([perfect_seed] * 6, [perfect_answer] * 6)
    assert ok["passed"] and ok["seed_valid"] == 1.0 and ok["answer_pointer_hit"] == 1.0
    failed = summarize(
        [perfect_seed] * 4 + [SeedResult(id="x", failure="OutputRejected: 가상")] * 2,
        [perfect_answer] * 5 + [AnswerResult(id="y", failure="SchemaViolation: 가상")],
    )
    assert failed["seed_valid"] == round(4 / 6, 3) and not failed["criteria"]["seed_valid"]["pass"]
    assert not failed["criteria"]["answer_valid"]["pass"] and not failed["passed"]
    only_seeds = summarize([perfect_seed] * 6, [])
    assert only_seeds["passed"] and not any(k.startswith("answer_") for k in only_seeds["criteria"])
