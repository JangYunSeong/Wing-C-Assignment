"""① 공고 프로필·③ 평가 근거 — 실제 적재 온톨로지(task_db_test)로 (`uv run pytest -m pg`)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from langgraph.types import Command
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from task_spec.api.app import create_app
from task_spec.graph import build_graph, make_checkpointer
from task_spec.ontology.queries import evidence, posting_exists, posting_profile
from task_spec.ontology.repository import OntologyNotLoaded, OntologyRepository, resolve_version
from tests.api.conftest import make_settings, reviewer_decision, wait_status
from tests.conftest import config_for

pytestmark = pytest.mark.pg
VERSION = "v2026-10-30e@b69d00f+dirty"
POSTING = "LIG_2026_2#001"


class _Urls(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    test_database_url: SecretStr | None = None


@pytest.fixture
async def repo() -> AsyncIterator[OntologyRepository]:
    url = _Urls().test_database_url
    if url is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    try:
        await resolve_version(url.get_secret_value(), VERSION)
    except OntologyNotLoaded:
        pytest.skip("실제 온톨로지 미적재 (load_pg --target test)")
    r = await OntologyRepository.open(url.get_secret_value(), VERSION)
    yield r
    await r.close()


async def test_profile_cell_and_primary(repo: OntologyRepository) -> None:
    p = await posting_profile(repo, POSTING)
    assert p is not None and p.source == "ontology" and p.ontology_version == VERSION
    assert p.cell_id == "sw.embedded|ind.defense"
    assert p.competencies and all(c.line_ids for c in p.competencies)
    for c in p.competencies:  # 주/보조: 담당업무·자격요건 = 주, 우대만 = 보조
        assert c.primary == bool({"duties", "required"} & set(c.sections))


async def test_preferred_only_is_supporting(repo: OntologyRepository) -> None:
    rows = await repo.fetch(
        """
        SELECT l.position_id FROM posting_line l JOIN line_competency lc ON lc.line_id = l.id
        GROUP BY l.position_id, lc.competency_id HAVING bool_and(l.section = 'preferred') LIMIT 1
        """
    )
    p = await posting_profile(repo, rows[0]["position_id"])
    assert p is not None
    assert any(not c.primary and c.sections == ["preferred"] for c in p.competencies)


async def test_unknown_posting(repo: OntologyRepository) -> None:
    assert await posting_profile(repo, "없는-공고") is None
    assert not await posting_exists(repo, "없는-공고")


async def test_evidence_matches_ontology_view(repo: OntologyRepository) -> None:
    positions = await repo.fetch(
        "SELECT id FROM position WHERE sub IS NOT NULL AND industry_id IS NOT NULL ORDER BY id LIMIT 40"
    )
    for r in positions:
        p = await posting_profile(repo, r["id"])
        assert p is not None and p.sub and p.industry_id
        ev = await evidence(
            repo,
            p.competency_ids,
            sub=p.sub,
            industry_id=p.industry_id,
            secondary_sub=p.secondary_sub,
            in_pilot=p.in_pilot,
        )
        mine = {(e.competency_id, u.unit_cl_cd, u.scope) for e in ev for u in e.ncs_units}
        view = await repo.fetch(
            "SELECT competency_id, unit_cl_cd, scope FROM v_position_competency_ncs WHERE position_id = %s AND scope IS NOT NULL",
            (r["id"],),
        )
        assert mine == {(v["competency_id"], v["unit_cl_cd"], v["scope"]) for v in view}, r["id"]


async def test_out_of_cell_units_excluded_and_esco_representative(repo: OntologyRepository) -> None:
    p = await posting_profile(repo, POSTING)
    assert p is not None and p.sub and p.industry_id
    ev = await evidence(repo, p.competency_ids, sub=p.sub, industry_id=p.industry_id)
    all_units = await repo.fetch(
        "SELECT count(*) AS n FROM competency_ncs WHERE competency_id = ANY(%s)",
        (p.competency_ids,),
    )
    assert sum(len(e.ncs_units) for e in ev) <= all_units[0]["n"]
    for e in ev:
        for u in e.ncs_units:
            assert u.scope in ("primary", "secondary") and u.n_criteria >= 0
        for x in e.esco:
            assert x.rel != "broad"  # 대표 연결만 (broad 는 대표가 아님)


async def test_graph_common_tasks_path_uses_ontology(repo: OntologyRepository) -> None:
    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(cp, ontology=repo)
        inp = {
            "run_id": f"run-{uuid.uuid4().hex[:8]}",
            "ontology_version": VERSION,
            "posting_id": POSTING,
            "documents": [],
        }
        cfg = config_for(inp)
        result = await graph.ainvoke(inp, cfg)
        assert result["__interrupt__"]
        state = (await graph.aget_state(cfg)).values
        assert state["posting_profile"].cell_id == "sw.embedded|ind.defense"
        assert any(e.ncs_units for e in state["evidence"])  # 문서 없이도 공고 역량의 평가 근거
        final = await graph.ainvoke(Command(resume=reviewer_decision("approved")), cfg)
        assert final["spec"].ontology_version == VERSION


async def test_graph_unknown_posting_stops(repo: OntologyRepository) -> None:
    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(cp, ontology=repo)
        inp = {
            "run_id": f"run-{uuid.uuid4().hex[:8]}",
            "ontology_version": VERSION,
            "posting_id": "없는-공고",
            "documents": [],
        }
        result = await graph.ainvoke(inp, config_for(inp))
        assert not result.get("__interrupt__") and result.get("spec") is None
        assert [e.code for e in result["errors"]] == ["posting_not_found"]


async def test_api_rejects_unknown_posting_and_records_real_version(
    repo: OntologyRepository, tmp_path: Path
) -> None:
    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(cp, ontology=repo)
        app = create_app(settings=make_settings(tmp_path, ontology_version="dev"), graph=graph)
        app.state.ontology = repo
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            r = await client.post("/runs", data={"posting_id": "없는-공고"})
            assert r.status_code == 404 and r.json()["error"]["code"] == "posting_not_found"
            r = await client.post("/runs", data={"posting_id": POSTING})
            assert r.status_code == 202
            run_id = r.json()["run_id"]
            view = await wait_status(client, run_id, "awaiting_approval", "failed")
            assert view["status"] == "awaiting_approval", view
            state = (await graph.aget_state({"configurable": {"thread_id": run_id}})).values
            assert state["ontology_version"] == VERSION  # 'dev' 가 아니라 실제 버전
        await app.state.launcher.shutdown()
