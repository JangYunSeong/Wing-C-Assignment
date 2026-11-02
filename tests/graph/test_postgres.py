"""Postgres 통합 테스트 — task_db_test 에서만 (`uv run pytest -m pg`)."""

from __future__ import annotations

import pytest
from langgraph.types import Command
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from task_spec.graph import build_graph, make_checkpointer
from tests.conftest import config_for, decision, fake_doc, run_input

pytestmark = pytest.mark.pg


class _DbUrls(BaseSettings):
    """DB URL 두 개만 읽는다 (LLM 키 없이도 통합 테스트가 돌도록)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: SecretStr | None = None
    test_database_url: SecretStr | None = None


@pytest.fixture(scope="module")
def test_db_url() -> str:
    urls = _DbUrls()
    if urls.test_database_url is None:
        pytest.skip("TEST_DATABASE_URL 이 없습니다")
    test_url = urls.test_database_url.get_secret_value()
    if urls.database_url is not None and test_url == urls.database_url.get_secret_value():
        pytest.fail(
            "TEST_DATABASE_URL 이 DATABASE_URL 과 같습니다 — 운영 DB 로 테스트하지 않습니다"
        )
    return test_url


async def test_interrupt_resume_roundtrip_on_postgres(test_db_url: str) -> None:
    inp = run_input(docs=[fake_doc("d1", "resume"), fake_doc("d2", "cover_letter")])
    cfg = config_for(inp)
    async with make_checkpointer(kind="postgres", conn_string=test_db_url) as cp:
        try:
            graph = build_graph(cp)
            first = await graph.ainvoke(inp, cfg)
            assert first["__interrupt__"]

            # 새 그래프 인스턴스로 이어 받기 = 프로세스 재시작 후 resume 과 같은 상황
            graph2 = build_graph(cp)
            snapshot = await graph2.aget_state(cfg)
            assert snapshot.next == ("approval",)
            assert snapshot.values["spec"].revision == 1

            final = await graph2.ainvoke(Command(resume=decision("approved")), cfg)
            assert final["spec"].approval.status == "approved"
        finally:
            await cp.adelete_thread(inp["run_id"])
        assert (await cp.aget_tuple(cfg)) is None
