"""운영용 가명화기: 규칙 + 공개 사전 + 로컬 형태소 분석기(Kiwi). 외부 API 호출 없음 (불변식 2).

- 입력은 텍스트만. PDF·DOCX 는 document-ingest 가 텍스트를 뽑기 전까지 `ingest_required` 로 멈춘다.
- 출력: `data/pseudonymized/<run_id>/<doc_id>.txt` (권한 600) + 실행 단위 `mapping.json` (권한 600).
- 누락 검사 실패면 가명화본을 쓰지 않고 실패로 돌려준다 → 그래프가 END 로 간다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
from pathlib import Path

from task_spec.nodes.pseudonymize.dictionaries import Dictionaries, load_dictionaries
from task_spec.nodes.pseudonymize.engine import (
    Detectors,
    RunMapping,
    ensure_private_dir,
    pseudonymize_text,
)
from task_spec.schemas import DocumentRef, PseudoCheck, PseudoDocRef

logger = logging.getLogger(__name__)

TEXT_SUFFIXES = frozenset({".txt"})
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _failed(doc: DocumentRef, reason: str, residual: int = 0) -> PseudoDocRef:
    return PseudoDocRef(
        doc_id=doc.doc_id,
        kind=doc.kind,
        check=PseudoCheck(passed=False, residual_count=residual, reason=reason),  # type: ignore[arg-type]
    )


def _write_private(path: Path, text: str) -> None:
    """심볼릭 링크를 따라가지 않고, 이미 있던 파일이어도 600 으로 만든다."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


class RulePseudonymizer:
    def __init__(
        self, data_dir: Path, *, dicts: Dictionaries | None = None, use_kiwi: bool = True
    ) -> None:
        self._data_dir = data_dir.resolve()
        self._root = self._data_dir / "pseudonymized"
        self._detectors = Detectors(dicts or load_dictionaries(), use_kiwi=use_kiwi)
        self._lock = threading.Lock()  # 같은 run 의 mapping.json 동시 갱신 방지

    async def warm_up(self) -> None:
        """탐지기를 한 번 돌려 Kiwi 모델을 미리 올린다."""
        await asyncio.to_thread(self._detectors.detect, "가상 데이터 워밍업")

    async def __call__(self, run_id: str, doc: DocumentRef) -> PseudoDocRef:
        return await asyncio.to_thread(self._run, run_id, doc)

    def _run(self, run_id: str, doc: DocumentRef) -> PseudoDocRef:
        # 경로 가두기: id 형식 검사 + 원본은 data_dir/raw/<run_id>/ 아래만 (privacy-auditor W2)
        if not (SAFE_ID.match(run_id) and SAFE_ID.match(doc.doc_id)):
            return _failed(doc, "read_error")
        src = Path(doc.path).resolve()
        if not src.is_relative_to(self._data_dir / "raw" / run_id):
            return _failed(doc, "read_error")
        if src.suffix.lower() not in TEXT_SUFFIXES:
            return _failed(doc, "ingest_required")
        try:
            text = src.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _failed(doc, "read_error")

        out_dir = self._root / run_id
        with self._lock:
            mapping_path = out_dir / "mapping.json"
            mapping = RunMapping.load(mapping_path)
            result = pseudonymize_text(text, mapping, self._detectors)
            if not result.residual:  # 실패한 문서의 원 값은 디스크에 남기지 않는다 (W5)
                ensure_private_dir(self._root)
                mapping.save(mapping_path)

        logger.info(
            "pseudonymized run=%s doc=%s counts=%s residual=%d",
            run_id,
            doc.doc_id,
            ",".join(f"{k}={v}" for k, v in sorted(result.counts.items())) or "-",
            result.residual,
        )
        if result.residual:
            return _failed(doc, "residual_pii", result.residual)

        out_path = out_dir / f"{doc.doc_id}.txt"
        _write_private(out_path, result.text)
        return PseudoDocRef(
            doc_id=doc.doc_id,
            kind=doc.kind,
            path=str(out_path),
            sha256=hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            check=PseudoCheck(
                passed=True,
                residual_count=0,
                detectors=result.detectors,
                counts=result.counts,
            ),
        )
