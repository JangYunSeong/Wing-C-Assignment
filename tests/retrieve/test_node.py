"""retrieve_t1 노드를 그래프 안에서 (FakeBackend: 주장 추출 → 역량 선택). 데이터는 가상."""

from __future__ import annotations

import json
from pathlib import Path

from task_spec.graph import build_graph, make_checkpointer
from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.prompting import PromptRegistry
from task_spec.retrieve.t1 import T1Retriever
from tests.conftest import config_for, fake_doc, run_input
from tests.graph.test_extract_claims import _writer
from tests.retrieve.fixtures import cardset

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


async def test_claims_get_candidates_and_selection(tmp_path: Path) -> None:
    claims = {
        "claims": [
            {
                "action": "데이터 파이프라인 설계",
                "quote": "[ORG_1] 에서 데이터 파이프라인을 설계했다.",
            },
            {"action": "처리 시간 단축", "result": "30%", "quote": "처리 시간 30% 단축."},
        ]
    }
    picks = {
        "items": [
            {"ref": "c1", "competency_ids": ["comp:data.pipeline"], "reason": "파이프라인 설계"},
        ]
    }
    fake = FakeBackend().push(
        Completion(json.dumps(claims, ensure_ascii=False)),
        Completion(json.dumps(picks, ensure_ascii=False)),
    )
    cards, m = cardset()
    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(
            cp,
            pseudonymizer=_writer(tmp_path),
            llm=LLMClient({"opencode": fake}),
            prompts=PROMPTS,
            retriever=T1Retriever(cards, m),
        )
        inp = run_input(docs=[fake_doc("d1", "resume")])
        result = await graph.ainvoke(inp, config_for(inp))

    by = {c.claim_id: c for c in result["candidates"]}
    assert by["d1-c1"].competency_ids == ["comp:data.pipeline"]
    assert by["d1-c1"].candidates[0].competency_id == "comp:data.pipeline"
    assert by["d1-c2"].competency_ids == []  # 후보 없음 → 선택 없음
    tasks = [p.prompt.split("@")[0] for p in result["provenance"]]
    assert tasks == ["extract_claims", "select_competency"]
    assert result["__interrupt__"]
