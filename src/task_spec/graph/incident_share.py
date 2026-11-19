"""회사 결정적 사건 공유 승인 그래프 [확정: 2026-11-19 사용자] — 빌드 승인 그래프(graph/build_approval.py)를 본뜬다.

    incident_share(interrupt) ─ approved → finalize_share → END
                              └ rejected → reject_share   → END

- thread 는 (사례, 내용 지문)마다 하나 — `thread_id(rec)`. 사례 내용은 등록 뒤 바뀌지 않지만, 지문을 붙여 다른 내용에 결정이
  적용되지 않게 한다. resume 하면 노드가 처음부터 다시 돌며 저장소의 지금 상태를 보고, 이미 결정·보관됐으면 stale 로 끝낸다.
- 승인은 interrupt 로만 (불변식 9). 잘못된 결정 값(반려 사유 없음 등)이면 사유를 붙여 다시 interrupt 한다.
- interrupt() 앞에는 부작용을 두지 않는다 (저장소 읽기·탐지만).
- 대기 내용에 사례 본문을 싣지 않는다 — 체크포인트에 회사 사례 사본을 두지 않는다 (화면은 GET /incidents/{id} 로 본다).
  대신 가명화와 같은 로컬 탐지기로 사람 이름·회사·제품 등으로 보이는 표현을 칸별로 센 것만 싣는다 (값은 싣지 않음).
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from task_spec.schemas import IncidentShareDecision, IncidentShareState
from task_spec.spec.company_incidents import (
    CompanyIncident,
    CompanyIncidentRepo,
    Detect,
    scan_incident,
)

Route = Literal["finalize_share", "reject_share", "__end__"]
_END: Literal["__end__"] = "__end__"  # langgraph END (str 로만 알려져 있어 타입을 좁힌다)


def thread_id(rec: CompanyIncident) -> str:
    return f"incident-{rec.incident_id.replace(':', '-')}-{rec.content_sha256[:12]}"


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'decision'}: {e['msg']}" for e in exc.errors()
    )[:300]


def incident_share_graph(
    store: CompanyIncidentRepo,
    checkpointer: BaseCheckpointSaver,
    *,
    detect: Detect | None = None,  # None = 가명화와 같은 로컬 탐지기 (테스트는 가짜)
) -> CompiledStateGraph:
    async def incident_share(state: IncidentShareState) -> Command[Route]:
        rec = await store.get(state.incident_id)
        if rec is None or rec.status != "pending" or rec.content_sha256 != state.content_sha256:
            return Command(
                update={"result": "stale"}, goto=_END
            )  # 이미 결정·보관됨 — 이 thread 는 낡았다
        found = await asyncio.to_thread(scan_incident, rec.incident, detect)
        payload: dict[str, Any] = {
            "incident_id": rec.incident_id,
            "company_id": rec.company_id,
            "sub": rec.sub,
            "source": str(rec.source.get("kind", "manual")),
            "content_sha256": rec.content_sha256,
            "flags": [f.text() for f in found],
            "error": state.error,
        }
        while True:
            raw = interrupt(payload)
            try:
                decision = IncidentShareDecision.model_validate(raw)
            except ValidationError as exc:
                payload = {**payload, "error": _summarize(exc)}
                continue
            break
        goto: Route = "finalize_share" if decision.status == "approved" else "reject_share"
        return Command(update={"decision": decision, "error": None}, goto=goto)

    async def finalize_share(state: IncidentShareState) -> dict[str, Any]:
        d = state.decision
        assert d is not None
        ok = await store.decide(
            state.incident_id, state.content_sha256, "shared", d.reviewer, d.at, d.comment
        )
        return {"result": "shared" if ok else "stale"}

    async def reject_share(state: IncidentShareState) -> dict[str, Any]:
        d = state.decision
        assert d is not None
        ok = await store.decide(
            state.incident_id, state.content_sha256, "rejected", d.reviewer, d.at, d.comment
        )
        return {"result": "rejected" if ok else "stale"}

    g = StateGraph(IncidentShareState)
    g.add_node("incident_share", incident_share)
    g.add_node("finalize_share", finalize_share)
    g.add_node("reject_share", reject_share)
    g.add_edge(START, "incident_share")
    g.add_edge("finalize_share", END)
    g.add_edge("reject_share", END)
    return g.compile(checkpointer=checkpointer)
