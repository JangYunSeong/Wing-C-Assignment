"""e2e 대조 평가 도구(eval/retrieve/run_e2e.py) — 실제 그래프(FakeBackend) 한 번 + 순수 함수. 가상 데이터만."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eval.retrieve.overlap_gold import score as gold_score
from eval.retrieve.run_e2e import (
    Pair,
    fill_judgement,
    judge_plan,
    load_pairs,
    node_walls,
    pick_source,
    provisional_gold,
    run_pair,
    run_summary,
    score,
    stability,
)
from task_spec.graph import build_graph, builder, make_checkpointer
from task_spec.llm import FakeBackend, LLMClient
from task_spec.prompting import PromptRegistry
from task_spec.retrieve.t1 import T1Retriever
from task_spec.schemas import OverlapVerdict
from tests.graph.test_extract_claims import _writer
from tests.graph.test_overlap_flow import CHECK, CLAIMS, PICKS, _json, _profile_node
from tests.retrieve.fixtures import cardset

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


def test_pairs_and_source_files(tmp_path: Path) -> None:
    (tmp_path / "pairs.json").write_text(
        json.dumps(
            {
                "pairs": [
                    {"resume": "v01", "split": "test", "posting_id": "p1", "kind": "own"},
                    {"resume": "v01", "split": "test", "posting_id": "p1"},
                    {"resume": "v02", "split": "test", "posting_id": "p2"},
                    {"resume": "v03", "split": "test", "posting_id": "p2"},
                ]
            }
        ),
        "utf-8",
    )
    pairs = load_pairs(tmp_path / "pairs.json")
    assert pairs == [
        Pair("v01", "test", "p1"),
        Pair("v02", "test", "p2"),
        Pair("v03", "test", "p2"),
    ]
    (tmp_path / "resumes" / "test").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "resumes" / "v01.txt").write_text("가상", "utf-8")
    (tmp_path / "docs" / "v01.docx").write_bytes(b"PK")
    (tmp_path / "resumes" / "test" / "v02.txt").write_text("가상", "utf-8")
    r, d = tmp_path / "resumes", tmp_path / "docs"
    assert pick_source(pairs[0], r, d).name == "v01.docx"  # docx 우선
    assert pick_source(pairs[0], r, None).name == "v01.txt"
    assert pick_source(pairs[1], r, d) == r / "test" / "v02.txt"  # split 폴더
    with pytest.raises(FileNotFoundError):
        pick_source(pairs[2], r, d)


@pytest.fixture
def patched_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder, "make_posting_profile", _profile_node)


async def test_run_pair_drives_the_real_graph(tmp_path: Path, patched_profile) -> None:
    src = tmp_path / "in" / "v01.txt"
    src.parent.mkdir()
    src.write_text("가상 이력서 (FakeBackend 라 내용은 읽히지 않음)", "utf-8")
    fake = FakeBackend().push(_json(CLAIMS), _json(PICKS), _json(CHECK))
    cards, m = cardset()
    union = {"comp:data.pipeline": "데이터 파이프라인", "comp:sw.cache": "캐시 설계"}
    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(
            cp,
            pseudonymizer=_writer(tmp_path),
            llm=LLMClient({"opencode": fake}),
            prompts=PROMPTS,
            retriever=T1Retriever(cards, m),
        )
        row = await run_pair(
            graph,
            data_dir=tmp_path / "data",
            max_bytes=1_000_000,
            ontology_version="onto-v2026.11.02",
            pair=Pair("v01", "test", "posting-fake-001"),
            src=src,
            run=0,
            union=union,
        )
    # 업로드는 API 와 같은 경로·이름으로 저장된다
    assert (tmp_path / "data" / "raw" / row["run_id"] / "resume-1.txt").is_file()
    assert row["status"] == "awaiting_approval" and row["input"] == "txt" and "error" not in row
    t = {x["competency_id"]: x for x in row["table"]}
    assert (
        t["comp:data.pipeline"]["L0"],
        t["comp:data.pipeline"]["TD"],
        t["comp:data.pipeline"]["source"],
    ) == (
        True,
        True,
        "both",
    )
    assert (t["comp:sw.cache"]["L0"], t["comp:sw.cache"]["OP"], t["comp:sw.cache"]["source"]) == (
        False,
        True,
        "verify",
    )
    assert t["comp:data.stat_ml"]["op_status"] == "related" and not t["comp:data.stat_ml"]["OP"]
    assert row["overlap_check"]["status"] == "ok" and row["comparison"]["related"] == [
        "comp:data.stat_ml"
    ]
    assert row["comparison"]["overlap_by_source"] == {
        "both": ["comp:data.pipeline"],
        "verify": ["comp:sw.cache"],
    }
    assert set(row["llm_calls_by_node"]) == {"extract_claims", "retrieve_t1", "verify_overlap"}
    assert {"verify_overlap", "compare", "draft_spec"} <= set(row["node_wall_s"])
    assert row["approval_summary"]["comparison"]["posting_checks"]
    assert row["pseudo_doc"]["doc_id"] == "resume-1" and row["judge_asked"] == union
    assert [c["claim_id"] for c in row["claims"]] == ["resume-1-c1", "resume-1-c2"]
    assert row["per_claim"]["resume-1-c1"]["picked"] == ["comp:data.pipeline"]
    # 판정 이유는 행에 남지 않는다
    assert "SENTINEL-REASON" not in json.dumps(row, ensure_ascii=False, default=str)


def _row(
    run: int, cmp: dict[str, tuple[bool, str | None, str, str | None, str | None]]
) -> dict[str, Any]:
    """cmp: 역량 → (L0, 확인 label, 운영 위치, 출처, 역할)."""
    return {
        "resume": "v01",
        "split": "test",
        "posting": "p1",
        "run": run,
        "run_id": f"e2e-{run}",
        "input": "txt",
        "pseudo_doc": {"doc_id": "resume-1"},
        "claims": [
            {
                "claim_id": "c1",
                "quote": "가상 주문 API 를 설계했다",
                "role": "design",
                "field": "과정",
            },
            {"claim_id": "c2", "quote": "가상 캐시로 응답을 줄였다", "role": None, "field": None},
        ],
        "per_claim": {
            "c1": {"picked": ["comp:a"], "top10": ["comp:a", "comp:c"]},
            "c2": {"picked": [], "top10": []},
        },
        "table": [
            {
                "competency_id": k,
                "label": k,
                "primary": True,
                "gt": None,
                "evidence": "",
                "evidence_claims": [],
                "L0": l0,
                "TD": td == "action",
                "td_label": td,
                "td_claims": [],
                "OP": where == "overlap",
                "op_status": where,
                "source": src,
                "role": role,
                "miss_cause": None,
                "op_miss_cause": None,
            }
            for k, (l0, td, where, src, role) in cmp.items()
        ],
        "cost_by_node": {"extract_claims": 0.002},
        "node_wall_s": {"compare": 0.1},
        "pipeline_cost_usd": 0.002,
        "wall_s": 3.0,
        "overlap_check": {"status": "ok", "labels": {}, "reasks": 1, "notes": 2, "failures": 0},
        "comparison": {
            "overlap_by_source": {"both": ["comp:a"]},
            "related": [],
            "posting_only": [],
        },
    }


JUDGED = [
    OverlapVerdict(competency_id="comp:a", label="action", evidence="가상 주문 API 를 설계했다"),
    OverlapVerdict(competency_id="comp:b", label="action", evidence="가상 캐시로 응답을 줄였다"),
    OverlapVerdict(competency_id="comp:c", label="none", evidence=""),
    OverlapVerdict(competency_id="comp:d", label="action", evidence="원문에 없는 근거 문장입니다"),
]
ASKED = {"comp:a": "A", "comp:b": "B", "comp:c": "C", "comp:d": "D"}


def test_judgement_scoring_and_compatibility() -> None:
    rows = [
        _row(
            0,
            {
                "comp:a": (True, "action", "overlap", "both", "design"),
                "comp:b": (False, "related", "related", None, None),
                "comp:c": (False, "action", "overlap", "verify", None),
                "comp:d": (False, "none", "posting_only", None, None),
            },
        ),
        _row(
            1,
            {
                "comp:a": (True, "action", "overlap", "both", "design"),
                "comp:b": (False, "action", "overlap", "verify", None),
                "comp:c": (False, "none", "posting_only", None, None),
                "comp:d": (False, "none", "posting_only", None, None),
            },
        ),
    ]
    for r in rows:
        fill_judgement(r, JUDGED, ASKED)
    t0 = {x["competency_id"]: x for x in rows[0]["table"]}
    assert (t0["comp:b"]["miss_cause"], t0["comp:b"]["op_miss_cause"]) == (
        "검색 누락",
        "확인 related",
    )
    assert (t0["comp:d"]["miss_cause"], t0["comp:d"]["op_miss_cause"]) == ("주장 누락", "주장 누락")
    assert t0["comp:b"]["evidence_claims"] == ["c2"]

    gold = provisional_gold(rows)
    s = score(rows, gold)
    r0, r1 = s["per_run"]
    assert r0["row_level"]["action"]["OP"] == {
        "predicted": 2,
        "gold": 3,
        "precision": 0.5,
        "recall": 0.333,
    }
    assert r1["row_level"]["action"]["OP"] == {
        "predicted": 2,
        "gold": 3,
        "precision": 1.0,
        "recall": 0.667,
    }
    assert r0["row_level"]["action"]["L0"]["recall"] == 0.333
    assert (
        r0["by_source"]["both"]["action_precision"] == 1.0 and r0["by_source"]["verify"]["n"] == 1
    )
    assert r0["role_rate"] == {"overlap": 0.5, "overlap_n": 2}
    assert r0["op_miss_causes"] == {"확인 related": 1, "주장 누락": 1}
    assert s["provisional"] and s["stability"]["op_agree"] == 0.5
    # 기존 도구가 같은 행을 읽는다 (overlap_gold score — L0 층)
    g = gold_score(rows[:1], gold)
    assert g["same_standard"]["action"]["L0"] == {
        "predicted": 1,
        "gold": 3,
        "precision": 1.0,
        "recall": 0.333,
    }


def test_plan_summary_and_walls() -> None:
    rows = [
        _row(1, {"comp:a": (True, "action", "overlap", "both", None)}),
        _row(0, {"comp:a": (True, "action", "overlap", "both", None)}),
    ]
    assert judge_plan(rows)["v01"]["run"] == 0  # 가장 이른 실행의 가명화본으로 한 번만 판정
    assert node_walls([("ingest", 1.0), ("pseudonymize", 1.5), ("compare", 4.0)]) == {
        "ingest": 1.0,
        "pseudonymize": 0.5,
        "compare": 2.5,
    }
    s = run_summary(
        [
            *rows,
            {"resume": "v02", "posting": "p", "run": 0, "input": "docx", "error": "RuntimeError"},
        ]
    )
    assert s["ok"] == 2 and s["failed"] == {"RuntimeError": 1} and s["reasks"] == 2
    assert s["cost_usd"]["pipeline"] == 0.004 and s["inputs"] == {"txt": 2, "docx": 1}
    assert stability(rows[:1]) == {"pairs_with_runs": 0}


async def test_stop_before_draft(tmp_path: Path, patched_profile) -> None:
    src = tmp_path / "v01.txt"
    src.write_text("가상", "utf-8")
    fake = FakeBackend().push(_json(CLAIMS), _json(PICKS), _json(CHECK))
    cards, m = cardset()
    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(
            cp,
            pseudonymizer=_writer(tmp_path),
            llm=LLMClient({"opencode": fake}),
            prompts=PROMPTS,
            retriever=T1Retriever(cards, m),
        )
        row = await run_pair(
            graph,
            data_dir=tmp_path / "data",
            max_bytes=1_000_000,
            ontology_version="onto-v2026.11.02",
            pair=Pair("v01", None, "posting-fake-001"),
            src=src,
            run=0,
            union={},
            stop_before_draft=True,
        )
    assert row["status"] == "stopped_before_draft" and row["approval_summary"] is None
    assert "draft_spec" not in row["node_wall_s"] and any(x["OP"] for x in row["table"])
