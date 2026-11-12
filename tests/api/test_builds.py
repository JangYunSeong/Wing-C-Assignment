"""빌드·전달 묶음 조회 API — 메모리 저장소. 응답에 계약·숨은 계획·오버레이 본문이 없는지 본다. 가상 데이터만."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from task_spec.api.app import create_app
from task_spec.schemas import TargetCompetency, TaskItem, TechEnv
from task_spec.schemas.build_record import README_PATH, StarterFile
from task_spec.schemas.domain import (
    AcceptedDesign,
    Artifact,
    Clarification,
    DebugStage,
    HiddenAcceptance,
)
from task_spec.schemas.package import CaseResult, VerifySummary, flag_id
from task_spec.starter.store import InMemoryBuildStore, TaskLabel
from tests.api.conftest import make_settings
from tests.starter.test_build_store import AT, FLAG, H_NEW, H_OLD, _artifact, _outcome

CID = "ct-0123456789ab"
SECRET = "가상 참조 구현 본문 SECRET-REF"


async def _client(
    tmp_path: Path, store: InMemoryBuildStore | None, approval: Any = None
) -> httpx.AsyncClient:
    app = create_app(settings=make_settings(tmp_path))
    if store is not None:
        app.state.builds = store
    if approval is not None:  # 빌드 승인 그래프 (#130, 메모리 체크포인터)
        app.state.build_approval = approval
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _review_build(store: InMemoryBuildStore) -> str:
    b = await store.new_build(CID, "onto-test", AT)
    await store.set_status(b.build_id, "generating")
    await store.put_files(
        b.build_id,
        [
            StarterFile.of(b.build_id, "starter", "src/App.java", "class App {}"),
            StarterFile.of(b.build_id, "ref_accepted", "src/Svc.java", SECRET, variant="d1"),
        ],
    )
    await store.save_outcome(b.build_id, _outcome(H_OLD))
    await store.set_status(b.build_id, "verifying")
    await store.set_status(b.build_id, "review")
    for role in ("candidate", "grader"):
        await store.put_artifact(_artifact(b.build_id, role, H_OLD))  # type: ignore[arg-type]
    return b.build_id


async def test_build_views_hide_contract_plan_and_file_bodies(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _review_build(store)
    async with await _client(tmp_path, store) as c:
        builds = (await c.get("/builds", params={"common_id": CID})).json()
        assert [b["build_id"] for b in builds] == [bid]
        r = await c.get(f"/builds/{bid}")
        body = r.json()
        assert r.status_code == 200 and body["status"] == "review"
        assert body["files"] == {"starter": 1, "ref_accepted": 1}
        assert body["review_flags"] == [{"flag_id": flag_id(FLAG), "text": FLAG, "verdict": None}]
        assert body["verification"]["starter-only"]["hidden_total"] == 1
        assert not {"contract", "hidden_plan", "outcome"} & body.keys()
        arts = (await c.get(f"/builds/{bid}/artifacts")).json()
        assert [a["role"] for a in arts] == ["candidate", "grader"]
        assert (await c.get(f"/builds/{bid}/deliveries")).json() == []
        everything = r.text + str(arts) + str(builds)
        assert SECRET not in everything and "class App" not in everything


async def test_recent_builds_without_common_id_have_titles(tmp_path: Path) -> None:
    """화면 목록용 — common_id 없이 최근 빌드, 공통 과제 제목·공고 id·템플릿 (명세 본문은 없음)."""
    store = InMemoryBuildStore()
    bid = await _review_build(store)
    other = await store.new_build("ct-ba9876543210", "onto-test", AT)
    store.labels[CID] = TaskLabel(title="가상 과제 제목", posting_id="가상_공고#001")
    async with await _client(tmp_path, store) as c:
        rows = (await c.get("/builds")).json()
        assert {r["build_id"] for r in rows} == {bid, other.build_id}
        mine = next(r for r in rows if r["build_id"] == bid)
        assert (mine["title"], mine["posting_id"]) == ("가상 과제 제목", "가상_공고#001")
        assert mine["template_ref"] == "data/python-duckdb@v1"
        assert next(r for r in rows if r["build_id"] == other.build_id)["title"] is None
        assert len((await c.get("/builds", params={"limit": 1})).json()) == 1
        assert (await c.get("/builds", params={"limit": 0})).status_code == 422
        detail = (await c.get(f"/builds/{bid}")).json()
        assert (
            detail["title"] == "가상 과제 제목"
            and detail["template_ref"] == "data/python-duckdb@v1"
        )


async def test_missing_and_malformed_ids(tmp_path: Path) -> None:
    async with await _client(tmp_path, InMemoryBuildStore()) as c:
        r = await c.get(f"/builds/{CID}-b9")
        assert r.status_code == 404 and r.json()["error"]["code"] == "build_not_found"
        assert (await c.get(f"/builds/{CID}-b9/artifacts")).status_code == 404
        assert (await c.get("/builds/../etc")).status_code in (404, 422)
        assert (await c.get("/builds/ct-xyz-b1")).status_code == 422
        assert (await c.get("/builds", params={"common_id": "eval-x"})).status_code == 422


async def test_delivery_targets_register_once_per_role(tmp_path: Path) -> None:
    async with await _client(tmp_path, InMemoryBuildStore()) as c:
        r = await c.post("/delivery-targets", json={"name": "guild-sftp", "role": "candidate"})
        assert r.status_code == 201 and r.json()["role"] == "candidate"
        again = await c.post("/delivery-targets", json={"name": "guild-sftp", "role": "candidate"})
        assert again.status_code == 201  # 같은 역할로 다시 등록은 그대로
        clash = await c.post("/delivery-targets", json={"name": "guild-sftp", "role": "grader"})
        assert clash.status_code == 409 and clash.json()["error"]["code"] == "build_rule_violation"
        bad = await c.post("/delivery-targets", json={"name": "sftp://u:p@host", "role": "grader"})
        assert bad.status_code == 422
        assert [t["name"] for t in (await c.get("/delivery-targets")).json()] == ["guild-sftp"]


async def test_no_store_is_503(tmp_path: Path) -> None:
    async with await _client(tmp_path, None) as c:
        r = await c.get("/delivery-targets")
        assert r.status_code == 503 and r.json()["error"]["code"] == "service_unavailable"


README_FLAG = f"{README_PATH} 에 운영 조건을 드러내는 말: 가상어 — 승인된 명세 문장에서 나옴"


async def test_readme(tmp_path: Path) -> None:
    async with await _client(tmp_path, None) as c:
        assert (await c.get(f"/builds/{CID}-b1/readme")).status_code == 503  # 저장소 미연결
    store = InMemoryBuildStore()
    bid = (await store.new_build(CID, "onto-test", AT)).build_id
    await store.set_status(bid, "generating")
    readme = StarterFile.of(bid, "readme", README_PATH, "# 가상 과제\n")
    await store.put_files(
        bid, [readme, StarterFile.of(bid, "ref_accepted", "S.java", SECRET, "d1")]
    )
    async with await _client(tmp_path, store) as c:
        assert (await c.get(f"/builds/{CID}-b9/readme")).status_code == 404
        r = await c.get(f"/builds/{bid}/readme")
        assert (
            r.status_code == 404 and r.json()["error"]["code"] == "readme_not_available"
        )  # 검증 전

        await store.set_status(bid, "verifying")
        flags = ["가상 다른 검토 표시", README_FLAG]
        await store.save_outcome(bid, _outcome(H_NEW).model_copy(update={"review_flags": flags}))
        await store.set_status(bid, "review")
        r = await c.get(f"/builds/{bid}/readme")
        body = r.json()
        assert r.status_code == 200 and body["content"] == "# 가상 과제\n"
        assert body["sha256"] == readme.sha256
        assert [f["text"] for f in body["review_flags"]] == [README_FLAG]  # README 표시만
        assert SECRET not in r.text  # 다른 파일 본문은 내주지 않는다


async def test_readme_missing(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _review_build(store)
    async with await _client(tmp_path, store) as c:
        r = await c.get(f"/builds/{bid}/readme")
        assert r.status_code == 404 and "저장한 README 없음" in r.json()["error"]["message"]


def _facilitator_task() -> TaskItem:
    """가상 명세 — 숨은 칸마다 표식 글(HIDDEN-…)을 넣어 응답에 없는지 본다."""
    return TaskItem(
        task_id=CID,
        track="common",
        task_kind="feature",
        target_competencies=[TargetCompetency(competency_id="comp:a", label="가상 역량")],
        tech_env=TechEnv(basis="공고 언어"),
        difficulty_target="medium",
        time_limit_min=60,
        title="가상 목록 화면",
        artifacts=[
            Artifact(artifact_id="a1", title="가상 지표", content="분당 10건", role="key"),
            Artifact(artifact_id="a2", title="가상 문서", content="미끼 예제", role="distractor"),
        ],
        max_requests=1,
        clarifications=[
            Clarification(
                clarification_id="c1",
                topic="가상 주제",
                answer="구체 답",
                answer_hint="모호한 답",
                match_terms=["생성 빈도"],
                reveals=["t1"],
            )
        ],
        max_clarifications=2,
        acceptance_hidden=[
            HiddenAcceptance(ac_id="h1", trap_id="t1", text="HIDDEN-REQ 숨은 요구 원문")
        ],
        debug_stage=DebugStage(
            time_limit_min=20,
            scenario="가상 증상",
            trap_ids=["t1"],
            artifacts=[Artifact(artifact_id="a3", title="가상 로그", content="로그", role="key")],
            max_requests=1,
            deliverables=["원인 메모"],
        ),
    )


async def test_facilitator_view_has_only_what_candidates_can_ask_for(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    bid = await _review_build(store)
    async with await _client(tmp_path, store) as c:
        assert (await c.get(f"/builds/{bid}/facilitator")).json()["error"]["code"] == (
            "facilitator_not_available"
        )  # 명세 없음
        store.tasks[CID] = _facilitator_task()
        r = await c.get(f"/builds/{bid}/facilitator")
    assert r.status_code == 200
    body = r.json()
    s1 = body["stage1"]
    assert [m["title"] for m in s1["materials"]["items"]] == ["가상 지표", "가상 문서"]
    assert s1["materials"]["max_requests"] == 1 and s1["clarifications"]["max_questions"] == 2
    ans = s1["clarifications"]["items"][0]
    assert (ans["answer"], ans["answer_hint"], ans["match_terms"]) == (
        "구체 답",
        "모호한 답",
        ["생성 빈도"],
    )
    assert body["stage2"]["scenario"] == "가상 증상"
    assert body["stage2"]["materials"]["items"][0]["content"] == "로그"
    text = r.text
    for hidden in ('"role"', "distractor", '"key"', "reveals", "trap", "HIDDEN-REQ", "observables"):
        assert hidden not in text, hidden


async def test_facilitator_view_waits_for_verified_build(tmp_path: Path) -> None:
    store = InMemoryBuildStore()
    b = await store.new_build(CID, "onto-test", AT)
    store.tasks[CID] = _facilitator_task()
    async with await _client(tmp_path, store) as c:
        r = await c.get(f"/builds/{b.build_id}/facilitator")
    assert r.status_code == 404 and "상태 pending" in r.json()["error"]["message"]


async def test_cases_view_for_approvers_has_case_text_and_results(tmp_path: Path) -> None:
    """승인자 보기 — 숨은 케이스 상황·통과 조건·의도한 사유, 공개 테스트, 허용 설계, 대상별 케이스 결과. 테스트·참조 구현 본문은 없다."""
    store = InMemoryBuildStore()
    b = await store.new_build(CID, "onto-test", AT)
    await store.set_status(b.build_id, "generating")
    await store.put_files(
        b.build_id,
        [StarterFile.of(b.build_id, "ref_accepted", "src/Svc.java", SECRET, variant="d1")],
    )
    out = _outcome(H_OLD)
    hc = out.hidden_plan.cases[0].case_id
    out = out.model_copy(
        update={
            "verification": {
                "naive-all": VerifySummary(
                    hidden_passed=0,
                    hidden_total=1,
                    public_passed=1,
                    public_total=1,
                    cases=[
                        CaseResult(
                            id=hc, kind="hidden", passed=False, reason="gap", message="기대 0"
                        ),
                    ],
                )
            }
        }
    )
    await store.save_outcome(b.build_id, out)
    async with await _client(tmp_path, store) as c:
        assert (await c.get(f"/builds/{b.build_id}/cases")).status_code == 404  # 검증 전
        await store.set_status(b.build_id, "verifying")
        await store.set_status(b.build_id, "review")
        store.tasks[CID] = _facilitator_task().model_copy(
            update={
                "accepted_designs": [
                    AcceptedDesign(
                        design_id="d1", summary="가상 설계", conditions="가상 조건", trap_ids=["t1"]
                    ),
                    AcceptedDesign(
                        design_id="d2", summary="다른 설계", conditions="조건", trap_ids=["t1"]
                    ),
                ]
            }
        )
        r = await c.get(f"/builds/{b.build_id}/cases")
    assert r.status_code == 200
    body = r.json()
    hidden = [x for x in body["cases"] if x["kind"] == "hidden"]
    assert hidden and hidden[0]["id"] == hc and hidden[0]["expect"] and hidden[0]["intended_reason"]
    assert [x["id"] for x in body["cases"] if x["kind"] == "public"] == [
        t.test_id for t in out.contract.public_tests
    ]
    assert body["results"]["naive-all"][0] == {
        "id": hc,
        "kind": "hidden",
        "passed": False,
        "reason": "gap",
        "message": "기대 0",
    }
    assert [d["design_id"] for d in body["accepted_designs"]] == ["d1", "d2"]
    assert body["acceptance_hidden"][0]["text"].startswith("HIDDEN-REQ")
    assert SECRET not in r.text  # 참조 구현 본문은 없다


async def test_build_request_queues_once_and_needs_an_approved_feature_task(
    tmp_path: Path,
) -> None:
    """과제 생성 요청 (#128) — 명세 승인과 따로, 사람이 요청할 때만 큐에 넣는다. 두 번 눌러도 진행 중 빌드는 하나."""
    from task_spec.starter.store import CommonRef

    store = InMemoryBuildStore()
    store.commons = {
        CID: CommonRef("approved", "feature", "onto-test"),
        "ct-ba9876543210": CommonRef("draft", "feature", "onto-test"),
        "ct-aaaaaaaaaaaa": CommonRef("approved", "incident", "onto-test"),
    }
    store.labels[CID] = TaskLabel(title="가상 과제 제목", posting_id="가상_공고#001")
    async with await _client(tmp_path, store) as c:
        r = await c.post(f"/common-tasks/{CID}/builds", json={"requested_by": "가상승인자"})
        assert r.status_code == 202
        body = r.json()
        assert body["created"] is True and body["build"]["status"] == "pending"
        assert (
            body["build"]["requested_by"] == "가상승인자"
            and body["build"]["title"] == "가상 과제 제목"
        )
        again = await c.post(f"/common-tasks/{CID}/builds", json={"requested_by": "다른승인자"})
        assert again.status_code == 200 and again.json()["created"] is False
        assert again.json()["build"]["build_id"] == body["build"]["build_id"]
        detail = (await c.get(f"/builds/{body['build']['build_id']}")).json()
        assert detail["requested_by"] == "가상승인자" and detail["attempt"] == 0
        for cid, code in (
            ("ct-ba9876543210", 409),  # 초안
            ("ct-aaaaaaaaaaaa", 409),  # 이슈형
            ("ct-000000000000", 404),  # 없음
        ):
            r = await c.post(f"/common-tasks/{cid}/builds", json={"requested_by": "가상승인자"})
            assert r.status_code == code, cid
        assert (await c.post(f"/common-tasks/{CID}/builds", json={})).status_code == 422
        assert (
            await c.post("/common-tasks/not-an-id/builds", json={"requested_by": "a"})
        ).status_code == 422


async def test_build_approval_flow_through_the_api(tmp_path: Path) -> None:
    """빌드 승인 (#130) — GET 이 승인 thread 를 시작(interrupt 까지), POST 는 resume 만. 판단이 빠지면 422 로 다시 기다린다."""
    from datetime import UTC, datetime

    from task_spec.graph import make_checkpointer
    from task_spec.graph.build_approval import build_approval_graph
    from task_spec.schemas.package import ReviewDecision

    store = InMemoryBuildStore()
    bid = await _review_build(store)
    await store.reopen(bid)  # README 를 저장한 빌드로 다시 (승인에 필요)
    await store.put_readme(StarterFile.of(bid, "readme", README_PATH, "# 가상 README"))
    await store.set_status(bid, "verifying")
    await store.set_status(bid, "review")
    for role in ("candidate", "grader"):
        await store.put_artifact(_artifact(bid, role, H_OLD))  # type: ignore[arg-type]
    pending = await store.new_build("ct-ba9876543210", "onto-test", AT)
    decision = ReviewDecision(
        flag_id=flag_id(FLAG), flag=FLAG, verdict="fixed", reviewer="가상승인자"
    )
    body = {
        "status": "approved",
        "reviewer": "가상승인자",
        "at": datetime(2026, 11, 12, tzinfo=UTC).isoformat(),
        "decisions": [decision.model_dump(mode="json")],
    }
    async with make_checkpointer(kind="memory") as cp:
        async with await _client(tmp_path, store, build_approval_graph(store, cp)) as c:
            view = (await c.get(f"/builds/{bid}/approval")).json()
            assert view["state"] == "awaiting" and view["flags"][0]["flag_id"] == flag_id(FLAG)
            bad = await c.post(f"/builds/{bid}/approve", json={**body, "decisions": []})
            assert (
                bad.status_code == 422 and bad.json()["error"]["code"] == "build_approval_invalid"
            )
            assert "빠진 표시 1건" in bad.json()["error"]["message"]
            assert (
                await c.post(f"/builds/{bid}/approve", json={"status": "approved"})
            ).status_code == 422
            ok = await c.post(f"/builds/{bid}/approve", json=body)
            assert ok.status_code == 200 and ok.json()["state"] == "approved"
            again = (await c.get(f"/builds/{bid}/approval")).json()
            assert again["status"] == "approved" and again["approved_by"] == "가상승인자"
            assert again["flags"][0]["verdict"] == "fixed"
            assert (
                await c.post(f"/builds/{pending.build_id}/approve", json=body)
            ).status_code == 409


async def test_build_reject_through_the_api(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from task_spec.graph import make_checkpointer
    from task_spec.graph.build_approval import build_approval_graph

    store = InMemoryBuildStore()
    bid = await _review_build(store)
    body = {
        "status": "rejected",
        "reviewer": "가상승인자",
        "at": datetime(2026, 11, 12, tzinfo=UTC).isoformat(),
        "comment": "가상 반려 사유",
    }
    async with make_checkpointer(kind="memory") as cp:
        async with await _client(tmp_path, store, build_approval_graph(store, cp)) as c:
            r = await c.post(
                f"/builds/{bid}/approve", json=body
            )  # GET 없이도 thread 를 시작하고 resume
            assert r.status_code == 200 and r.json()["state"] == "rejected"
            assert r.json()["error"] == "승인자 반려 (가상승인자): 가상 반려 사유"
    b = await store.get(bid)
    assert b is not None and b.status == "failed"
