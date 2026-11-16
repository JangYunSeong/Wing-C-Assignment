"""흘림 판정 평가 도구 (eval/spec/run_fact_leak.py) — 입력에 숨은 칸이 없고, 인용을 원문에서 확인해 센다. 가상 명세·가짜 판정."""

from __future__ import annotations

import copy

from eval.spec.run_fact_leak import judge_input, score, summarize
from task_spec.schemas import DraftAnalysisOutput, JudgeLeakOutput, JudgeLeakOutputV2, TaskItem
from task_spec.spec.analysis_request import AnalysisResult, apply_analysis
from tests.spec.test_analysis_request import _good, _task


def _analysed(**edit: str) -> TaskItem:
    d = copy.deepcopy(_good())
    d.update(edit)
    return apply_analysis(_task(), AnalysisResult(output=DraftAnalysisOutput.model_validate(d)))


def test_input_shows_what_a_candidate_sees_and_the_answers_to_protect() -> None:
    task = _analysed()
    inp = judge_input(task)
    assert "[background] 가상의 물류 회사" in inp.visible and "[request] 주문 상태가" in inp.visible
    assert "f2 이력은 정해진 순서로 보여야 해요." in inp.hints and "이력 조회 순서" not in inp.hints
    assert "f2 (이력 조회 순서) 이력은 최신순으로 보여 주세요." in inp.facts
    assert "최신순" not in inp.visible + inp.hints  # 구체 답은 지킬 것에만
    shown = inp.model_dump_json()  # 숨은 요구는 안 보인다
    assert all(t.description not in shown for t in task.traps)
    assert all(a.text not in shown for a in task.acceptance_hidden)


def _judged(points_f2: list[dict[str, str]]) -> JudgeLeakOutput:
    return JudgeLeakOutput.model_validate(
        {
            "facts": [
                {"fact_id": "f1", "points": [{"point": "요청마다 한 건"}]},
                {"fact_id": "f2", "points": points_f2},
            ]
        }
    )


def test_quote_must_be_in_the_text_to_count() -> None:
    task = _analysed(background=_good()["background"] + " 이력은 늘 최신 것부터 보인다.")
    out = _judged(
        [
            {"point": "최신순", "quote": "이력은 늘 최신 것부터 보인다.", "source": "background"},
            {"point": "주문마다", "quote": "주문별로 조회한다", "source": "request"},  # 지어낸 인용
        ]
    )
    facts = {f["fact_id"]: f for f in score(task, out)["facts"]}
    assert facts["f1"]["verdict"] == "safe"
    f2 = facts["f2"]
    assert f2["verdict"] == "partial"
    assert [
        (p["status"], p["kinds"]) for p in f2["points"]
    ] == [  # v1 출력 모양(핵심마다 한 곳)도 센다
        ("found", ["visible"]),
        ("unverified", []),
    ]


def test_all_points_found_is_leaked_and_summary_splits_by_where() -> None:
    task = _analysed(request=_good()["request"] + " 목록은 최신순이면 좋겠어요.")
    out = _judged(
        [{"point": "최신순", "quote": "목록은 최신순이면 좋겠어요.", "source": "background"}]
    )
    s = score(task, out)  # source 를 틀리게 적어도 인용이 있는 곳으로 센다
    f2 = s["facts"][1]
    assert f2["verdict"] == "leaked" and f2["points"][0]["kinds"] == ["visible"]
    total = summarize([{"name": "x", **s}])
    assert [total[k] for k in ("facts", "leaked", "safe", "found_in_visible")] == [2, 1, 1, 1]
    # f1 은 요청 글이 직접 말한 r1 을 덮는다 — 보이는 것이 설계라 따로 센다
    assert [f["mentioned"] for f in s["facts"]] == [True, False]
    assert (total["facts_mentioned"], total["unmentioned_facts_seen_in_visible"]) == (1, 1)


def test_fact_left_out_by_the_judge_is_missing() -> None:
    out = JudgeLeakOutput.model_validate({"facts": [{"fact_id": "f1", "points": [{"point": "x"}]}]})
    assert [f["verdict"] for f in score(_analysed(), out)["facts"]] == ["safe", "missing"]


def test_v2_evidence_counts_every_place_a_point_is_seen() -> None:
    """v2 — 한 핵심이 보이는 곳을 모두 적어, 자료와 요청 글에 함께 보이면 두 곳 다 센다 (책임 자리가 둘)."""
    task = _analysed(request=_good()["request"] + " 목록은 최신순이면 좋겠어요.")
    out = JudgeLeakOutputV2.model_validate(
        {
            "facts": [
                {"fact_id": "f1", "points": [{"point": "요청마다 한 건"}]},
                {
                    "fact_id": "f2",
                    "points": [
                        {
                            "point": "최신순",
                            "evidence": [
                                {"source": "request", "quote": "목록은 최신순이면 좋겠어요."},
                                {"source": "f2", "quote": "이력은 정해진 순서로 보여야 해요."},
                                {"source": "a1", "quote": "sort: desc"},  # 없는 인용
                            ],
                        }
                    ],
                },
            ]
        }
    )
    p = score(task, out)["facts"][1]["points"][0]
    assert p["status"] == "found" and p["kinds"] == ["hint", "visible"]
    assert [e["ok"] for e in p["evidence"]] == [True, True, False]


def test_scopes_split_what_the_judge_sees_and_what_counts() -> None:
    """text = 보이는 글·모호한 답 (요청 글 단계 몫), artifact = 자료 (생성기 몫) — 범위 밖 인용은 세지 않는다."""
    task = _analysed()
    task = task.model_copy(
        update={
            "artifacts": [
                a.model_copy(update={"content": "sort: desc"}) for a in task.artifacts[:1]
            ]
        }
    )
    text, art = judge_input(task, "text"), judge_input(task, "artifact")
    assert text.artifacts == "(없음)" and "[request]" in text.visible
    assert (
        art.hints == "(없음)" and "[request]" not in art.visible and "sort: desc" in art.artifacts
    )
    aid = task.artifacts[0].artifact_id
    out = JudgeLeakOutputV2.model_validate(
        {
            "facts": [
                {"fact_id": "f1", "points": [{"point": "x"}]},
                {
                    "fact_id": "f2",
                    "points": [
                        {"point": "최신순", "evidence": [{"source": aid, "quote": "sort: desc"}]}
                    ],
                },
            ]
        }
    )
    assert score(task, out, "artifact")["facts"][1]["verdict"] == "leaked"
    assert score(task, out, "text")["facts"][1]["points"][0]["status"] == "unverified"


def test_quote_that_skips_lines_counts_when_every_piece_is_there() -> None:
    task = _analysed(
        background=_good()["background"] + "\n칸: requester_id,\n  created_at,\n  result"
    )
    out = JudgeLeakOutputV2.model_validate(
        {
            "facts": [
                {
                    "fact_id": "f1",
                    "points": [
                        {
                            "point": "요청자·결과",
                            "evidence": [{"source": "background", "quote": "requester_id, result"}],
                        }
                    ],
                },
                {"fact_id": "f2", "points": [{"point": "x"}]},
            ]
        }
    )
    assert score(task, out)["facts"][0]["verdict"] == "leaked"


def test_spec_mode_protects_requirements_and_acceptance() -> None:
    """문제 분석 전 명세 — 공식 요구·공개 수용 기준 문장이 지킬 답 (생성기 평가)."""
    from eval.spec.run_fact_leak import spec_items

    shown, table = spec_items(_task())
    assert table == {"f1": "r1", "f2": "r2", "f3": "p1", "f4": "p2"}
    inp = judge_input(shown, "artifact")
    assert "f1 (r1) 상태 변경 요청마다 이력을 남긴다" in inp.facts and inp.hints == "(없음)"
