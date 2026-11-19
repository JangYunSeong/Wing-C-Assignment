"""실행 만들 때 담당자가 문제를 고른다 [확정: 2026-11-20 사용자 — 같은 공고에 문제가 여럿이면] — GET /postings/{id}/tasks ·
POST /runs 의 common_id. ASGI 앱 + memory 체크포인터 + InMemoryCommonStore. 가상 데이터만.
공고 묶음(이어진 공고)은 공고 저장소가 있어야 해서 tests/postings/test_postings_pg.py 에서 본다."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from task_spec.nodes.spec import FLAG_CHOSEN_COMMON
from task_spec.spec.store import CommonKey, InMemoryCommonStore, StoredCommon
from tests.api.conftest import FAKE_FILE, reviewer_decision, wait_status
from tests.api.test_runs_interview import ONTO, POSTING, _interview, api  # noqa: F401 — 픽스처
from tests.spec.test_interview_common import _task


async def _generated(
    store: InMemoryCommonStore, title: str, *, approve: bool = True
) -> StoredCommon:
    """공고 문구로 만든 공통 과제 (인터뷰 과제 아님)."""
    key = CommonKey(POSTING, ONTO, f"draft_feature_task@11+가상#{title}")
    row = await store.save_draft(key, _task(title), [])
    if approve:
        await store.approve(row.id, "task-reviewer-fake", datetime(2026, 11, 20, tzinfo=UTC))
    return row


async def _run(client: httpx.AsyncClient, **data: str) -> httpx.Response:
    return await client.post(
        "/runs", data={"posting_id": POSTING, **data}, files=[("resumes", FAKE_FILE)]
    )


async def test_tasks_lists_every_approved_problem_of_the_posting(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    client, store = api
    iv = await _interview(store, "iv-1")
    gen = await _generated(store, "가상 목록 조회 과제")
    await _generated(store, "가상 초안 과제", approve=False)  # 승인 전은 고를 수 없다
    tasks = (await client.get(f"/postings/{POSTING}/tasks")).json()
    assert [t["common_id"] for t in tasks] == [gen.id, iv.id]  # 최근 승인 순
    assert [t["interview"] for t in tasks] == [False, True]
    assert tasks[0]["posting_id"] == POSTING and tasks[0]["title"] == "가상 목록 조회 과제"


async def test_run_uses_the_chosen_problem_over_the_interview_task(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    client, store = api
    iv = await _interview(store, "iv-1")
    gen = await _generated(store, "가상 목록 조회 과제")
    # 고르지 않으면 지금처럼 공고의 인터뷰 과제
    plain = await _run(client)
    view = await wait_status(client, plain.json()["run_id"], "awaiting_approval")
    assert [t["task_id"] for t in view["spec"]["common_tasks"]] == [iv.id]
    # 고르면 그 문제
    resp = await _run(client, common_id=gen.id)
    assert resp.status_code == 202, resp.text
    view = await wait_status(client, resp.json()["run_id"], "awaiting_approval")
    assert [t["task_id"] for t in view["spec"]["common_tasks"]] == [gen.id]
    assert FLAG_CHOSEN_COMMON + gen.id in view["spec"]["review_flags"]
    approved = await client.post(
        f"/runs/{view['run_id']}/approve", json=reviewer_decision("approved")
    )
    assert approved.status_code == 202, approved.text
    await wait_status(client, view["run_id"], "done")
    # 고른 문제는 이미 승인된 과제 — 지원자 명세 승인이 다시 승인해 원래 승인 기록을 덮지 않는다
    row = next(r for r in store.rows if r["id"] == gen.id)
    assert (row["status"], row["approved_by"]) == ("approved", "task-reviewer-fake")
    assert (await client.get(f"/runs/{view['run_id']}")).json()["common_id"] == gen.id


async def test_problem_that_is_not_choosable_is_422(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    client, store = api
    draft = await _generated(store, "가상 초안 과제", approve=False)
    data_dir = client._transport.app.state.settings.data_dir  # type: ignore[attr-defined]
    for bad in ("ct-000000000000", draft.id):
        resp = await _run(client, common_id=bad)
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "common_task_not_choosable"
    assert not (data_dir / "raw").exists() or not any(
        (data_dir / "raw").iterdir()
    )  # 지원서를 남기지 않는다


async def test_chosen_problem_runs_even_if_the_interview_task_is_on_another_version(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    client, store = api
    await _interview(store, "iv-old", version="onto-v2026.10.01")
    assert (await _run(client)).status_code == 409  # 고르지 않으면 지금처럼 멈춘다
    gen = await _generated(store, "가상 목록 조회 과제")
    resp = await _run(client, common_id=gen.id)
    assert resp.status_code == 202, resp.text
    view = await wait_status(client, resp.json()["run_id"], "awaiting_approval")
    assert [t["task_id"] for t in view["spec"]["common_tasks"]] == [gen.id]


async def _chosen_pending(client: httpx.AsyncClient, common_id: str) -> dict:  # type: ignore[type-arg]
    resp = await _run(client, common_id=common_id)
    assert resp.status_code == 202, resp.text
    return await wait_status(client, resp.json()["run_id"], "awaiting_approval")


async def test_chosen_problem_replaced_or_gone_blocks_approval(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    """고른 문제가 실행 도중 교체(superseded)·삭제되면 승인을 막는다 [확정: 2026-11-20 사용자] — 인터뷰 과제의 '교체 전 과제를
    가리키는 명세는 승인하지 않는다'와 같게. 실행을 새로 만들어 다시 고른다."""
    client, store = api
    gen = await _generated(store, "가상 목록 조회 과제")
    view = await _chosen_pending(client, gen.id)
    newer = await store.save_draft(
        CommonKey(POSTING, ONTO, "draft_feature_task@11+가상#가상 목록 조회 과제"),
        _task("가상 목록 조회 과제 2판"),
        [],
    )
    await store.approve(newer.id, "task-reviewer-fake", datetime(2026, 11, 21, tzinfo=UTC))
    refused = await client.post(
        f"/runs/{view['run_id']}/approve", json=reviewer_decision("approved")
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "chosen_task_unavailable"

    other = await _generated(store, "가상 지울 과제")
    view = await _chosen_pending(client, other.id)
    store.rows = [r for r in store.rows if r["id"] != other.id]
    refused = await client.post(
        f"/runs/{view['run_id']}/approve", json=reviewer_decision("approved")
    )
    assert refused.status_code == 409


async def test_revising_the_common_task_of_a_chosen_problem_is_409(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    client, store = api
    gen = await _generated(store, "가상 목록 조회 과제")
    view = await _chosen_pending(client, gen.id)
    resp = await client.post(
        f"/runs/{view['run_id']}/approve",
        json={**reviewer_decision("revise", "공통 과제를 바꿔 주세요"), "revise_target": "common"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "chosen_task_locked"


async def test_posting_group_requires_a_choice_or_uses_the_only_linked_problem(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    """묶음에 승인된 문제가 둘 이상이면 고르게 하고(422), 이어진 공고에만 하나 있으면 그것 [확정: 2026-11-20 사용자]."""
    from types import SimpleNamespace

    from tests.api.conftest import FakeProvisional

    client, store = api
    linked = "posting-fake-002"
    fake = FakeProvisional({})
    fake.levels[POSTING] = "경력"
    fake.groups = {POSTING: [linked, POSTING], linked: [linked, POSTING]}
    client._transport.app.state.postings = SimpleNamespace(store=fake)  # type: ignore[attr-defined]
    key = CommonKey(linked, ONTO, "interview:iv-linked")
    row = await store.save_draft(key, _task("가상 이어진 인터뷰 과제"), [])
    await store.approve_interview(row.id, "recruiter-fake", datetime(2026, 11, 20, tzinfo=UTC))
    # 이어진 공고에만 하나 — 고르지 않아도 그것
    resp = await _run(client)
    assert resp.status_code == 202, resp.text
    view = await wait_status(client, resp.json()["run_id"], "awaiting_approval")
    assert view["common_id"] == row.id
    assert [t["task_id"] for t in view["spec"]["common_tasks"]] == [row.id]
    # 묶음에 둘 — 고르게 한다
    await _generated(store, "가상 목록 조회 과제")
    need = await _run(client)
    assert need.status_code == 422
    assert need.json()["error"]["code"] == "common_task_choice_required"
