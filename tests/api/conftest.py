"""API 테스트 픽스처. 파일 내용은 전부 가상 ("가상 데이터")."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from langgraph.graph.state import CompiledStateGraph
from pydantic import SecretStr

from task_spec.api.app import create_app
from task_spec.config import Settings
from task_spec.graph import build_graph, make_checkpointer
from task_spec.nodes.pseudonymize import Pseudonymizer, stub_pseudonymizer

FAKE_OPENAI_KEY = "sk-fake-openai-0000"
FAKE_OPENCODE_KEY = "oc-fake-opencode-0000"

FAKE_FILE = ("resume.txt", "가상 데이터".encode(), "text/plain")


def make_settings(data_dir: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": SecretStr("postgresql://fake:fake@127.0.0.1:1/fake"),
        "checkpointer": "memory",
        "ontology_version": "onto-v2026.11.02",
        "ontology_artifact_dir": data_dir / "ontology",
        "data_dir": data_dir,
        "openai_api_key": SecretStr(FAKE_OPENAI_KEY),
        "opencode_api_key": SecretStr(FAKE_OPENCODE_KEY),
        "opencode_base_url": "http://127.0.0.1:1/v1",
        "upload_max_bytes": 1024,
        "sse_poll_seconds": 0.02,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def reviewer_decision(status: str, comment: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reviewer": "reviewer-fake",
        "comment": comment,
        "at": datetime.now(UTC).isoformat(),
    }


async def wait_status(client: httpx.AsyncClient, run_id: str, *wanted: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            body = (await client.get(f"/runs/{run_id}")).json()
            if body["status"] in wanted:
                return body  # type: ignore[no-any-return]
            await asyncio.sleep(0.02)


AppFactory = Callable[..., Awaitable[httpx.AsyncClient]]


@pytest.fixture
async def make_client(tmp_path: Path) -> AsyncIterator[AppFactory]:
    """(가명화기, 설정 덮어쓰기)를 받아 클라이언트를 만든다. 종료 시 런처·체크포인터 정리."""
    apps: list[FastAPI] = []
    clients: list[httpx.AsyncClient] = []
    cms: list[Any] = []

    async def _make(
        pseudonymizer: Pseudonymizer = stub_pseudonymizer, **overrides: Any
    ) -> httpx.AsyncClient:
        cm = make_checkpointer(kind="memory")
        cp = await cm.__aenter__()
        cms.append(cm)
        graph: CompiledStateGraph = build_graph(cp, pseudonymizer=pseudonymizer)
        app = create_app(settings=make_settings(tmp_path, **overrides), graph=graph)
        apps.append(app)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        clients.append(client)
        return client

    yield _make
    for app in apps:
        await app.state.launcher.shutdown()
    for c in clients:
        await c.aclose()
    for cm in cms:
        await cm.__aexit__(None, None, None)


@pytest.fixture
async def client(make_client: AppFactory) -> httpx.AsyncClient:
    return await make_client()


class FakeOntology:
    """회사 질의만 답하는 가상 온톨로지 (position.company·company 표) — 실행·빌드 목록의 회사 칸."""

    def __init__(self, positions: dict[str, str], companies: list[str]) -> None:
        self.positions, self.companies = positions, companies

    async def fetch(self, query: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        if "FROM company" in query:
            return [{"name": n} for n in self.companies]
        ids = params[0] if params else []
        return [{"id": i, "company": self.positions[i]} for i in ids if i in self.positions]  # type: ignore[attr-defined]


class FakeProvisional:
    """임시 공고 저장소 대신 — 회사 칸만."""

    def __init__(self, companies: dict[str, str | None]) -> None:
        self.companies = companies

    async def get_provisional(self, pid: str) -> dict[str, object] | None:
        return {"id": pid, "company": self.companies[pid]} if pid in self.companies else None
