"""운영용 가명화기: 규칙 + 공개 사전 + 로컬 형태소 분석기(Kiwi). 외부 API 호출 없음 (불변식 2).

- 입력은 ingest 가 만든 추출본(`data/extracted/<run_id>/<doc_id>.txt`)만. sha256 을 대조한다.
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
    pseudonymize_documents,
)
from task_spec.schemas import ExtractedRef, PseudoCheck, PseudoDocRef

logger = logging.getLogger(__name__)

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _failed(doc: ExtractedRef, reason: str, residual: int = 0) -> PseudoDocRef:
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

    async def __call__(self, run_id: str, doc: ExtractedRef) -> PseudoDocRef:
        return (await self.batch(run_id, [doc]))[0]

    async def batch(self, run_id: str, docs: list[ExtractedRef]) -> list[PseudoDocRef]:
        """한 run 의 문서를 함께 처리한다 — 전부 탐지한 뒤 전파한다 (code review M5, `pseudonymize_documents`)."""
        return await asyncio.to_thread(self._run_batch, run_id, docs)

    def _read(self, run_id: str, doc: ExtractedRef) -> str | PseudoDocRef:
        # 경로 가두기: id 형식 검사 + 추출본은 data_dir/extracted/<run_id>/ 아래만 (privacy-auditor W2)
        if not (SAFE_ID.match(run_id) and SAFE_ID.match(doc.doc_id)):
            return _failed(doc, "read_error")
        if not doc.ok or doc.path is None or doc.sha256 is None:
            return _failed(doc, doc.reason or "read_error")
        given = Path(doc.path)
        if given.is_symlink() or given.name != f"{doc.doc_id}.txt":  # 감사 W7
            return _failed(doc, "read_error")
        src = given.resolve()
        if not src.is_relative_to(self._data_dir / "extracted" / run_id):
            return _failed(doc, "read_error")
        try:
            raw = src.read_bytes()
            if hashlib.sha256(raw).hexdigest() != doc.sha256:
                return _failed(doc, "read_error")
            return raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return _failed(doc, "read_error")

    def _run_batch(self, run_id: str, docs: list[ExtractedRef]) -> list[PseudoDocRef]:
        read = [self._read(run_id, d) for d in docs]
        todo = [(i, r) for i, r in enumerate(read) if isinstance(r, str)]
        out: list[PseudoDocRef | None] = [r if isinstance(r, PseudoDocRef) else None for r in read]
        if not todo:
            return [r for r in out if r is not None]
        out_dir = self._root / run_id
        with self._lock:
            mapping_path = out_dir / "mapping.json"
            mapping = RunMapping.load(mapping_path)
            results = pseudonymize_documents([t for _, t in todo], mapping, self._detectors)
            if not any(
                r.residual for r in results
            ):  # 실패한 문서의 원 값은 디스크에 남기지 않는다 (W5)
                ensure_private_dir(self._root)
                mapping.save(mapping_path)
        any_failed = any(r.residual for r in results)
        for (i, _), result in zip(todo, results, strict=True):
            doc = docs[i]
            logger.info(
                "pseudonymized run=%s doc=%s counts=%s residual=%d",
                run_id,
                doc.doc_id,
                ",".join(f"{k}={v}" for k, v in sorted(result.counts.items())) or "-",
                result.residual,
            )
            out_path = out_dir / f"{doc.doc_id}.txt"
            if result.residual:
                out[i] = _failed(doc, "residual_pii", result.residual)
                out_path.unlink(missing_ok=True)
                continue
            if any_failed:
                # 같은 batch 의 다른 문서가 걸리면 실행이 멈춘다 — 통과한 문서도 쓰지 않고, 앞서 쓴 것이 있으면 지운다
                # (마지막 점검 L2: 매핑을 저장하지 않으므로 남길 이유가 없다)
                out[i] = _failed(doc, "batch_failed")
                out_path.unlink(missing_ok=True)
                continue
            _write_private(out_path, result.text)
            out[i] = PseudoDocRef(
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
        return [r for r in out if r is not None]
