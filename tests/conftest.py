"""공통 픽스처. 데이터는 전부 가상 (실존 인물·학교·회사 아님)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.graph.state import CompiledStateGraph

from task_spec.graph import build_graph, make_checkpointer
from task_spec.nodes.pseudonymize import Pseudonymizer
from task_spec.schemas import DocumentRef, PseudoCheck, PseudoDocRef


def fake_doc(doc_id: str, kind: str) -> DocumentRef:
    # 가상 데이터: 경로·해시는 실제 파일이 아니다.
    return DocumentRef(
        doc_id=doc_id,
        kind=kind,  # type: ignore[arg-type]
        path=f"data/raw/test/{doc_id}.txt",
        sha256=hashlib.sha256(doc_id.encode()).hexdigest(),
    )


def run_input(*, docs: list[DocumentRef] | None = None) -> dict[str, Any]:
    return {
        "run_id": f"run-{uuid.uuid4().hex[:8]}",
        "ontology_version": "onto-v2026.11.02",
        "posting_id": "posting-fake-001",
        "documents": [d.model_dump() for d in (docs or [])],
    }


def decision(status: str, comment: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reviewer": "reviewer-fake",
        "comment": comment,
        "at": datetime.now(UTC).isoformat(),
    }


def config_for(inp: dict[str, Any]) -> dict[str, Any]:
    return {"configurable": {"thread_id": inp["run_id"]}}


async def failing_pseudonymizer(run_id: str, doc: DocumentRef) -> PseudoDocRef:
    return PseudoDocRef(
        doc_id=doc.doc_id,
        kind=doc.kind,
        check=PseudoCheck(passed=False, residual_count=2, detectors=["email", "url"]),
    )


@pytest.fixture
async def memory_graph() -> AsyncIterator[CompiledStateGraph]:
    async with make_checkpointer(kind="memory") as cp:
        yield build_graph(cp)


@pytest.fixture
def graph_with() -> Any:
    """가명화기를 바꿔 끼운 그래프를 만드는 팩토리."""

    async def _make(pseudonymizer: Pseudonymizer) -> CompiledStateGraph:
        cm = make_checkpointer(kind="memory")
        cp = await cm.__aenter__()
        return build_graph(cp, pseudonymizer=pseudonymizer)

    return _make
