"""그래프 안에서 ④ 공고 역량 확인 → 대조 → 승인 요약 → 겹침 낮추기(revise). FakeBackend, 가상 데이터만."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

from task_spec.graph import build_graph, builder, make_checkpointer
from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.prompting import PromptRegistry
from task_spec.retrieve.t1 import T1Retriever
from task_spec.schemas import PostingCompetency, PostingProfile, RunState
from tests.conftest import config_for, decision, fake_doc, run_input
from tests.graph.test_extract_claims import _writer
from tests.retrieve.fixtures import cardset

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


def _profile_node(*_: Any, **__: Any):
    async def posting_profile(state: RunState) -> dict[str, Any]:
        return {
            "posting_profile": PostingProfile(
                posting_id=state.posting_id,
                ontology_version=state.ontology_version,
                sub="data.eng",
                competencies=[
                    PostingCompetency(
                        competency_id="comp:data.pipeline", label="데이터 파이프라인", primary=True
                    ),
                    PostingCompetency(
                        competency_id="comp:sw.cache", label="캐시 설계", primary=True
                    ),
                    PostingCompetency(
                        competency_id="comp:data.stat_ml", label="통계 모델", primary=False
                    ),
                ],
            )
        }

    return posting_profile


def _json(body: dict[str, Any]) -> Completion:
    return Completion(json.dumps(body, ensure_ascii=False))


CLAIMS = {
    "claims": [
        {"action": "데이터 파이프라인 설계", "quote": "[ORG_1] 에서 데이터 파이프라인을 설계했다."},
        {"action": "처리 시간 단축", "result": "30%", "quote": "처리 시간 30% 단축."},
    ]
}
PICKS = {"items": [{"ref": "c1", "competency_ids": ["comp:data.pipeline"], "reason": "가상"}]}
REASON = "SENTINEL-REASON-가상이유"  # 판정 이유 — State·체크포인트·API 어디에도 남으면 안 된다
CHECK = {
    "items": [
        {
            "competency_id": "comp:data.pipeline",
            "evidence_claims": ["c1"],
            "reason": REASON,
            "label": "action",
        },
        {
            "competency_id": "comp:sw.cache",
            "evidence_claims": ["c2"],
            "reason": REASON,
            "label": "action",
        },
        {
            "competency_id": "comp:data.stat_ml",
            "evidence_claims": ["c2"],
            "reason": REASON,
            "label": "related",
        },
    ]
}


def _has(obj: Any, needle: str) -> bool:
    """체크포인트 저장소 안의 모든 bytes·str 을 훑는다 (msgpack 문자열은 utf-8 그대로)."""
    if isinstance(obj, bytes | bytearray):
        return needle.encode() in obj
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, dict):
        return any(_has(k, needle) or _has(v, needle) for k, v in obj.items())
    if isinstance(obj, list | tuple | set):
        return any(_has(x, needle) for x in obj)
    return False


@pytest.fixture
def patched_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder, "make_posting_profile", _profile_node)


async def test_check_flows_into_summary_and_downgrade(tmp_path: Path, patched_profile) -> None:
    fake = FakeBackend().push(_json(CLAIMS), _json(PICKS), _json(CHECK))
    cards, m = cardset()
    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(
            cp,
            pseudonymizer=_writer(tmp_path),
            llm=LLMClient({"opencode": fake}),
            prompts=PROMPTS,
            retriever=T1Retriever(cards, m),
        )
        inp = run_input(docs=[fake_doc("d1", "resume")])
        cfg = config_for(inp)
        result = await graph.ainvoke(inp, cfg)

        tasks = [p.prompt.split("@")[0] for p in result["provenance"]]
        assert tasks == ["extract_claims", "select_competency", "verify_overlap"]
        assert result["overlap_check"].status == "ok"
        summary = result["__interrupt__"][0].value["summary"]["comparison"]
        assert summary["check_status"] == "ok"
        rows = {r["competency_id"]: r for r in summary["posting_checks"]}
        assert (rows["comp:data.pipeline"]["status"], rows["comp:data.pipeline"]["source"]) == (
            "overlap",
            "both",
        )
        cache = rows["comp:sw.cache"]
        assert (cache["status"], cache["source"], cache["claim_ids"]) == (
            "overlap",
            "verify",
            ["d1-c2"],
        )
        assert cache["role"] is None  # ⟦성과⟧ 칸 → 역할 판정 안 함 (불변식 6)
        assert rows["comp:data.pipeline"]["role"] == "design"  # ⟦담당업무⟧ 칸 + 설계 표현
        assert rows["comp:data.stat_ml"]["status"] == "related"
        assert summary["overlap"] == ["데이터 파이프라인", "캐시 설계"]
        assert summary["related"] == ["통계 모델"] and summary["posting_only"] == []
        # 인용·판정 이유는 요약에 넣지 않는다 (W1·W2)
        assert "처리 시간" not in json.dumps(summary, ensure_ascii=False)

        # 승인자: 캐시 설계 겹침을 관련 경험으로 내리고 경험 검증을 다시 (LLM 추가 호출 없음)
        down = {
            **decision("revise", "캐시 근거가 약함"),
            "revise_target": "verification",
            "downgrade_overlap": ["comp:sw.cache", "comp:data.stat_ml"],
        }
        again = await graph.ainvoke(Command(resume=down), cfg)
        s2 = again["__interrupt__"][0].value["summary"]["comparison"]
        rows2 = {r["competency_id"]: r for r in s2["posting_checks"]}
        assert (
            rows2["comp:sw.cache"]["status"] == "related" and rows2["comp:sw.cache"]["downgraded"]
        )
        assert s2["overlap"] == ["데이터 파이프라인"]
        assert again["comparison"].downgraded == ["comp:sw.cache"]
        assert ("downgrade_ignored", "comp:data.stat_ml") in [
            (e.code, e.ref_id) for e in again["errors"]
        ]

        # 다음 revise 에서 다시 보내지 않아도 유지된다
        again2 = await graph.ainvoke(Command(resume=decision("revise", "문구만")), cfg)
        assert again2["comparison"].downgraded == ["comp:sw.cache"]
        # 이미 낮춘 역량을 다시 보내면 조용히 무시 (downgrade_ignored 가 쌓이지 않음)
        again3 = await graph.ainvoke(
            Command(
                resume={
                    **decision("revise", "한 번 더"),
                    "revise_target": "all",
                    "downgrade_overlap": ["comp:sw.cache"],
                }
            ),
            cfg,
        )
        assert [e.code for e in again3["errors"]].count("downgrade_ignored") == 1
        # 되돌리기 → 다시 대조 규칙대로 (확인 경로 action → 겹침, 출처 verify)
        back = {
            **decision("revise", "되돌림"),
            "revise_target": "verification",
            "restore_overlap": ["comp:sw.cache", "comp:data.pipeline"],
        }
        again4 = await graph.ainvoke(Command(resume=back), cfg)
        rows4 = {
            r["competency_id"]: r
            for r in again4["__interrupt__"][0].value["summary"]["comparison"]["posting_checks"]
        }
        assert (rows4["comp:sw.cache"]["status"], rows4["comp:sw.cache"]["source"]) == (
            "overlap",
            "verify",
        )
        assert not rows4["comp:sw.cache"]["downgraded"] and again4["comparison"].downgraded == []
        assert ("restore_ignored", "comp:data.pipeline") in [
            (e.code, e.ref_id) for e in again4["errors"]
        ]
        done = await graph.ainvoke(Command(resume=decision("approved")), cfg)
        assert done["spec"].approval is not None

        # 판정 이유는 State·체크포인트·승인 payload(API 응답) 어디에도 없다
        snap = await graph.aget_state(cfg)
        assert not _has(json.dumps(snap.values, default=str, ensure_ascii=False), REASON)
        assert _has(cp.blobs, "처리 시간") or _has(
            cp.storage, "처리 시간"
        )  # 훑기가 동작하는지 (주장 인용은 State 에 있음)
        assert not _has(cp.storage, REASON) and not _has(cp.blobs, REASON)
        assert not _has(cp.writes, REASON)
        assert not _has(json.dumps(result["__interrupt__"][0].value, ensure_ascii=False), REASON)


async def test_downgrade_needs_revise_and_verification(tmp_path: Path, patched_profile) -> None:
    fake = FakeBackend().push(_json(CLAIMS), _json(PICKS), _json(CHECK))
    cards, m = cardset()
    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(
            cp,
            pseudonymizer=_writer(tmp_path),
            llm=LLMClient({"opencode": fake}),
            prompts=PROMPTS,
            retriever=T1Retriever(cards, m),
        )
        inp = run_input(docs=[fake_doc("d1", "resume")])
        cfg = config_for(inp)
        await graph.ainvoke(inp, cfg)
        bad = {**decision("approved"), "downgrade_overlap": ["comp:sw.cache"]}
        r = await graph.ainvoke(Command(resume=bad), cfg)
        assert "downgrade_overlap" in r["__interrupt__"][0].value["error"]
        common_only = {
            **decision("revise", "x"),
            "revise_target": "common",
            "downgrade_overlap": ["comp:sw.cache"],
        }
        r = await graph.ainvoke(Command(resume=common_only), cfg)
        assert "revise_target" in r["__interrupt__"][0].value["error"]


# 선택만 일치: 선택 경로가 pipeline 을 골랐는데 확인 경로는 none [2026-11-06]
CHECK_SELECT_ONLY = {
    "items": [
        {"competency_id": "comp:data.pipeline", "evidence_claims": [], "label": "none"},
        {"competency_id": "comp:sw.cache", "evidence_claims": ["c2"], "label": "action"},
        {"competency_id": "comp:data.stat_ml", "evidence_claims": [], "label": "none"},
    ]
}


async def test_select_only_promote_and_lower_again(tmp_path: Path, patched_profile) -> None:
    fake = FakeBackend().push(_json(CLAIMS), _json(PICKS), _json(CHECK_SELECT_ONLY))
    cards, m = cardset()
    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(
            cp,
            pseudonymizer=_writer(tmp_path),
            llm=LLMClient({"opencode": fake}),
            prompts=PROMPTS,
            retriever=T1Retriever(cards, m),
        )
        inp = run_input(docs=[fake_doc("d1", "resume")])
        cfg = config_for(inp)
        result = await graph.ainvoke(inp, cfg)

        def rows(r: dict[str, Any]) -> dict[str, dict[str, Any]]:
            s = r["__interrupt__"][0].value["summary"]["comparison"]
            return {x["competency_id"]: x for x in s["posting_checks"]}

        p0 = rows(result)["comp:data.pipeline"]
        assert (p0["status"], p0["select_only"], p0["source"], p0["promoted"]) == (
            "posting_only",
            True,
            None,
            False,
        )
        assert result["comparison"].overlap[0].competency_id == "comp:sw.cache"
        # 검증 후보에는 남는다 (주장 → 역량 연결은 그대로, 사유 표시)
        t = next(x for x in result["comparison"].claims_to_verify if x.claim_id == "d1-c1")
        assert any(r.startswith("선택만 일치(확인 경로 근거 없음)") for r in t.reasons)

        # 승인자가 겹침으로 올림 (선택만 일치가 아닌 stat_ml 은 무시 기록)
        up = {
            **decision("revise", "파이프라인 근거 확인함"),
            "revise_target": "verification",
            "promote_overlap": ["comp:data.pipeline", "comp:data.stat_ml"],
        }
        r1 = await graph.ainvoke(Command(resume=up), cfg)
        p1 = rows(r1)["comp:data.pipeline"]
        assert (p1["status"], p1["source"], p1["promoted"], p1["select_only"]) == (
            "overlap",
            "promoted",
            True,
            True,
        )
        assert r1["comparison"].promoted == ["comp:data.pipeline"]
        assert ("promote_ignored", "comp:data.stat_ml") in [
            (e.code, e.ref_id) for e in r1["errors"]
        ]

        # 다시 올리면 조용히 무시, 다음 revise 에도 유지
        r2 = await graph.ainvoke(
            Command(
                resume={
                    **decision("revise", "한 번 더"),
                    "revise_target": "all",
                    "promote_overlap": ["comp:data.pipeline"],
                }
            ),
            cfg,
        )
        assert r2["comparison"].promoted == ["comp:data.pipeline"]
        assert [e.code for e in r2["errors"]].count("promote_ignored") == 1

        # 올린 겹침을 낮추면 올림 취소 → 원래 자리(공고에만, select_only)
        down = {
            **decision("revise", "다시 내림"),
            "revise_target": "verification",
            "downgrade_overlap": ["comp:data.pipeline"],
        }
        r3 = await graph.ainvoke(Command(resume=down), cfg)
        p3 = rows(r3)["comp:data.pipeline"]
        assert (p3["status"], p3["promoted"], p3["downgraded"], p3["select_only"]) == (
            "posting_only",
            False,
            False,
            True,
        )
        assert r3["comparison"].promoted == [] and r3["comparison"].downgraded == []

        def revise(comment: str, **lists: list[str]) -> Command:
            return Command(
                resume={**decision("revise", comment), "revise_target": "verification", **lists}
            )

        # 올림 → 내림 → 다시 올림
        r4 = await graph.ainvoke(revise("다시 올림", promote_overlap=["comp:data.pipeline"]), cfg)
        assert r4["comparison"].promoted == ["comp:data.pipeline"]
        # 확인 action 겹침(cache)을 낮추고, 올린 pipeline 도 같은 결정에서 내림
        r5 = await graph.ainvoke(
            revise("둘 다 내림", downgrade_overlap=["comp:sw.cache", "comp:data.pipeline"]), cfg
        )
        assert r5["comparison"].downgraded == ["comp:sw.cache"] and r5["comparison"].promoted == []
        # 낮춘 역량을 바로 올리면 무시 기록 + 되돌리기 안내
        r6 = await graph.ainvoke(revise("캐시 올림", promote_overlap=["comp:sw.cache"]), cfg)
        msgs = [(e.code, e.message, e.ref_id) for e in r6["errors"]]
        assert (
            "promote_ignored",
            "낮춘 역량은 올릴 수 없음 — 되돌리기(restore)를 먼저 쓰세요",
            "comp:sw.cache",
        ) in msgs
        assert r6["comparison"].downgraded == ["comp:sw.cache"] and r6["comparison"].promoted == []
        # 같은 revise 에서 되돌리기(cache)와 올리기(pipeline)를 함께
        r7 = await graph.ainvoke(
            revise(
                "되돌리고 올림",
                restore_overlap=["comp:sw.cache"],
                promote_overlap=["comp:data.pipeline"],
            ),
            cfg,
        )
        p7 = rows(r7)
        assert (p7["comp:sw.cache"]["status"], p7["comp:sw.cache"]["source"]) == (
            "overlap",
            "verify",
        )
        assert (p7["comp:data.pipeline"]["status"], p7["comp:data.pipeline"]["source"]) == (
            "overlap",
            "promoted",
        )
        assert r7["comparison"].downgraded == [] and r7["comparison"].promoted == [
            "comp:data.pipeline"
        ]
        done = await graph.ainvoke(Command(resume=decision("approved")), cfg)
        assert done["spec"].approval is not None
