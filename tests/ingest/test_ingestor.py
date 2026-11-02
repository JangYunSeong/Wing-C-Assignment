"""FileIngestor: 경로 가두기·해시·권한·실패 사유·로그."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from task_spec.nodes.ingest import FileIngestor
from task_spec.schemas import DocumentRef
from tests.ingest import docs


def _raw(root: Path, run_id: str, name: str, make=None, body: bytes | None = None) -> DocumentRef:  # type: ignore[no-untyped-def]
    p = root / "raw" / run_id / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if make is not None:
        make(p)
    else:
        p.write_bytes(body or b"")
    data = p.read_bytes()
    return DocumentRef(
        doc_id=Path(name).stem, kind="resume", path=str(p), sha256=hashlib.sha256(data).hexdigest()
    )


async def test_pdf_extracted_privately_with_structure(tmp_path: Path) -> None:
    ref = await FileIngestor(tmp_path)(
        "run1", _raw(tmp_path, "run1", "resume-1.pdf", docs.one_column_pdf)
    )
    assert ref.ok and ref.method == "text_layer" and ref.fields == 3
    txt = Path(ref.path)  # type: ignore[arg-type]
    assert stat.S_IMODE(os.stat(txt).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(txt.parent).st_mode) == 0o700
    assert hashlib.sha256(txt.read_bytes()).hexdigest() == ref.sha256
    full = json.loads(txt.with_suffix(".json").read_text(encoding="utf-8"))
    assert full["sections"][0]["kind"] == "경력"


@pytest.mark.parametrize(
    ("name", "body", "reason"),
    [
        ("resume-1.hwp", b"HWP Document File", "unsupported_format"),
        ("resume-1.pdf", b"%PDF-1.4 broken", "extract_error"),
        ("resume-1.txt", b"   \n  ", "empty_text"),
    ],
)
async def test_failures_have_reason(tmp_path: Path, name: str, body: bytes, reason: str) -> None:
    ref = await FileIngestor(tmp_path)("run1", _raw(tmp_path, "run1", name, body=body))
    assert not ref.ok and ref.reason == reason and ref.path is None


async def test_source_outside_raw_rejected(tmp_path: Path) -> None:
    doc = _raw(tmp_path, "other-run", "resume-1.txt", body="성명: 김가상".encode())
    ref = await FileIngestor(tmp_path)("run1", doc)
    assert not ref.ok


async def test_tampered_source_rejected(tmp_path: Path) -> None:
    doc = _raw(tmp_path, "run1", "resume-1.txt", body="원래 내용".encode())
    Path(doc.path).write_text("바뀐 내용", encoding="utf-8")
    assert not (await FileIngestor(tmp_path)("run1", doc)).ok


@pytest.mark.parametrize("run_id", ["../x", "a/b", ""])
async def test_unsafe_run_id(tmp_path: Path, run_id: str) -> None:
    doc = _raw(tmp_path, "run1", "resume-1.txt", body="가상".encode())
    assert not (await FileIngestor(tmp_path)(run_id, doc)).ok


async def test_logs_have_no_content(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await FileIngestor(tmp_path)(
        "run1", _raw(tmp_path, "run1", "resume-1.pdf", docs.one_column_pdf)
    )
    assert "가상데이터랩" not in caplog.text and "집계" not in caplog.text
    assert "method=text_layer" in caplog.text


async def test_unexpected_exception_is_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from task_spec.nodes.ingest import node as mod

    def boom(lines):  # type: ignore[no-untyped-def]
        raise ValueError("원문 조각 김가상 이 예외에 들어감")

    monkeypatch.setattr(mod, "structure", boom)
    caplog.set_level(logging.DEBUG)
    ref = await FileIngestor(tmp_path)(
        "run1", _raw(tmp_path, "run1", "resume-1.txt", body="성명: 김가상".encode())
    )
    assert not ref.ok and ref.reason == "extract_error"
    assert "김가상" not in caplog.text and "ValueError" in caplog.text
    assert not (tmp_path / "tmp" / "run1").exists()  # 작업 폴더 정리


async def test_upload_originals_are_private(tmp_path: Path) -> None:
    from task_spec.api.uploads import _write_all

    _write_all(tmp_path / "raw" / "run1", [("resume-1.txt", b"x")])
    assert stat.S_IMODE(os.stat(tmp_path / "raw" / "run1").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "raw" / "run1" / "resume-1.txt").st_mode) == 0o600
