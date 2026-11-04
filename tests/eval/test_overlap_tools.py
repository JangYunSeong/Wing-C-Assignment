"""대조 겹침 평가 도구의 순수 함수 — 합의·최종 정답·채점·검색어 정규화·프로젝트 맥락·순위. 가상 데이터만."""

from __future__ import annotations

import json

import pytest

from eval.retrieve.overlap_gold import build_gold, parse_reviewed, score
from eval.retrieve.run_judge_consensus import consensus
from eval.retrieve.run_search_variants import best_rank, norm_terms, project_context

DOC = (
    "가상 주문 플랫폼 개편 2030.01~2030.06\n"
    "⟦문제 정의⟧ 주문 조회가 느려 고객 이탈이 있었음\n"
    "⟦과정⟧ 조회 쿼리를 바꾸고 결과를 캐\n"
    "시해 응답을 줄임\n"
    "⟦기여한 점⟧ 응답 시간을 3초에서 0.4초로 단축\n"
)


def test_consensus_keeps_only_agreed_labels() -> None:
    a = {"comp:a": "action", "comp:b": "none", "comp:c": "related"}
    b = {"comp:a": "action", "comp:b": "related", "comp:d": "none"}
    agreed, differ = consensus(a, b)
    assert agreed == {"comp:a": "action"}
    assert differ == ["comp:b", "comp:c", "comp:d"]


def test_norm_terms_adds_general_words_for_numeric_results() -> None:
    assert "성능 개선" in norm_terms("응답 시간을 3초에서 0.4초로 단축")
    assert "비용 절감" in norm_terms(None, "월 토큰 사용을 20% 절감")
    assert norm_terms("화면 문구를 다듬었다") == ""


def test_project_context_finds_header_and_problem_across_line_break() -> None:
    header, problem = project_context(DOC, "결과를 캐시해 응답을 줄임")
    assert header.startswith("가상 주문 플랫폼 개편") and problem.startswith("주문 조회가 느려")
    assert project_context(DOC, "없는 문장") == ("", "")


def test_best_rank_takes_best_over_evidence_claims() -> None:
    assert best_rank([["x", "y", "comp:p"], ["comp:p"]], "comp:p") == 1
    assert best_rank([["x"], ["y"]], "comp:p") is None


REVIEWED = """| 이력서 | 역량 | 1차 | 2차 | 최종 | 확신 | 메모 |
|---|---|---|---|---|---|---|
| x1 | 캐시 설계 | action | related | action | 상 | 조회 캐시를 직접 붙임 |
| x1 | 배포 자동화 | none | mention | none | 하 | 도구 이름만 |
"""


def _consensus() -> dict:
    return {
        "gold": {"x1": {"comp:a": "action", "comp:b": "none"}},
        "disagreement_rows": [
            {
                "resume": "x1",
                "label": "캐시 설계",
                "competency_id": "comp:cache",
                "first_evidence": "결과를 캐시해 응답을 줄임",
                "second_evidence": "",
            },
            {
                "resume": "x1",
                "label": "배포 자동화",
                "competency_id": "comp:deploy",
                "first_evidence": "",
                "second_evidence": "",
            },
        ],
    }


def test_build_gold_merges_consensus_and_human_review() -> None:
    rows = parse_reviewed(REVIEWED)
    assert [r["final"] for r in rows] == ["action", "none"]  # 머리·구분 줄은 건너뜀
    report = [
        {
            "resume": "x1",
            "judge_items": [{"competency_id": "comp:a", "evidence": "조회 쿼리를 바꾸고"}],
        }
    ]
    g = build_gold(_consensus(), rows, report)
    assert g["sources"] == {"consensus": 2, "human": 2}
    assert g["counts"] == {"action": 2, "related": 0, "mention": 0, "none": 2}
    x = g["resumes"]["x1"]
    assert x["comp:a"] == {
        "label": "action",
        "source": "consensus",
        "evidence": ["조회 쿼리를 바꾸고"],
    }
    assert x["comp:cache"]["source"] == "human" and x["comp:cache"]["evidence"] == [
        "결과를 캐시해 응답을 줄임"
    ]
    assert x["comp:deploy"]["confidence"] == "하"
    with pytest.raises(ValueError):
        build_gold(_consensus(), parse_reviewed(REVIEWED.replace("캐시 설계", "없는 역량")), report)


def test_score_layers_causes_and_low_confidence() -> None:
    rows = parse_reviewed(REVIEWED)
    gold = build_gold(_consensus(), rows, [])
    row = {
        "resume": "x1",
        "claims": [{"claim_id": "c1", "quote": "조회 쿼리를 바꾸고 결과를 캐시해 응답을 줄임"}],
        "per_claim": {"c1": {"picked": ["comp:a"], "top10": ["comp:a", "comp:cache"]}},
        "table": [
            {"competency_id": "comp:a", "L0": True, "L1": False, "L2": False},
            {"competency_id": "comp:cache", "L0": False, "L1": True, "L2": False},
            {"competency_id": "comp:deploy", "L0": False, "L1": False, "L2": True},
            {"competency_id": "comp:b", "L0": False, "L1": False, "L2": False},
        ],
    }
    out = score([row], gold)
    act = out["same_standard"]["action"]
    assert act["L0"] == {"predicted": 1, "gold": 2, "precision": 1.0, "recall": 0.5}
    assert act["L0+L1+L2"]["precision"] == round(2 / 3, 3) and act["L0+L1"]["recall"] == 1.0
    assert out["missed_action_causes"] == {"선택 누락": 1}  # 근거 주장의 상위 10 에는 있었음
    low = score([row], gold, exclude_low=True)
    assert low["rows"] == 3 and low["same_standard"]["action"]["L0+L1+L2"]["precision"] == 1.0


def test_load_pairs_groups_postings_by_resume_and_reads_only_needed_fields(tmp_path) -> None:
    from eval.retrieve.run_overlap import Job, load_pairs

    pairs = {
        "pairs": [
            {"resume": "z1", "split": "dev", "posting_id": "p#1", "kind": "own"},
            {"resume": "z1", "split": "dev", "posting_id": "p#2", "kind": "other"},
            {"resume": "z1", "split": "dev", "posting_id": "p#1"},
            {"resume": "z2", "split": "test", "posting_id": "p#3"},
        ]
    }
    f = tmp_path / "pairs.json"
    f.write_text(json.dumps(pairs), "utf-8")
    jobs = load_pairs(f)
    assert jobs == [
        Job(tmp_path / "dev" / "z1.txt", ("p#1", "p#2"), "dev"),
        Job(tmp_path / "test" / "z2.txt", ("p#3",), "test"),
    ]
    pairs["pairs"].append({"resume": "z2", "split": "dev", "posting_id": "p#4"})
    f.write_text(json.dumps(pairs), "utf-8")
    with pytest.raises(ValueError):
        load_pairs(f)


def test_in_report_keeps_held_out_rows_out_of_metrics() -> None:
    from eval.retrieve.run_overlap import in_report

    assert in_report({"resume": "r1"}, {"dev"})  # split 없는 행(--resumes)은 늘 지표에
    assert in_report({"split": "dev"}, {"dev"})
    assert not in_report({"split": "test"}, {"dev"})
    assert in_report({"split": "test"}, None)


def test_estimate_judge_cost_scales_with_prompt_and_items() -> None:
    from eval.retrieve.run_overlap import estimate_judge_cost

    base = estimate_judge_cost(1300, 10)
    assert base == pytest.approx(1000 * 2e-6 + 600 * 8e-6)
    assert estimate_judge_cost(2600, 20) == pytest.approx(2 * base)


def test_write_report_scores_only_report_splits(tmp_path) -> None:
    from eval.retrieve.run_overlap import write_report

    def row(resume: str, split: str, gt: str, l0: bool) -> dict:
        return {
            "resume": resume,
            "split": split,
            "posting": "p#1",
            "sub": "x",
            "extract_errors": [],
            "judge_check": {},
            "judge_cost_usd": 0.01,
            "pipeline_cost_usd": 0.001,
            "claims": [],
            "per_claim": {},
            "table": [
                {
                    "competency_id": "comp:a",
                    "label": "가상 역량",
                    "gt": gt,
                    "evidence": "",
                    "evidence_claims": [],
                    "L0": l0,
                    "L1": False,
                    "L2": False,
                    "miss_cause": None,
                }
            ],
        }

    out = tmp_path / "run"
    write_report(
        "t",
        [row("d1", "dev", "action", True), row("t1", "test", "action", False)],
        legacy=False,
        out_root=tmp_path,
        out_dir=out,
        report_splits={"dev"},
    )
    rep = json.loads((out / "report.json").read_text("utf-8"))
    assert rep["ok"] == 1 and rep["held_out_pairs"] == 1 and rep["held_out_resumes"] == ["t1"]
    assert rep["same_standard"]["action"]["L0"]["recall"] == 1.0  # test 행(놓침)은 지표 밖
    assert rep["cost_usd"] == {"judge": 0.02, "pipeline": 0.002}  # 비용은 held-out 까지
    assert "t1" not in (out / "samples.md").read_text("utf-8")
    assert len(rep["rows"]) == 2  # 원자료는 남긴다


def test_read_judge_dir_validates_answers(tmp_path) -> None:
    from eval.retrieve.judge_packets import read_judge_dir

    asked = {
        "z1": {"comp:a": "가상 A", "comp:b": "가상 B"},
        "z2": {"comp:a": "가상 A"},
        "z3": {"comp:a": "가상 A"},
    }
    (tmp_path / "z1.json").write_text(
        "```json\n"
        + json.dumps(
            {
                "items": [
                    {"competency_id": "comp:a", "label": "action", "evidence": "주문 API 구현"},
                    {"competency_id": "comp:a", "label": "none", "evidence": ""},
                    {"competency_id": "comp:zz", "label": "none", "evidence": ""},
                ]
            },
            ensure_ascii=False,
        )
        + "\n```",
        "utf-8",
    )
    (tmp_path / "z2.json").write_text('{"items": [{"competency_id": "comp:a"}]}', "utf-8")
    labels, evidence, checks = read_judge_dir(tmp_path, asked)
    assert labels == {"z1": {"comp:a": "action"}}  # 중복이면 첫 답, 목록 밖 id 는 버림
    assert evidence["z1"]["comp:a"] == "주문 API 구현"
    assert checks["z1"] == {
        "asked": 2,
        "answered": 1,
        "duplicates": 1,
        "unknown_ids": ["comp:zz"],
        "missing_ids": ["comp:b"],
    }
    assert checks["z2"]["error"].startswith("스키마 위반")
    assert checks["z3"] == {"error": "답 파일 없음"}


def test_render_packet_carries_prompt_and_answer_contract() -> None:
    from types import SimpleNamespace

    from eval.retrieve.judge_packets import render_packet
    from task_spec.prompting import Message

    prompt = SimpleNamespace(
        messages=[
            Message(role="system", content="가상 지시 — 출력 스키마 {}"),
            Message(
                role="user", content="역량 목록:\ncomp:a — 가상 A\n\n이력서 (가명화본):\n[PERSON_1]"
            ),
        ]
    )
    md = render_packet("z1", prompt, "/x/out/z1.json", 1)  # type: ignore[arg-type]
    assert "`/x/out/z1.json`" in md and "1개 모두" in md
    assert all(m.content in md for m in prompt.messages)
    assert md.index("## 지시") < md.index("## 입력")
