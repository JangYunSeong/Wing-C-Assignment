"""API 시나리오 (httpx + ASGITransport, memory 체크포인터, 가상 데이터)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from langgraph.checkpoint.base import empty_checkpoint

from tests.api.conftest import (
    FAKE_FILE,
    FAKE_OPENAI_KEY,
    FAKE_OPENCODE_KEY,
    AppFactory,
    FakeOntology,
    FakeProvisional,
    reviewer_decision,
    wait_status,
)
from tests.conftest import failing_pseudonymizer


async def _create(client: httpx.AsyncClient, **extra: object) -> str:
    resp = await client.post(
        "/runs",
        data={"posting_id": "posting-fake-001"},
        files=[("resumes", FAKE_FILE), ("cover_letters", ("cl.txt", b"fake", "text/plain"))],
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "running"
    # USE_COVER_LETTERS=false (기본): 자기소개서는 받되 저장·처리하지 않고 알린다 [확정: 2026-11-03]
    assert body["ignored"] == ["cover_letter#1"]
    return str(body["run_id"])


async def test_create_approve_done_and_spec(client: httpx.AsyncClient, tmp_path: Path) -> None:
    run_id = await _create(client)
    saved = sorted(p.name for p in (tmp_path / "raw" / run_id).iterdir())
    assert saved == ["resume-1.txt"]  # 자기소개서는 저장하지 않음

    awaiting = await wait_status(client, run_id, "awaiting_approval")
    assert awaiting["spec"]["revision"] == 1
    assert awaiting["spec"]["ontology_version"] == "onto-v2026.11.02"
    assert awaiting["current_node"] == "approval"
    assert awaiting["approval_summary"]["total_time_min"] >= 0  # 승인 요약이 함께 온다
    assert (await client.get(f"/runs/{run_id}/spec")).status_code == 404  # 초안은 확정본 아님

    resp = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 202
    done = await wait_status(client, run_id, "done")
    assert done["spec"] is None
    # 확정 뒤에도 승인 요약(대조·배정)을 다시 계산해 준다 — 화면이 확정된 실행을 열 때
    assert done["approval_summary"] is not None
    assert (
        done["approval_summary"]["total_time_min"] == awaiting["approval_summary"]["total_time_min"]
    )

    spec = await client.get(f"/runs/{run_id}/spec")
    assert spec.status_code == 200
    assert spec.json()["approval"]["status"] == "approved"
    assert spec.json()["approval"]["reviewer"] == "reviewer-fake"


async def test_revise_then_rejected(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")

    resp = await client.post(
        f"/runs/{run_id}/approve", json=reviewer_decision("revise", "범위를 좁혀 주세요")
    )
    assert resp.status_code == 202
    # 재개 직후 스냅샷이 아직 옛 interrupt 일 수 있으므로 revision 으로 구분한다.
    async with asyncio.timeout(5):
        while True:
            body = await wait_status(client, run_id, "awaiting_approval")
            if body["spec"]["revision"] == 2:
                break
            await asyncio.sleep(0.02)

    await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("rejected"))
    rejected = await wait_status(client, run_id, "rejected")
    assert rejected["revision"] == 2
    assert (await client.get(f"/runs/{run_id}/spec")).status_code == 404


async def test_run_without_documents_still_needs_approval(client: httpx.AsyncClient) -> None:
    resp = await client.post("/runs", data={"posting_id": "posting-fake-001"})
    assert resp.status_code == 202
    body = await wait_status(client, resp.json()["run_id"], "awaiting_approval")
    assert body["spec"]["revision"] == 1


async def test_approve_when_not_pending_is_409(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")
    await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    await wait_status(client, run_id, "done")

    resp = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "approval_not_pending"
    assert resp.json()["error"]["run_id"] == run_id


class _GoneCommonStore:
    """명세가 가리키는 공통 과제 판이 모두 지워진 저장소 (가짜)."""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def missing(self, common_ids: list[str]) -> list[str]:
        self.asked.append(list(common_ids))
        return ["ct-gone"]


async def test_approve_when_common_task_gone_is_409_and_run_stays_pending(
    client: httpx.AsyncClient,
) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")
    store = _GoneCommonStore()
    client._transport.app.state.common_store = store  # type: ignore[attr-defined]

    resp = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "common_task_gone"
    assert "ct-gone" in resp.json()["error"]["message"]
    assert store.asked  # 명세의 공통 과제 참조로 물었다
    # 실행은 그대로 승인 대기 — 확정(finalize)을 돌리지 않았다
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "awaiting_approval"
    # 반려는 공통 과제를 승인하지 않으므로 막지 않는다
    resp = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("rejected"))
    assert resp.status_code == 202


async def test_unknown_run_is_404(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("get", "/runs/nope"),
        ("get", "/runs/nope/spec"),
        ("get", "/runs/nope/events"),
    ]:
        resp = await getattr(client, method)(path)
        assert resp.status_code == 404, path
        assert resp.json()["error"]["code"] == "run_not_found"
    resp = await client.post("/runs/nope/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 404


async def test_invalid_decision_is_422_and_does_not_resume(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")
    bad = [
        {**reviewer_decision("revise"), "comment": None},  # revise 는 comment 필수
        {**reviewer_decision("approved"), "reviewer": ""},
        {**reviewer_decision("maybe")},
    ]
    for body in bad:
        resp = await client.post(f"/runs/{run_id}/approve", json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "awaiting_approval"


async def test_reassign_with_common_only_revise_is_recorded_not_dropped(
    client: httpx.AsyncClient,
) -> None:
    """공통 과제만 다시 만드는 수정 요청에 슬롯 배정 바꾸기를 함께 보내면 202 그대로 — 배정은 바꾸지 않고
    reassign_ignored 로 남긴다 (#249, 예전에는 기록 없이 버림). 결정 검증기로 막지 않는 이유는 tests/schemas/test_approval."""
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")
    body = {
        **reviewer_decision("revise", "가상 사유"),
        "revise_target": "common",
        "reassign": {"d1-c1": "s1"},
    }
    assert (await client.post(f"/runs/{run_id}/approve", json=body)).status_code == 202
    async with asyncio.timeout(5):
        while True:
            view = await wait_status(client, run_id, "awaiting_approval")
            if view["spec"]["revision"] == 2:
                break
            await asyncio.sleep(0.02)
    ignored = [e for e in view["errors"] if e["code"] == "reassign_ignored"]
    assert [(e["node"], e["ref_id"]) for e in ignored] == [("draft_spec", None)]  # 모르는 주장 id


async def test_disallowed_extension_rejected(client: httpx.AsyncClient, tmp_path: Path) -> None:
    resp = await client.post(
        "/runs",
        data={"posting_id": "p"},
        files=[("resumes", ("evil.exe", b"fake", "application/octet-stream"))],
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_file_type"
    assert not (tmp_path / "raw").exists()


async def test_oversize_file_rejected(client: httpx.AsyncClient, tmp_path: Path) -> None:
    resp = await client.post(
        "/runs",
        data={"posting_id": "p"},
        files=[("resumes", ("big.txt", b"x" * 2048, "text/plain"))],  # 상한 1024
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"
    assert not (tmp_path / "raw").exists()


async def test_missing_posting_id_is_422(client: httpx.AsyncClient) -> None:
    resp = await client.post("/runs", files=[("resumes", FAKE_FILE)])
    assert resp.status_code == 422


async def test_residual_pii_run_is_failed(make_client: AppFactory) -> None:
    client = await make_client(pseudonymizer=failing_pseudonymizer)
    run_id = await _create(client)
    body = await wait_status(client, run_id, "failed")
    assert [e["code"] for e in body["errors"]] == ["residual_pii"]  # 이력서 1건만 처리
    assert body["spec"] is None
    assert (await client.get(f"/runs/{run_id}/spec")).status_code == 404
    resp = await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    assert resp.status_code == 409


async def test_healthz_exposes_no_key(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ontology_version"] == "onto-v2026.11.02"
    assert body["llm_keys"] == {"openai": True, "opencode": True}
    assert FAKE_OPENAI_KEY not in resp.text
    assert FAKE_OPENCODE_KEY not in resp.text


async def test_responses_do_not_echo_upload_content(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    body = await wait_status(client, run_id, "awaiting_approval")
    assert "가상 데이터" not in json.dumps(body, ensure_ascii=False)


async def test_sse_streams_until_terminal(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")

    async def consume() -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        async with client.stream("GET", f"/runs/{run_id}/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line.removeprefix("data:").strip()))
        return events

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    await client.post(f"/runs/{run_id}/approve", json=reviewer_decision("approved"))
    async with asyncio.timeout(5):
        events = await task
    statuses = [e["status"] for e in events]
    assert statuses[0] == "awaiting_approval"
    assert statuses[-1] == "done"


async def test_promote_overlap_via_approve(client: httpx.AsyncClient) -> None:
    run_id = await _create(client)
    await wait_status(client, run_id, "awaiting_approval")
    bad = [
        {**reviewer_decision("approved"), "promote_overlap": ["comp:fake-1"]},  # revise 만
        {
            **reviewer_decision("revise", "가상 사유"),
            "revise_target": "common",  # 경험 검증을 다시 만들어야 한다
            "promote_overlap": ["comp:fake-1"],
        },
        {
            **reviewer_decision("revise", "가상 사유"),
            "promote_overlap": ["comp:fake-1"],
            "restore_overlap": ["comp:fake-1"],  # 같은 id 를 두 목록에
        },
        {**reviewer_decision("revise", "가상 사유"), "promote_overlap": ["x" * 121]},
    ]
    for body in bad:
        resp = await client.post(f"/runs/{run_id}/approve", json=body)
        assert resp.status_code == 422, body
        assert resp.json()["error"]["code"] == "validation_error"
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "awaiting_approval"

    ok = {
        **reviewer_decision("revise", "가상 사유"),
        "revise_target": "verification",
        "promote_overlap": ["comp:fake-1"],
    }
    resp = await client.post(f"/runs/{run_id}/approve", json=ok)
    assert resp.status_code == 202, resp.text
    async with asyncio.timeout(5):
        while True:
            body = await wait_status(client, run_id, "awaiting_approval")
            if body["spec"]["revision"] == 2:
                break
            await asyncio.sleep(0.02)


async def test_list_runs_newest_first_with_status_filter(client: httpx.AsyncClient) -> None:
    """실행 목록 (2026-11-18 — 관리 화면을 Wing admin-fe 로 옮기며 '최근 실행'을 브라우저 저장소 대신 서버에서).
    상태·공고·시각만 — 명세 초안·승인 요약은 GET /runs/{id}. 같은 체크포인터의 빌드 승인 thread 는 빠진다."""
    first = await _create(client)
    await wait_status(client, first, "awaiting_approval")
    await client.post(f"/runs/{first}/approve", json=reviewer_decision("rejected"))
    await wait_status(client, first, "rejected")
    second = await _create(client)
    await wait_status(client, second, "awaiting_approval")
    saver = client._transport.app.state.graph.checkpointer  # type: ignore[attr-defined]
    await saver.aput(
        {"configurable": {"thread_id": "build-ct-0123456789ab-b1-가상", "checkpoint_ns": ""}},
        empty_checkpoint(),
        {"source": "input", "step": -1, "parents": {}},
        {},
    )

    rows = (await client.get("/runs")).json()
    assert [(r["run_id"], r["status"]) for r in rows] == [
        (second, "awaiting_approval"),
        (first, "rejected"),
    ]
    assert set(rows[0]) == {
        "run_id",
        "status",
        "current_node",
        "revision",
        "posting_id",
        "company_id",
        "company",
        "company_known",
        "career_level",
        "career_level_source",
        "started_at",
        "updated_at",
    }
    assert rows[0]["posting_id"] == "posting-fake-001" and rows[0]["revision"] == 1
    assert rows[0]["current_node"] == "approval"
    assert rows[0]["company_id"] is None  # 온톨로지·공고 저장소가 없으면 회사를 모른다
    assert rows[0]["career_level"] is None  # 신입·경력도 모른다
    assert rows[1]["started_at"] < rows[0]["started_at"] <= rows[0]["updated_at"]

    pending = (await client.get("/runs", params={"status": "awaiting_approval"})).json()
    assert [r["run_id"] for r in pending] == [second]
    both = await client.get("/runs", params=[("status", "rejected"), ("status", "running")])
    assert [r["run_id"] for r in both.json()] == [first]
    assert [r["run_id"] for r in (await client.get("/runs?limit=1")).json()] == [second]
    assert (await client.get("/runs", params={"status": "가상"})).status_code == 422
    assert (await client.get("/runs", params={"limit": 0})).status_code == 422


async def test_list_runs_by_company_including_companies_not_in_ontology(
    client: httpx.AsyncClient,
) -> None:
    """회사로 묶고 거르기 — 회사 id 는 표기에서 공백·법인 표기를 뺀 값. 임시 공고의 표기가 온톨로지 회사와 같으면 같은 회사,
    온톨로지에 없는 새 회사도 id 가 생긴다 (company_known=false). limit 이 없으면 회사의 실행을 전부."""
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.postings = SimpleNamespace(
        store=FakeProvisional({"prov-fake0001": "(주) 가상 회사", "prov-fake0002": "New  Corp"})
    )
    ids = {}
    for pid in ("posting-fake-001", "prov-fake0001", "prov-fake0002", "posting-fake-003"):
        resp = await client.post("/runs", data={"posting_id": pid}, files=[("resumes", FAKE_FILE)])
        assert resp.status_code == 202, resp.text
        ids[pid] = resp.json()["run_id"]
        await wait_status(client, ids[pid], "awaiting_approval")
    app.state.ontology = FakeOntology({"posting-fake-001": "가상회사"}, ["가상회사", "다른회사"])

    rows = {r["posting_id"]: r for r in (await client.get("/runs")).json()}
    assert len(rows) == 4
    pick = ("company_id", "company", "company_known")
    assert [rows[p][k] for p in ("posting-fake-001", "prov-fake0001") for k in pick] == [
        "가상회사",
        "가상회사",
        True,
    ] * 2  # 임시 공고도 온톨로지 표기로
    assert [rows["prov-fake0002"][k] for k in pick] == ["newcorp", "New  Corp", False]
    assert [rows["posting-fake-003"][k] for k in pick] == [None, None, None]  # 회사를 모르는 공고

    async def by(company: str) -> list[str]:
        return [
            r["run_id"] for r in (await client.get("/runs", params={"company": company})).json()
        ]

    same = [ids["prov-fake0001"], ids["posting-fake-001"]]  # 최근 것부터
    assert await by("가상회사") == same
    assert await by("㈜가상 회사") == same  # 표기로 줘도 같은 규칙으로 맞춘다
    assert await by("newcorp") == [ids["prov-fake0002"]]
    assert await by("다른회사") == []


async def test_list_runs_shows_posting_career_level(client: httpx.AsyncClient) -> None:
    """신입·경력 (postings/career.py) — 사람이 정한 값(confirmed)이 먼저, 없으면 공고 문구(직무명·자격요건) 추정(posting_text),
    둘 다 없으면 null. 회사로 걸러도 같은 값."""
    app = client._transport.app  # type: ignore[attr-defined]
    store = FakeProvisional(
        {"prov-fake0001": "가상회사", "prov-fake0002": "가상회사"},
        rows={"prov-fake0001": {"title": "가상 백엔드 (경력)", "lines": []}},
    )
    store.levels["prov-fake0002"] = "신입"
    app.state.postings = SimpleNamespace(store=store)
    ids = {}
    for pid in ("posting-fake-001", "prov-fake0001", "prov-fake0002", "posting-fake-003"):
        resp = await client.post("/runs", data={"posting_id": pid}, files=[("resumes", FAKE_FILE)])
        assert resp.status_code == 202, resp.text
        ids[pid] = resp.json()["run_id"]
        await wait_status(client, ids[pid], "awaiting_approval")
    app.state.ontology = FakeOntology(
        {"posting-fake-001": "가상회사"},
        ["가상회사"],
        careers={"posting-fake-001": ["가상회사 / 가상 직무", "신입 지원 가능"]},
    )
    rows = {r["posting_id"]: r for r in (await client.get("/runs")).json()}
    pick = ("career_level", "career_level_source")
    assert [rows["posting-fake-001"][k] for k in pick] == ["신입", "posting_text"]
    assert [rows["prov-fake0001"][k] for k in pick] == ["경력", "posting_text"]
    assert [rows["prov-fake0002"][k] for k in pick] == ["신입", "confirmed"]
    assert [rows["posting-fake-003"][k] for k in pick] == [None, None]
    picked = (await client.get("/runs", params={"company": "가상회사"})).json()
    assert {r["posting_id"]: r["career_level"] for r in picked} == {
        "posting-fake-001": "신입",
        "prov-fake0001": "경력",
        "prov-fake0002": "신입",
    }
