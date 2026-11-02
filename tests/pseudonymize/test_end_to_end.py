"""종단: 업로드(.txt) → 실제 가명화 → 주장 추출(FakeBackend) → 승인 대기. 원 값이 어디에도 남지 않는지."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from task_spec.api.app import create_app
from task_spec.graph import build_graph, make_checkpointer
from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.nodes.ingest import FileIngestor
from task_spec.nodes.pseudonymize import RulePseudonymizer
from task_spec.prompting import PromptRegistry
from tests.api.conftest import make_settings, wait_status
from tests.pseudonymize.fixtures import RESUME

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
ORIGINALS = (
    "김가상",
    "박가온",
    "kim.gasang@example.com",
    "010-0000-1234",
    "삼성전자",
    "가상대학교",
)


async def _client(tmp_path: Path, fake: FakeBackend):
    cm = make_checkpointer(kind="memory")
    cp = await cm.__aenter__()
    pseudonymizer = RulePseudonymizer(tmp_path)
    await pseudonymizer.warm_up()  # 앱 lifespan 과 같이 Kiwi 를 미리 올린다
    graph = build_graph(
        cp,
        ingestor=FileIngestor(tmp_path),
        pseudonymizer=pseudonymizer,
        llm=LLMClient({"opencode": fake}),
        prompts=PROMPTS,
    )
    app = create_app(settings=make_settings(tmp_path, upload_max_bytes=100_000), graph=graph)
    return (
        app,
        cm,
        graph,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"),
    )


async def test_txt_resume_flows_to_approval_without_leaking(tmp_path: Path) -> None:
    body = {
        "claims": [
            {
                "action": "파이프라인 설계",
                "target": "Spark",
                "quote": "팀장 [PERSON_2]님과 함께 Spark 기반 파이프라인을 설계했다.",
            }
        ]
    }
    fake = FakeBackend().push(Completion(json.dumps(body, ensure_ascii=False)))
    app, cm, graph, client = await _client(tmp_path, fake)
    try:
        files = {"resumes": ("resume.txt", RESUME.encode(), "text/plain")}
        r = await client.post("/runs", data={"posting_id": "posting-fake-001"}, files=files)
        run_id = r.json()["run_id"]
        view = await wait_status(client, run_id, "awaiting_approval", "failed")
        assert view["status"] == "awaiting_approval", view

        state = (await graph.aget_state({"configurable": {"thread_id": run_id}})).values
        assert [c.claim_id for c in state["claims"]] == ["resume-1-c1"]
        dumped = json.dumps({k: str(v) for k, v in state.items()}, ensure_ascii=False)
        api_text = json.dumps(view, ensure_ascii=False)
        for original in ORIGINALS:
            assert original not in dumped, original  # State·체크포인트
            assert original not in api_text, original  # API 응답
        # LLM 에 간 것은 가명화본뿐 (FakeBackend 는 본문을 남기지 않으므로 claim 인용으로 확인)
        assert "[PERSON_2]" in state["claims"][0].quote
    finally:
        await app.state.launcher.shutdown()
        await client.aclose()
        await cm.__aexit__(None, None, None)


async def test_broken_pdf_upload_stops_with_extract_error(tmp_path: Path) -> None:
    app, cm, graph, client = await _client(tmp_path, FakeBackend())
    try:
        files = {"resumes": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")}
        r = await client.post("/runs", data={"posting_id": "posting-fake-001"}, files=files)
        view = await wait_status(client, r.json()["run_id"], "failed", "awaiting_approval")
        assert view["status"] == "failed"
        assert "extract_error" in [e["code"] for e in view["errors"]]
    finally:
        await app.state.launcher.shutdown()
        await client.aclose()
        await cm.__aexit__(None, None, None)
