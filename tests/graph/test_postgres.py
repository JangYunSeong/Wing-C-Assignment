"""Postgres 통합 테스트 — task_db_test 에서만 (`uv run pytest -m pg`)."""

from __future__ import annotations

import anyio
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


async def test_cancelled_query_does_not_break_later_requests(test_db_url: str) -> None:
    # 2026-11-05 운영: SSE 연결이 끊겨 조회가 anyio 취소 범위로 끊긴 뒤, 연결 하나가 "another command is
    # already in progress" 상태로 남아 실행 조회·헬스체크가 재시작 전까지 전부 실패했다.
    # asyncio task.cancel() 로는 재현되지 않는다 — sse-starlette 처럼 anyio 취소 범위로 끊어야 한다.
    from task_spec.api.routers.health import _ping

    cfg = {"configurable": {"thread_id": "cancel-probe", "checkpoint_ns": ""}}
    async with make_checkpointer(kind="postgres", conn_string=test_db_url) as cp:
        with anyio.move_on_after(0.3) as scope:
            async with cp._cursor() as cur:
                await cur.execute("SELECT pg_sleep(3)")
        assert scope.cancelled_caught

        assert await cp.aget_tuple(cfg) is None
        assert await _ping(cp)


async def test_build_approval_roundtrip_on_postgres(test_db_url: str) -> None:
    """빌드 승인 그래프 (#130) — 실제 체크포인터로 interrupt → (새 그래프 인스턴스 = 재시작) → 판단이 빠진 resume(다시 기다림)
    → 승인. State 의 승인 결정·판단 모델이 직렬화 허용 목록을 지나 저장·복원되는지 본다. 가상 과제만."""
    import uuid
    from datetime import UTC, datetime

    from task_spec.graph.build_approval import build_approval_graph, outcome_key, thread_id
    from task_spec.schemas.build_record import README_PATH, StarterFile
    from task_spec.schemas.package import ReviewDecision, flag_id
    from task_spec.spec.store import CommonKey, CommonTaskStore
    from task_spec.starter.store import PgBuildStore
    from tests.spec.test_store_pg import _task
    from tests.starter.test_build_store import FLAG, H_OLD, _artifact, _outcome

    common = await CommonTaskStore.open(test_db_url)
    store = await PgBuildStore.open(test_db_url)
    key = CommonKey(f"test-{uuid.uuid4().hex[:8]}", "onto-test", "draft_task@1")
    row = await common.save_draft(key, _task(), [])
    b = await store.new_build(row.id, "onto-test")
    await store.set_status(b.build_id, "generating")
    await store.put_readme(StarterFile.of(b.build_id, "readme", README_PATH, "# 가상"))
    await store.save_outcome(b.build_id, _outcome(H_OLD))
    await store.set_status(b.build_id, "verifying")
    b = await store.set_status(b.build_id, "review")
    for role in ("candidate", "grader"):
        await store.put_artifact(_artifact(b.build_id, role, H_OLD))  # type: ignore[arg-type]
    cfg = {"configurable": {"thread_id": thread_id(b)}}
    good = {
        "status": "approved",
        "reviewer": "가상승인자",
        "at": datetime(2026, 11, 12, tzinfo=UTC).isoformat(),
        "decisions": [
            ReviewDecision(
                flag_id=flag_id(FLAG), flag=FLAG, verdict="accepted_risk", reviewer="가상승인자"
            ).model_dump(mode="json")
        ],
    }
    async with make_checkpointer(kind="postgres", conn_string=test_db_url) as cp:
        try:
            first = await build_approval_graph(store, cp).ainvoke(
                {"build_id": b.build_id, "outcome_key": outcome_key(b)}, cfg
            )
            assert first["__interrupt__"]
            graph2 = build_approval_graph(store, cp)  # 재시작 뒤
            await graph2.ainvoke(Command(resume={**good, "decisions": []}), cfg)
            snap = await graph2.aget_state(cfg)
            assert snap.next == ("build_approval",)
            final = await graph2.ainvoke(Command(resume=good), cfg)
            assert final["result"] == "approved"
            assert final["decision"].decisions[0].verdict == "accepted_risk"  # 모델로 복원
            done = await store.get(b.build_id)
            assert done is not None and done.status == "approved"
        finally:
            await cp.adelete_thread(thread_id(b))
            async with store._pool.connection() as conn:
                for sql in (
                    "DELETE FROM public.build_artifact WHERE build_id = %s",
                    "DELETE FROM public.starter_file WHERE build_id = %s",
                    "DELETE FROM public.common_task_build WHERE id = %s",
                ):
                    await conn.execute(sql, (b.build_id,))  # type: ignore[arg-type]
                await conn.execute(
                    "DELETE FROM public.common_task WHERE posting_id = %s", (key.posting_id,)
                )
            await store.close()
            await common.close()
