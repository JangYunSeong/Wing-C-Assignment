"""새 공고(경로 2) — 실제 적재 온톨로지·규칙(task_db_test)으로 (`uv run pytest -m pg`)."""

from __future__ import annotations

import json
import uuid
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
from task_spec.postings.search import CompanyFilter
from task_spec.postings.store import PostingStore
from task_spec.postings.suggest import OntologyLists, load_lists, suggest
from task_spec.prompting import PromptRegistry
from task_spec.schemas import PostingLineIn
from tests.api.conftest import make_settings, wait_status
from tests.postings.fixtures import POSTING

pytestmark = pytest.mark.pg
VERSION = "v2026-11-06f@60d5031+dirty"
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
    fake = FakeBackend()
    for _ in range(3):  # 처음 추천 · 같은 원문을 다시 올린 추천 · 원문을 고쳐 올린 추천
        fake.push(Completion(json.dumps(llm_out, ensure_ascii=False)))
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
                assert sug["career_level_candidate"] == "경력"  # 자격요건 '실무 경력 3년 이상'
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
                        "career_level": "신입",  # 확인자가 추정과 다르게 고름
                    },
                )
                assert r.status_code == 201, r.text
                pid = r.json()["posting_id"]
                assert pid.startswith("prov-") and r.json()["competencies"] > 0

                r = await client.get(
                    "/postings", params={"company": "(주) 가상테크", "pilot_only": "false"}
                )
                assert r.status_code == 200, r.text
                assert pid in {h["posting_id"] for h in r.json()}
                assert {h["company"] for h in r.json()} == {"가상테크"}
                r = await client.put(
                    f"/postings/{pid}/company",
                    json={"company": "다른회사", "set_by": "reviewer-fake"},
                )
                assert r.status_code == 200, r.text
                r = await client.get(
                    "/postings", params={"company": "가상테크", "pilot_only": "false"}
                )
                assert pid not in {h["posting_id"] for h in r.json()}
                r = await client.get(
                    "/postings", params={"company": "다른회사", "pilot_only": "false"}
                )
                assert pid in {h["posting_id"] for h in r.json()}
                await client.put(
                    f"/postings/{pid}/company", json={"company": None, "set_by": "reviewer-fake"}
                )
                assert (await client.get("/postings", params={"company": "(주)"})).json() == []

                prof = (await client.get(f"/postings/{pid}")).json()
                assert (
                    prof["source"] == "provisional"
                    and prof["sub"] == sub
                    and prof["ontology_version"] == VERSION
                )
                assert (prof["career_level"], prof["career_level_source"]) == ("신입", "confirmed")

                # 공고 찾기: 임시 공고는 회사·직무명으로, 온톨로지 공고는 시범만 기본 [제안: 2026-11-04]
                hits = (await client.get("/postings", params={"q": "가상테크 데이터"})).json()
                assert any(h["posting_id"] == pid and h["source"] == "provisional" for h in hits)
                assert next(h for h in hits if h["posting_id"] == pid)["career_level"] == "신입"
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
                assert {"career_level", "career_level_source"} <= prof_o.keys()
                # 온톨로지 공고도 정할 수 있다 (task_db posting_meta) — 지우면 공고 문구 추정으로 돌아간다
                level_url = f"/postings/{quote(first['posting_id'], safe='')}/career-level"
                put = await client.put(
                    level_url, json={"career_level": "경력", "set_by": "reviewer-fake"}
                )
                assert put.status_code == 200 and put.json()["career_level_source"] == "confirmed"
                again = (
                    await client.get(f"/postings/{quote(first['posting_id'], safe='')}")
                ).json()
                assert (again["career_level"], again["career_level_source"]) == (
                    "경력",
                    "confirmed",
                )
                cleared = await client.put(
                    level_url, json={"career_level": None, "set_by": "reviewer-fake"}
                )
                assert cleared.json()["career_level_source"] in ("posting_text", None)
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

                # 같은 회사·같은 원문 — 확정은 409 로 알리고, 다른 문제를 만들려고 일부러 따로 등록할 때만 allow_duplicate
                # [확정: 2026-11-20 사용자]. 운영: 같은 원문의 공고가 두 번 확정돼 목록에 두 줄 (문제는 서로 달랐다)
                again = {
                    "suggestion_id": sug["suggestion_id"],
                    "sub": sub,
                    "industry_id": "ind.it_si",
                    "confirmed_by": "reviewer-fake",
                }
                refused = await client.post("/postings", json=again)
                assert refused.status_code == 409
                assert refused.json()["error"]["code"] == "posting_duplicate"
                assert pid in refused.json()["error"]["message"]
                twin = await client.post("/postings", json={**again, "allow_duplicate": True})
                assert twin.status_code == 201, twin.text
                assert twin.json()["posting_id"] not in (pid, None)
                dup = await client.post(
                    "/postings/suggest",
                    data={"company": "(주) 가상테크", "title": "데이터 엔지니어", "text": POSTING},
                )
                # 확정 전에 미리 알린다 — 먼저 만든 것부터
                assert dup.json()["existing_posting_ids"] == [pid, twin.json()["posting_id"]]

                # 온톨로지에 없는 셀(data.eng|ind.platform) → 경고 + 직군 단위 NCS 로 대체한 평가 근거
                # (원문을 고쳐 다시 올린 공고 — 같은 회사·같은 제목이라 비슷한 공고로 알리지만 막지 않는다)
                edited = await client.post(
                    "/postings/suggest",
                    data={
                        "company": "가상테크",
                        "title": "데이터 엔지니어",
                        "text": POSTING + "\n- 가상 사내 데이터 카탈로그 운영",
                    },
                )
                assert edited.json()["existing_posting_ids"] == []
                assert edited.json()["similar_posting_ids"] == [pid, twin.json()["posting_id"]]
                r = await client.post(
                    "/postings",
                    json={
                        "suggestion_id": edited.json()["suggestion_id"],
                        "sub": "data.eng",
                        "industry_id": "ind.platform",
                        "confirmed_by": "reviewer-fake",
                    },
                )
                assert r.status_code == 201, r.text
                pid2 = r.json()["posting_id"]
                # 신입·경력을 고르지 않고 확인한 공고는 공고 문구 추정 — 실행을 만들려면 정해야 한다
                prof2 = (await client.get(f"/postings/{pid2}")).json()
                assert (prof2["career_level"], prof2["career_level_source"]) == (
                    "경력",
                    "posting_text",
                )
                need = await client.post("/runs", data={"posting_id": pid2})
                assert need.status_code == 422
                assert need.json()["error"]["code"] == "career_level_required"
                r = await client.post("/runs", data={"posting_id": pid2, "career_level": "경력"})
                assert r.status_code == 202, r.text
                await wait_status(client, r.json()["run_id"], "awaiting_approval", "failed")
                st2 = (
                    await graph.aget_state({"configurable": {"thread_id": r.json()["run_id"]}})
                ).values
                assert "posting_cell_unseen" in [e.code for e in st2["errors"]]
                prof2 = (await client.get(f"/postings/{pid2}")).json()
                assert (prof2["career_level"], prof2["career_level_source"]) == (
                    "경력",
                    "confirmed",
                )
                units = [u for e in st2["evidence"] for u in e.ncs_units]
                assert units and {u.scope for u in units} == {"sub_fallback"}
            await app.state.launcher.shutdown()
    finally:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            await conn.execute(
                "DELETE FROM public.posting_meta WHERE posting_id IN "
                "(SELECT id FROM public.provisional_posting WHERE company = '가상테크')"
            )
            await conn.execute("DELETE FROM public.provisional_posting WHERE company = '가상테크'")
            await conn.execute(
                "DELETE FROM public.posting_suggestion WHERE company IN ('가상테크', '(주) 가상테크')"
            )
        await store.close()


async def test_posting_company_set_clear_and_init() -> None:
    """사람이 정한 공고의 회사 (posting_company) [2026-11-19 사용자] — 정하기·지우기·실행 만들기의 처음만 넣기."""
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await PostingStore.open(u.get_secret_value())
    pid = f"prov-test{uuid.uuid4().hex[:8]}"
    try:
        assert await store.entered_companies([pid]) == {}
        await store.init_company(pid, "(주)가상회사", "run:a")
        await store.init_company(pid, "다른회사", "run:b")  # 이미 있으면 덮지 않는다
        assert await store.entered_companies([pid, "prov-none"]) == {pid: "(주)가상회사"}
        await store.set_company(pid, "다른회사", "reviewer-fake")
        assert await store.entered_companies([pid]) == {pid: "다른회사"}
        await store.set_company(pid, None, "reviewer-fake")
        assert await store.entered_companies([pid]) == {}
        assert await store.entered_companies([]) == {}
    finally:
        async with store._pool.connection() as conn:
            await conn.execute("DELETE FROM public.posting_company WHERE posting_id = %s", (pid,))
        await store.close()


async def test_search_provisional_company_filter() -> None:
    """한 회사 공고만 (GET /postings?company=) [2026-11-20 사용자] — 법인 표기·공백은 무시, 사람이 정한 회사가 먼저."""
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await PostingStore.open(u.get_secret_value())
    tag = uuid.uuid4().hex[:6]
    mine, other = f"인터뷰회사{tag}", f"다른회사{tag}"
    sid = await store.create_suggestion(
        text="t",
        company=mine,
        title="t",
        method="test",
        lines=[],
        subs=[],
        industries=[],
        meta={},
        ontology_version=VERSION,
    )

    async def make(company: str | None) -> str:
        return await store.create_provisional(
            suggestion_id=sid,
            company=company,
            title="백엔드",
            sub="sw.backend",
            secondary_sub=None,
            industry_id="it",
            confirmed_by="tester",
            ontology_version=VERSION,
            rules_sha256="x",
            lines=[],
        )

    by_data = await make(f"(주) {mine}")
    by_hand = await make(None)
    moved = await make(mine)
    elsewhere = await make(other)
    try:
        await store.set_company(by_hand, f"주식회사{mine}", "tester")
        await store.set_company(moved, other, "tester")
        entered = await store.all_entered_companies()
        assert entered[by_hand] == f"주식회사{mine}" and entered[moved] == other
        only = CompanyFilter(key=mine.casefold(), include=(by_hand,), exclude=(moved,))
        rows = await store.search_provisional([], None, only)
        assert {r["id"] for r in rows} == {by_data, by_hand}
        assert elsewhere not in {r["id"] for r in await store.search_provisional([], None, only)}
        assert len(await store.search_provisional([], 1, only)) == 1
    finally:
        ids = [by_data, by_hand, moved, elsewhere]
        async with store._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.posting_company WHERE posting_id = ANY(%s)", (ids,)
            )
            await conn.execute("DELETE FROM public.provisional_posting WHERE id = ANY(%s)", (ids,))
            await conn.execute("DELETE FROM public.posting_suggestion WHERE id = %s", (sid,))
        await store.close()


async def test_same_posting_check_and_insert_are_atomic() -> None:
    """확정을 두 번 눌러도(동시 요청) 같은 회사·같은 원문 공고는 하나만 (2026-11-20 리뷰) — 검사와 저장이 한 트랜잭션·잠금."""
    import asyncio

    from task_spec.postings.store import SamePostingExists, text_sha256

    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await PostingStore.open(u.get_secret_value())
    tag = uuid.uuid4().hex[:6]
    text = f"가상 원문 {tag}"
    sid = await store.create_suggestion(
        text=text,
        company=f"가상중복{tag}",
        title="t",
        method="test",
        lines=[],
        subs=[],
        industries=[],
        meta={},
        ontology_version=VERSION,
    )
    key = (f"가상중복{tag}".casefold(), text_sha256(text))

    async def make() -> str | Exception:
        try:
            return await store.create_provisional(
                suggestion_id=sid,
                company=f"가상중복{tag}",
                title="t",
                sub="sw.backend",
                secondary_sub=None,
                industry_id="it",
                confirmed_by="tester",
                ontology_version=VERSION,
                rules_sha256="x",
                lines=[],
                unique=key,
            )
        except SamePostingExists as e:
            return e

    try:
        results = await asyncio.gather(*(make() for _ in range(4)))
        made = [r for r in results if isinstance(r, str)]
        refused = [r for r in results if isinstance(r, SamePostingExists)]
        assert len(made) == 1 and len(refused) == 3
        assert all(r.posting_ids == made for r in refused)
        assert await store.same_postings(key[0], key[1]) == made
    finally:
        async with await psycopg.AsyncConnection.connect(
            u.get_secret_value(), autocommit=True
        ) as conn:
            await conn.execute(
                "DELETE FROM public.provisional_posting WHERE suggestion_id = %s", (sid,)
            )
            await conn.execute("DELETE FROM public.posting_suggestion WHERE id = %s", (sid,))
        await store.close()


async def test_posting_link_group_and_choosable_tasks(ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """같은 공고 묶음 (docs/design/posting-link.md) — 잇기·사슬·회사 검사, 묶음의 승인된 문제에서 고른다
    [확정: 2026-11-20 사용자]. 공통 과제·사례·빌드는 옮기지 않는다."""
    from datetime import UTC, datetime

    from task_spec.spec.store import CommonKey, InMemoryCommonStore
    from tests.spec.test_interview_common import _task

    url, repo, lists = ctx
    store = await PostingStore.open(url)
    tag = uuid.uuid4().hex[:6]
    company = f"가상잇기{tag}"
    sid = await store.create_suggestion(
        text=f"t{tag}",
        company=company,
        title="t",
        method="test",
        lines=[],
        subs=[],
        industries=[],
        meta={},
        ontology_version=VERSION,
    )

    async def make(name: str | None) -> str:
        return await store.create_provisional(
            suggestion_id=sid,
            company=name,
            title="백엔드",
            sub="sw.backend",
            secondary_sub=None,
            industry_id="it",
            confirmed_by="tester",
            ontology_version=VERSION,
            rules_sha256="x",
            lines=[],
        )

    a, b, other = await make(company), await make(f"(주) {company}"), await make(f"다른{tag}")
    blank = await make(None)  # 회사가 정해지지 않은 공고
    common = InMemoryCommonStore()
    version = repo.version.version
    approved: dict[str, str] = {}
    for pid, title in ((a, "가상 문제 A"), (b, "가상 문제 B")):
        row = await common.save_draft(
            CommonKey(pid, version, f"draft_feature_task@11+{title}"), _task(title), []
        )
        await common.approve(row.id, "reviewer-fake", datetime(2026, 11, 20, tzinfo=UTC))
        approved[pid] = row.id
    try:
        app = create_app(settings=make_settings(tmp_path))
        app.state.ontology = repo
        app.state.postings = PostingsContext(store=store, lists=lists, llm=None, prompts=PROMPTS)  # type: ignore[arg-type]
        app.state.common_store = common
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:

            async def link(pid: str, to: str) -> httpx.Response:
                return await client.put(
                    f"/postings/{pid}/link", json={"linked_to": to, "linked_by": "tester"}
                )

            async def tasks(pid: str) -> list[str]:
                return [t["common_id"] for t in (await client.get(f"/postings/{pid}/tasks")).json()]

            assert await tasks(b) == [approved[b]]  # 잇기 전 — 자기 공고 문제만
            r = await link(b, a)
            assert r.status_code == 200, r.text
            assert r.json()["group"] == [a, b]
            assert (await link(b, a)).status_code == 200  # 같은 기준이면 그대로
            assert sorted(await tasks(b)) == sorted(approved.values())  # 묶음의 문제 모두
            assert sorted(await tasks(a)) == sorted(approved.values())
            # 사슬·회사가 다른 공고·자기 자신·없는 공고
            for pid, to, code in (
                (a, b, 409),  # b 는 a 에 이어진 공고 — 기준 공고에 잇는다
                (other, b, 409),
                (other, a, 409),  # 회사가 다르다
                (a, a, 409),
                (blank, a, 409),  # 회사가 없는 공고는 잇지 않는다 [확정: 2026-11-20 사용자]
                (b, "prov-000000000000", 404),
            ):
                assert (await link(pid, to)).status_code == code, (pid, to)
            # 묶음에 든 공고의 회사는 다른 회사로 바꾸지 않는다 (같은 회사의 다른 표기는 된다)
            moved = await client.put(
                f"/postings/{b}/company", json={"company": f"다른{tag}", "set_by": "tester"}
            )
            assert moved.status_code == 409
            assert moved.json()["error"]["code"] == "posting_link_conflict"
            same = await client.put(
                f"/postings/{b}/company",
                json={"company": f"주식회사 {company}", "set_by": "tester"},
            )
            assert same.status_code == 200, same.text
            gone = await client.delete(f"/postings/{b}/link")
            assert gone.status_code == 200 and gone.json()["group"] == [b]
            assert await tasks(b) == [approved[b]]
    finally:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            await conn.execute(
                "DELETE FROM public.posting_link WHERE posting_id = ANY(%s) OR linked_to = ANY(%s)",
                ([a, b, other, blank], [a, b, other, blank]),
            )
            await conn.execute(
                "DELETE FROM public.posting_company WHERE posting_id = ANY(%s)",
                ([a, b, other, blank],),
            )
            await conn.execute(
                "DELETE FROM public.provisional_posting WHERE suggestion_id = %s", (sid,)
            )
            await conn.execute("DELETE FROM public.posting_suggestion WHERE id = %s", (sid,))
        await store.close()
