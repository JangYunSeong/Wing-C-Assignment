"""② 입력 처리 노드 — 원본 → 추출·구조화 → `data/extracted/<run_id>/<doc_id>.{txt,json}` (권한 600).

가명화 이전 단계: LLM·임베딩 import 금지 (CLAUDE.md 불변식 2, 훅이 막는다).
추출본에는 원문 개인정보가 있으므로 State 에는 `ExtractedRef`(경로·해시·방법·건수)만 둔다.
실패한 문서는 `ok=False` + 사유로 남기고, 가명화 단계가 이를 실패로 넘겨 실행이 멈춘다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from task_spec.nodes.ingest.extractors import OcrError, UnsupportedFormat, extract_raw
from task_spec.nodes.ingest.structure import structure
from task_spec.schemas import DocumentRef, ExtractedDocument, ExtractedRef, NodeError, RunState

logger = logging.getLogger(__name__)

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
Ingestor = Callable[[str, DocumentRef], Awaitable[ExtractedRef]]


async def stub_ingestor(run_id: str, doc: DocumentRef) -> ExtractedRef:
    """골격·그래프 테스트용: 아무것도 읽지 않고 통과로 표시한다 (stub_pseudonymizer 와 짝)."""
    return ExtractedRef(doc_id=doc.doc_id, kind=doc.kind, ok=True, method="txt")


def _write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)


class FileIngestor:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir.resolve()

    async def __call__(self, run_id: str, doc: DocumentRef) -> ExtractedRef:
        return await asyncio.to_thread(self._run, run_id, doc)

    def _failed(self, doc: DocumentRef, reason: str) -> ExtractedRef:
        return ExtractedRef(doc_id=doc.doc_id, kind=doc.kind, ok=False, reason=reason)  # type: ignore[arg-type]

    def _run(self, run_id: str, doc: DocumentRef) -> ExtractedRef:
        """어떤 예외도 메시지 그대로 밖으로 내보내지 않는다 — pydantic ValidationError 등은 원문을 담는다 (감사 W1)."""
        if not (SAFE_ID.match(run_id) and SAFE_ID.match(doc.doc_id)):
            return self._failed(doc, "extract_error")
        work_dir = self._data_dir / "tmp" / run_id
        try:
            return self._run_unsafe(run_id, doc, work_dir)
        except Exception as exc:
            logger.warning(
                "ingest failed run=%s doc=%s exc=%s", run_id, doc.doc_id, type(exc).__name__
            )
            return self._failed(doc, "extract_error")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _run_unsafe(self, run_id: str, doc: DocumentRef, work_dir: Path) -> ExtractedRef:
        src = Path(doc.path).resolve()
        if not src.is_relative_to(self._data_dir / "raw" / run_id) or not src.is_file():
            return self._failed(doc, "extract_error")
        if hashlib.sha256(src.read_bytes()).hexdigest() != doc.sha256:
            return self._failed(doc, "extract_error")
        try:
            raw = extract_raw(src, work_dir)
        except UnsupportedFormat:
            return self._failed(doc, "unsupported_format")
        except OcrError:
            return self._failed(doc, "ocr_error")
        except Exception as exc:  # 손상 파일 등 — 메시지에 내용이 섞일 수 있어 타입만 남긴다
            logger.warning(
                "extract failed run=%s doc=%s exc=%s", run_id, doc.doc_id, type(exc).__name__
            )
            return self._failed(doc, "extract_error")

        text, sections = structure(raw.lines)
        if not text.strip():
            return self._failed(doc, "empty_text")
        extracted = ExtractedDocument(
            doc_id=doc.doc_id,
            method=raw.method,  # type: ignore[arg-type]
            text=text,
            sections=sections,
            pages=raw.pages,
            warnings=raw.warnings,
        )
        out_dir = self._data_dir / "extracted" / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(out_dir.parent, 0o700)
        os.chmod(out_dir, 0o700)
        txt = out_dir / f"{doc.doc_id}.txt"
        _write_private(txt, text)
        _write_private(out_dir / f"{doc.doc_id}.json", extracted.model_dump_json())
        n_fields = sum(len(s.fields) for s in sections)
        logger.info(
            "extracted run=%s doc=%s method=%s pages=%d chars=%d sections=%d fields=%d",
            run_id,
            doc.doc_id,
            raw.method,
            raw.pages,
            len(text),
            len(sections),
            n_fields,
        )
        return ExtractedRef(
            doc_id=doc.doc_id,
            kind=doc.kind,
            ok=True,
            path=str(txt),
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            method=raw.method,  # type: ignore[arg-type]
            sections=len(sections),
            fields=n_fields,
            warnings=raw.warnings,
        )


def make_ingest(
    ingestor: Ingestor, *, use_cover_letters: bool = False
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    """use_cover_letters=False 면 자기소개서는 읽지 않는다 [확정: 2026-11-03 — 검증할 주장은 이력서에서만]."""

    async def ingest(state: RunState) -> dict[str, Any]:
        docs = [d for d in state.documents if use_cover_letters or d.kind != "cover_letter"]
        skipped = [
            NodeError(
                node="ingest",
                code="cover_letter_unused",
                message="자기소개서는 현재 쓰지 않음 (USE_COVER_LETTERS=false)",
                ref_id=d.doc_id,
            )
            for d in state.documents
            if d not in docs
        ]
        refs = [await ingestor(state.run_id, d) for d in docs]
        errors = skipped + [
            NodeError(
                node="ingest",
                code=r.reason or "extract_error",
                message="텍스트 추출 실패",
                ref_id=r.doc_id,
            )
            for r in refs
            if not r.ok
        ]
        return {"extracted": refs, "errors": errors}

    return ingest
