"""새 공고(경로 2) — 실제 적재 온톨로지·규칙(task_db_test)으로 (`uv run pytest -m pg`)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import httpx
import psycopg
import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from task_spec.api.app import create_app
from task_spec.api.deps import PostingsContext
from task_spec.graph import build_graph, make_checkpointer
from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.ontology.repository import OntologyNotLoaded, OntologyRepository, resolve_version
from task_spec.postings.store import PostingStore
from task_spec.postings.suggest import OntologyLists, load_lists, suggest
from task_spec.prompting import PromptRegistry
from task_spec.schemas import PostingLineIn
from tests.api.conftest import make_settings, wait_status
from tests.postings.fixtures import POSTING

pytestmark = pytest.mark.pg
VERSION = "v2026-10-30e@b69d00f+dirty"
PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


class _Urls(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    test_database_url: SecretStr | None = None


@pytest.fixture
async def ctx() -> AsyncIterator[tuple[str, OntologyRepository, OntologyLists]]:
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    url = u.get_secret_value()
    try:
        await resolve_version(url, VERSION)
    except OntologyNotLoaded:
        pytest.skip("실제 온톨로지 미적재")
    repo = await OntologyRepository.open(url, VERSION)
    lists = await load_lists(repo)
    if lists.matcher is None:
        await repo.close()
        pytest.skip("매칭 규칙 미적재 (load_pg --rules-root)")
    yield url, repo, lists
    await repo.close()


async def test_matcher_parity_with_ontology_labels(ctx) -> None:  # type: ignore[no-untyped-def]
    """사본 매처 = 온톨로지 결과 (시범 직군 전 공고 문장: 종류·역량·수준)."""
    _, repo, lists = ctx
    rows = await repo.fetch(
        """
        SELECT l.text, l.kind, l.level, p.sub,
               coalesce(array_agg(lc.competency_id) FILTER (WHERE lc.competency_id IS NOT NULL), '{}') AS comps
          FROM posting_line l JOIN position p ON p.id = l.position_id
          LEFT JOIN line_competency lc ON lc.line_id = l.id
         WHERE p.in_pilot AND l.kind NOT IN ('out_of_scope', 'skip')
         GROUP BY l.id, l.text, l.kind, l.level, p.sub
        """
    )
    assert len(rows) > 2000
    bad = []
    for r in rows:
        c = lists.matcher.classify(r["text"], r["sub"])  # type: ignore[union-attr]
        if (c["kind"], set(c["comps"]), c["level"] or "") != (
            r["kind"],
            set(r["comps"]),
            r["level"] or "",
        ):
            bad.append(r["text"][:40])
    assert bad == [], bad[:5]


async def test_rules_keep_key_order(ctx) -> None:  # type: ignore[no-untyped-def]
    """jsonb 는 키 순서를 바꾼다 — 수준 규칙은 순서가 우선순위라 json 으로 저장해야 한다."""
    _, _, lists = ctx
    assert [k for k, _ in lists.matcher.levels] == ["수행", "활용", "이해"]  # type: ignore[union-attr]
    assert lists.matcher.level("사용 경험 및 이해") == "활용"  # type: ignore[union-attr]


async def test_suggest_known_company_with_self_excluded(ctx) -> None:  # type: ignore[no-untyped-def]
    _, repo, lists = ctx
    p = (
        await repo.fetch(
            "SELECT id, company, job, position_name, sub, industry_id FROM position WHERE id = 'kakao_2026#001'"
        )
    )[0]
    lines = [
        PostingLineIn(text=r["text"], section=r["section"])
        for r in await repo.fetch(
            "SELECT text, section FROM posting_line WHERE position_id = %s", (p["id"],)
        )
    ]
    subs, ind, _ = await suggest(
        lists,
        company=p["company"],
        title=p["job"],
        text="",
        lines=lines,
        llm=None,
        prompts=None,
        exclude_positions=frozenset({p["id"]}),
    )
    assert ind[0].id == p["industry_id"]  # 등록된 회사 → 업종
    assert p["sub"] in [c.id for c in subs]  # 직군이 상위 3 안 (자기 자신 제외)
    assert all(p["id"] not in r for c in subs for r in c.reasons)


async def test_api_flow_suggest_confirm_run(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url, repo, lists = ctx
    llm_out = {
        "sub_ids": ["data.eng"],
        "industry_id": "ind.platform",
        "reason": "데이터 파이프라인 구축",
    }
    fake = FakeBackend().push(Completion(json.dumps(llm_out, ensure_ascii=False)))
    llm = LLMClient({"openai": fake})
    store = await PostingStore.open(url)
    try:
        async with make_checkpointer(kind="memory") as cp:
            graph = build_graph(cp, ontology=repo, postings=store, ontology_lists=lists)
            app = create_app(settings=make_settings(tmp_path), graph=graph)
            app.state.ontology = repo
            app.state.postings = PostingsContext(store=store, lists=lists, llm=llm, prompts=PROMPTS)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as client:
                r = await client.post(
                    "/postings/suggest",
                    data={"company": "가상테크", "title": "데이터 엔지니어", "text": POSTING},
                )
                assert r.status_code == 201, r.text
                sug = r.json()
                assert (
                    sug["segment_method"] == "rules" and sug["llm_used"]
                )  # 모르는 회사 → 업종은 LLM
                assert sug["industry_candidates"][0]["id"] == "ind.platform"
                sub = "data.eng"  # 확인자가 고른 값 — 온톨로지에 있는 셀(data.eng|ind.it_si)로 근거까지 확인

                bad = await client.post(
                    "/postings",
                    json={
                        "suggestion_id": sug["suggestion_id"],
                        "sub": "없는직군",
                        "industry_id": "ind.platform",
                        "confirmed_by": "reviewer-fake",
                    },
                )
                assert bad.status_code == 422

                r = await client.post(
                    "/postings",
                    json={
                        "suggestion_id": sug["suggestion_id"],
                        "sub": sub,
                        "industry_id": "ind.it_si",
                        "confirmed_by": "reviewer-fake",
                    },
                )
                assert r.status_code == 201, r.text
                pid = r.json()["posting_id"]
                assert pid.startswith("prov-") and r.json()["competencies"] > 0

                prof = (await client.get(f"/postings/{pid}")).json()
                assert (
                    prof["source"] == "provisional"
                    and prof["sub"] == sub
                    and prof["ontology_version"] == VERSION
                )

                # 공고 찾기: 임시 공고는 회사·직무명으로, 온톨로지 공고는 시범만 기본 [제안: 2026-11-04]
                hits = (await client.get("/postings", params={"q": "가상테크 데이터"})).json()
                assert any(h["posting_id"] == pid and h["source"] == "provisional" for h in hits)
                onto = (await client.get("/postings", params={"q": "", "limit": 5})).json()
                onto = [h for h in onto if h["source"] == "ontology"]
                assert onto and all(h["in_pilot"] and h["company"] for h in onto)
                first = onto[0]
                narrowed = (
                    await client.get(
                        "/postings", params={"q": f"{first['company']} {first['sub']}"}
                    )
                ).json()
                assert any(h["posting_id"] == first["posting_id"] for h in narrowed)
                # 직군 이름으로도 — 시범 sw.backend 공고의 직무명은 영어가 많다
                be = (await client.get("/postings", params={"q": "백엔드"})).json()
                assert any(h["sub"] == "sw.backend" and h["source"] == "ontology" for h in be)
                # LIKE 와일드카드는 글자 그대로 — '%' 가 들어간 공고는 없다
                assert (await client.get("/postings", params={"q": "%"})).json() == []
                prof_o = (
                    await client.get(f"/postings/{quote(first['posting_id'], safe='')}")
                ).json()
                assert prof_o["source"] == "ontology" and prof_o["company"] == first["company"]
                assert (
                    await client.get(f"/postings/{quote('없는공고#999', safe='')}")
                ).status_code == 404

                r = await client.post("/runs", data={"posting_id": pid})
                assert r.status_code == 202
                view = await wait_status(client, r.json()["run_id"], "awaiting_approval", "failed")
                assert view["status"] == "awaiting_approval", view
                state = (
                    await graph.aget_state({"configurable": {"thread_id": r.json()["run_id"]}})
                ).values
                assert state["posting_profile"].source == "provisional"
                assert any(e.ncs_units for e in state["evidence"])  # 임시 공고도 셀 범위 평가 근거

                assert (
                    await client.post("/runs", data={"posting_id": "prov-000000000000"})
                ).status_code == 404

                # 온톨로지에 없는 셀(data.eng|ind.platform) → 경고 + 직군 단위 NCS 로 대체한 평가 근거
                r = await client.post(
                    "/postings",
                    json={
                        "suggestion_id": sug["suggestion_id"],
                        "sub": "data.eng",
                        "industry_id": "ind.platform",
                        "confirmed_by": "reviewer-fake",
                    },
                )
                r = await client.post("/runs", data={"posting_id": r.json()["posting_id"]})
                await wait_status(client, r.json()["run_id"], "awaiting_approval", "failed")
                st2 = (
                    await graph.aget_state({"configurable": {"thread_id": r.json()["run_id"]}})
                ).values
                assert "posting_cell_unseen" in [e.code for e in st2["errors"]]
                units = [u for e in st2["evidence"] for u in e.ncs_units]
                assert units and {u.scope for u in units} == {"sub_fallback"}
            await app.state.launcher.shutdown()
    finally:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            await conn.execute("DELETE FROM public.provisional_posting WHERE company = '가상테크'")
            await conn.execute("DELETE FROM public.posting_suggestion WHERE company = '가상테크'")
        await store.close()
