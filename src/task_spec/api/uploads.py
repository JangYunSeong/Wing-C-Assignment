"""업로드 저장. 받은 파일은 즉시 `data_dir/raw/<run_id>/` 에 쓰고 (경로·sha256) 참조만 State 로 넘긴다.

파일명은 신뢰하지 않는다 — 저장 이름은 `<doc_id><확장자>`. 원본 파일명·내용은 응답·로그에 남기지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path

from fastapi import UploadFile

from task_spec.api.errors import FileTooLarge, InvalidUpload, UnsupportedFileType
from task_spec.schemas import DocumentKind, DocumentRef

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx", ".hwpx", ".txt"})
_CHUNK = 1024 * 1024


async def _read_limited(upload: UploadFile, limit: int, run_id: str) -> bytes:
    buf = bytearray()
    while chunk := await upload.read(_CHUNK):
        buf += chunk
        if len(buf) > limit:
            raise FileTooLarge(f"파일 크기 상한({limit} bytes)을 넘었습니다", run_id=run_id)
    return bytes(buf)


def _write_all(run_dir: Path, items: list[tuple[str, bytes]]) -> None:
    """원본(원문 PII)은 700 폴더 / 600 파일로 (추출본·가명화본과 같은 수준, 감사 W6)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for d in (run_dir.parent, run_dir):
        os.chmod(d, 0o700)
    for name, data in items:
        fd = os.open(run_dir / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)


async def store_uploads(
    *,
    run_id: str,
    data_dir: Path,
    max_bytes: int,
    uploads: list[tuple[DocumentKind, UploadFile]],
) -> list[DocumentRef]:
    """전부 검증한 뒤 한꺼번에 쓴다 (일부만 저장된 상태를 남기지 않는다)."""
    run_dir = data_dir / "raw" / run_id
    staged: list[tuple[str, bytes]] = []
    refs: list[DocumentRef] = []
    counters: dict[str, int] = {}
    for kind, upload in uploads:
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise UnsupportedFileType(
                f"허용 형식은 {', '.join(sorted(ALLOWED_EXTENSIONS))} 입니다", run_id=run_id
            )
        data = await _read_limited(upload, max_bytes, run_id)
        if not data:
            raise InvalidUpload("빈 파일은 받을 수 없습니다", run_id=run_id)
        counters[kind] = counters.get(kind, 0) + 1
        doc_id = f"{kind}-{counters[kind]}"
        staged.append((f"{doc_id}{ext}", data))
        refs.append(
            DocumentRef(
                doc_id=doc_id,
                kind=kind,
                path=str(run_dir / f"{doc_id}{ext}"),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    if staged:
        try:
            await asyncio.to_thread(_write_all, run_dir, staged)
        except OSError:
            await asyncio.to_thread(shutil.rmtree, run_dir, True)
            raise
    return refs
