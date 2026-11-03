"""자기소개서 사용 여부 [확정: 2026-11-03 — 검증할 주장은 이력서에서만]. 가상 문서만."""

from __future__ import annotations

from task_spec.nodes.ingest import make_ingest
from task_spec.schemas import DocumentRef, ExtractedRef, RunState
from tests.conftest import fake_doc


async def _ingestor(run_id: str, doc: DocumentRef) -> ExtractedRef:
    return ExtractedRef(doc_id=doc.doc_id, kind=doc.kind, ok=True, method="txt")


def _state() -> RunState:
    return RunState(
        run_id="r1",
        ontology_version="onto-v2026.11.02",
        posting_id="p",
        documents=[fake_doc("d1", "resume"), fake_doc("d2", "cover_letter")],
    )


async def test_cover_letter_is_skipped_by_default() -> None:
    out = await make_ingest(_ingestor)(_state())
    assert [r.doc_id for r in out["extracted"]] == ["d1"]
    assert [(e.code, e.ref_id) for e in out["errors"]] == [("cover_letter_unused", "d2")]


async def test_cover_letter_used_when_enabled() -> None:
    out = await make_ingest(_ingestor, use_cover_letters=True)(_state())
    assert [r.doc_id for r in out["extracted"]] == ["d1", "d2"] and out["errors"] == []
