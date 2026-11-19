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
    """회사·신입·경력 질의만 답하는 가상 온톨로지 (position.company·company 표, 직무명+자격요건 문장) — 실행·빌드 목록의
    회사·신입·경력 칸. careers = 공고 id → [직무명, 자격요건 문장…]."""

    def __init__(
        self,
        positions: dict[str, str],
        companies: list[str],
        careers: dict[str, list[str]] | None = None,
    ) -> None:
        self.positions, self.companies = positions, companies
        self.careers = careers or {}

    async def fetch(self, query: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        if "FROM company" in query:
            return [{"name": n} for n in self.companies]
        if "SELECT 1" in query:  # posting_exists
            return [{"ok": 1}] if params[0] in {*self.positions, *self.careers} else []
        ids = params[0] if params else []
        if "posting_line" in query:  # position_career_texts
            return [
                {"id": i, "position_name": self.careers[i][0], "required": self.careers[i][1:]}
                for i in ids  # type: ignore[attr-defined]
                if i in self.careers
            ]
        return [{"id": i, "company": self.positions[i]} for i in ids if i in self.positions]  # type: ignore[attr-defined]


class FakeProvisional:
    """임시 공고 저장소 대신 — 회사 칸, 신입·경력 추정용 제목·문장(rows), 사람이 정한 신입·경력(levels)."""

    def __init__(
        self,
        companies: dict[str, str | None],
        rows: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.companies = companies
        self.rows = rows or {}
        self.levels: dict[str, str] = {}
        self.set_by: dict[str, str] = {}

    async def get_provisional(self, pid: str) -> dict[str, object] | None:
        if pid not in self.companies:
            return None
        return {"id": pid, "company": self.companies[pid], **self.rows.get(pid, {})}

    async def career_levels(self, pids: list[str]) -> dict[str, str]:
        return {p: self.levels[p] for p in pids if p in self.levels}

    async def set_career_level(self, pid: str, level: str | None, set_by: str) -> None:
        if level is None:
            self.levels.pop(pid, None)
        else:
            self.levels[pid] = level
        self.set_by[pid] = set_by

    async def init_career_level(self, pid: str, level: str, set_by: str) -> None:
        if pid not in self.levels:
            self.levels[pid], self.set_by[pid] = level, set_by
