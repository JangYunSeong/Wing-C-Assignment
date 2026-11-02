"""종단: PDF 업로드 → 추출(칸 표시) → 가명화 → 주장 추출(FakeBackend) → 승인 대기."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from task_spec.api.app import create_app
from task_spec.graph import build_graph, make_checkpointer
from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.nodes.ingest import FileIngestor
from task_spec.nodes.pseudonymize import RulePseudonymizer, load_pseudonymized
from task_spec.prompting import PromptRegistry
from tests.api.conftest import make_settings, wait_status
from tests.ingest import docs

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


async def test_pdf_flows_to_approval_with_field_markers(tmp_path: Path) -> None:
    body = {
        "claims": [
            {
                "action": "DAG 재구성",
                "quote": "Airflow DAG 를 재구성하고 Spark 집계를 파티션 단위로 나눴다.",
            }
        ]
    }
    fake = FakeBackend().push(Completion(json.dumps(body, ensure_ascii=False)))
    pseudonymizer = RulePseudonymizer(tmp_path)
    await pseudonymizer.warm_up()
    cm = make_checkpointer(kind="memory")
    cp = await cm.__aenter__()
    graph = build_graph(
        cp,
        ingestor=FileIngestor(tmp_path),
        pseudonymizer=pseudonymizer,
        llm=LLMClient({"opencode": fake}),
        prompts=PROMPTS,
    )
    app = create_app(settings=make_settings(tmp_path, upload_max_bytes=2_000_000), graph=graph)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    try:
        pdf = docs.one_column_pdf(tmp_path / "src.pdf").read_bytes()
        r = await client.post(
            "/runs",
            data={"posting_id": "posting-fake-001"},
            files={"resumes": ("cv.pdf", pdf, "application/pdf")},
        )
        view = await wait_status(client, r.json()["run_id"], "awaiting_approval", "failed")
        assert view["status"] == "awaiting_approval", view
        state = (await graph.aget_state({"configurable": {"thread_id": r.json()["run_id"]}})).values
        assert state["extracted"][0].method == "text_layer" and state["extracted"][0].fields == 3
        pseudo = load_pseudonymized(state["pseudo_docs"][0])
        assert "⟦과정⟧" in pseudo  # 칸 표시가 가명화 뒤에도 남는다
        assert "가상대학교" not in pseudo and "[SCHOOL_1]" in pseudo
        # 알려진 한계 (pseudonymization 스킬, eval hard 0%): 사전·접미어 없는 회사명은 아직 남는다.
        # 경력 줄 규칙이 들어오면 이 단언을 not in 으로 바꾼다.
        assert "가상데이터랩" in pseudo
        assert [c.claim_id for c in state["claims"]] == ["resume-1-c1"]
    finally:
        await app.state.launcher.shutdown()
        await client.aclose()
        await cm.__aexit__(None, None, None)
