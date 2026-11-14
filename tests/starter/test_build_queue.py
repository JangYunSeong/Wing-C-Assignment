"""빌드 큐 (#128) — 생성 요청·가져가기·lease·죽은 작업 복구. 메모리 구현, 가상 데이터만.

명세 승인과 과제 생성은 따로다 [확정: 2026-11-12 사용자] — 생성은 사람이 요청할 때만 큐에 들어간다.
같은 규칙을 Postgres 에서는 test_build_queue_pg.py 가 동시성까지 본다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from task_spec.schemas.build_record import StarterFile, next_stage_log
from task_spec.starter.store import (
    LEASE_EXPIRED,
    SUPERSEDED,
    BuildRuleError,
    CommonRef,
    InMemoryBuildStore,
)
from tests.starter.test_build_store import AT, H_OLD, _outcome

CID = "ct-0123456789ab"
CID2 = "ct-ba9876543210"
APPROVED = CommonRef("approved", "feature", "onto-test")


def _store(**commons: CommonRef) -> InMemoryBuildStore:
    s = InMemoryBuildStore()
    s.commons = {CID: APPROVED, CID2: APPROVED, **commons}
    return s


async def test_request_creates_one_active_build_per_common_task() -> None:
    """두 번 눌러도 진행 중 빌드는 하나 — 두 번째는 같은 빌드를 돌려준다."""
    s = _store()
    b, created = await s.request_build(CID, "가상승인자", AT)
    assert created and b.status == "pending" and b.requested_by == "가상승인자"
    assert b.requested_at == AT and b.ontology_version == "onto-test"
    again, created2 = await s.request_build(CID, "다른승인자", AT + timedelta(minutes=1))
    assert not created2 and again.build_id == b.build_id
    assert len(await s.builds(CID)) == 1


async def test_review_or_failed_build_does_not_block_a_new_request() -> None:
    """진행 중이 아닌 빌드(review·failed)가 있으면 새 빌드(-b2)를 만든다 — 실패는 사람이 다시 요청."""
    s = _store()
    b, _ = await s.request_build(CID, "가상승인자", AT)
    await s.fail(b.build_id, "가상 실패")
    b2, created = await s.request_build(CID, "가상승인자", AT)
    assert created and b2.seq == 2


@pytest.mark.parametrize(
    ("common", "msg"),
    [
        (CommonRef("draft", "feature", "onto-test"), "승인된 공통 과제만"),
        (CommonRef("superseded", "feature", "onto-test"), "승인된 공통 과제만"),
        (CommonRef("approved", "incident", "onto-test"), "기능 개발형"),
    ],
)
async def test_request_needs_an_approved_feature_task(common: CommonRef, msg: str) -> None:
    s = _store(**{CID: common})
    with pytest.raises(BuildRuleError, match=msg):
        await s.request_build(CID, "가상승인자", AT)
    with pytest.raises(KeyError):
        await s.request_build("ct-000000000000", "가상승인자", AT)


async def test_claim_takes_requests_in_order_and_holds_a_lease() -> None:
    s = _store()
    first, _ = await s.request_build(CID, "a", AT)
    second, _ = await s.request_build(CID2, "a", AT + timedelta(seconds=1))
    got = await s.claim("w1", 60, AT + timedelta(seconds=5))
    assert got is not None and got.build_id == first.build_id
    assert (got.status, got.worker, got.attempt) == ("generating", "w1", 1)
    assert got.lease_until == AT + timedelta(seconds=65) and got.started_at is not None
    nxt = await s.claim("w2", 60, AT)
    assert nxt is not None and nxt.build_id == second.build_id
    assert await s.claim("w3", 60, AT) is None  # 빈 큐


async def test_builds_made_without_a_request_are_not_claimed() -> None:
    """eval 적재(new_build)로 만든 pending 빌드는 출처가 eval 이라 builder 가 가져가지 않는다."""
    s = _store()
    b = await s.new_build(CID, "onto-test", AT)
    assert b.origin == "eval"
    assert await s.claim("w1", 60, AT) is None


async def test_eval_builds_in_progress_do_not_block_a_request() -> None:
    """#175: 진행 중 빌드 하나 보장이 DB 색인은 요청 빌드만, 앱 계층은 모든 빌드를 봐서, 진행 중인 eval 빌드가 있으면 생성 요청이
    그 빌드를 돌려받고 처리되지 않았다 (claim 은 eval 빌드를 가져가지 않으므로). 이제 둘 다 출처 칸 하나(origin = request)를 본다."""
    s = _store()
    ev = await s.new_build(CID, "onto-test", AT)
    await s.set_status(ev.build_id, "generating")
    b, created = await s.request_build(CID, "가상승인자", AT)
    assert created and b.build_id != ev.build_id and b.origin == "request"
    taken = await s.claim("w1", 60, AT)
    assert taken is not None and taken.build_id == b.build_id


async def test_claim_fails_a_request_whose_task_was_superseded() -> None:
    """대기 중에 공고의 새 판이 승인되면 그 요청은 빌드하지 않는다 — failed + superseded, 다음 요청으로."""
    s = _store()
    old, _ = await s.request_build(CID, "a", AT)
    nxt, _ = await s.request_build(CID2, "a", AT + timedelta(seconds=1))
    s.commons[CID] = CommonRef("superseded", "feature", "onto-test")
    got = await s.claim("w1", 60, AT)
    assert got is not None and got.build_id == nxt.build_id
    stale = await s.get(old.build_id)
    assert stale is not None and (stale.status, stale.error) == ("failed", SUPERSEDED)
    assert stale.finished_at is not None


async def test_heartbeat_extends_only_the_owners_lease() -> None:
    s = _store()
    await s.request_build(CID, "a", AT)
    b = await s.claim("w1", 60, AT)
    assert b is not None
    assert await s.heartbeat(b.build_id, "w1", 120, AT + timedelta(seconds=30))
    got = await s.get(b.build_id)
    assert got is not None and got.lease_until == AT + timedelta(seconds=150)
    assert not await s.heartbeat(b.build_id, "w2", 120, AT)  # 다른 worker
    await s.fail(b.build_id, "가상 실패")
    assert not await s.heartbeat(b.build_id, "w1", 120, AT)  # 끝난 빌드


async def test_stage_and_cost_add_up_while_running() -> None:
    s = _store()
    await s.request_build(CID, "a", AT)
    b = await s.claim("w1", 60, AT)
    assert b is not None
    await s.record_stage(b.build_id, "contract", 0.25)
    got = await s.record_stage(b.build_id, "build", 0.5)
    assert (got.stage, got.cost_usd) == ("build", 0.75)
    # 진행 기록 — 비용은 방금 끝난 단계(contract)에 쌓이고, 단계마다 한 줄 (화면 진행 막대)
    assert [(m.stage, m.cost_usd, m.attempt) for m in got.stage_log] == [
        ("contract", 0.75, 1),
        ("build", 0.0, 1),
    ]
    same = await s.record_stage(b.build_id, "build", 0.1)  # 실패 단계의 비용 — 같은 단계에 더함
    assert [m.stage for m in same.stage_log] == ["contract", "build"]
    assert same.stage_log[-1].cost_usd == pytest.approx(0.1)
    assert sum(m.cost_usd for m in same.stage_log) == pytest.approx(same.cost_usd)
    await s.set_status(b.build_id, "verifying")
    done = await s.set_status(b.build_id, "review")
    assert done.finished_at is not None and done.lease_until is None
    with pytest.raises(BuildRuleError, match="단계 기록"):
        await s.record_stage(b.build_id, "load")


async def test_fail_keeps_a_short_reason_only() -> None:
    s = _store()
    await s.request_build(CID, "a", AT)
    b = await s.claim("w1", 60, AT)
    assert b is not None
    got = await s.fail(b.build_id, "가상 사유\n" + "x" * 1000)
    assert got.status == "failed" and got.error is not None and len(got.error) == 300
    assert "\n" not in got.error and got.lease_until is None


async def test_daily_budget_stops_claiming() -> None:
    s = _store()
    await s.request_build(CID, "a", AT)
    b = await s.claim("w1", 60, AT, daily_budget_usd=1.0)
    assert b is not None
    await s.record_stage(b.build_id, "build", 1.5)
    await s.request_build(CID2, "a", AT)
    assert await s.claim("w2", 60, AT + timedelta(hours=1), daily_budget_usd=1.0) is None
    later = await s.claim("w2", 60, AT + timedelta(days=1, minutes=1), daily_budget_usd=1.0)
    assert later is not None  # 하루가 지나면 다시


async def test_expired_lease_is_retried_once_then_failed() -> None:
    """builder 가 사라지면(heartbeat 없음) 처음부터 다시 — 반쯤 쓴 파일·결과는 지운다. 시도를 다 쓰면 failed."""
    s = _store()
    await s.request_build(CID, "a", AT)
    b = await s.claim("w1", 60, AT)
    assert b is not None
    await s.put_files(b.build_id, [StarterFile.of(b.build_id, "starter", "a.py", "x = 1")])
    await s.save_outcome(b.build_id, _outcome(H_OLD))
    assert await s.recover_expired(AT + timedelta(seconds=30)) == []  # 아직 lease 안
    (back,) = await s.recover_expired(AT + timedelta(seconds=61))
    assert (back.status, back.error, back.outcome, back.worker) == (
        "pending",
        LEASE_EXPIRED,
        None,
        None,
    )
    assert await s.files(b.build_id) == []
    again = await s.claim("w2", 60, AT + timedelta(seconds=70))
    assert again is not None and again.attempt == 2 and again.error is None
    (dead,) = await s.recover_expired(AT + timedelta(seconds=200))
    assert (dead.status, dead.error) == ("failed", LEASE_EXPIRED)
    assert dead.finished_at is not None


def test_next_stage_log_starts_a_new_line_per_attempt() -> None:
    at = AT
    log = next_stage_log([], "contract", 0.0, at, 1)
    log = next_stage_log(log, "build", 0.02, at + timedelta(minutes=1), 1)
    # 죽은 작업 복구 뒤 다시 가져가면(시도 2) 같은 단계라도 새 줄
    log = next_stage_log(log, "contract", 0.0, at + timedelta(minutes=30), 2)
    assert [(m.stage, m.attempt, m.cost_usd) for m in log] == [
        ("contract", 1, 0.02),
        ("build", 1, 0.0),
        ("contract", 2, 0.0),
    ]
