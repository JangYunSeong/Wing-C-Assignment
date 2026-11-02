from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from task_spec.nodes.ingest.extractors import UnsupportedFormat, extract_raw
from task_spec.nodes.ingest.structure import structure
from tests.ingest import docs


def test_one_column_pdf_keeps_right_aligned_period_on_header_line(tmp_path: Path) -> None:
    raw = extract_raw(docs.one_column_pdf(tmp_path / "a.pdf"))
    assert raw.method == "text_layer" and raw.pages == 1
    header = next(ln for ln in raw.lines if "가상데이터랩" in ln)
    assert "2023.01 ~ 2024.12" in header  # 오른쪽 정렬 기간이 같은 줄
    text, sections = structure(raw.lines)
    career = next(s for s in sections if s.kind == "경력")
    assert career.headers[0].period == "2023.01 ~ 2024.12"
    assert [f.label for f in career.fields] == ["문제 정의", "과정", "기여한 점"]
    assert "⟦과정⟧ Airflow DAG 를 재구성하고 Spark 집계를 파티션 단위로 나눴다." in text


def test_two_column_pdf_reads_left_then_right(tmp_path: Path) -> None:
    raw = extract_raw(docs.two_column_pdf(tmp_path / "b.pdf"))
    assert "page1:two_columns" in raw.warnings
    joined = "\n".join(raw.lines)
    assert joined.index("왼쪽 단 내용 줄 9") < joined.index("자기소개")
    assert not any("왼쪽" in ln and "오른쪽" in ln for ln in raw.lines)  # 줄이 섞이지 않음


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract 미설치")
def test_scanned_pdf_uses_ocr(tmp_path: Path) -> None:
    raw = extract_raw(docs.scanned_pdf(tmp_path / "c.pdf"), tmp_path / "work")
    assert raw.method == "ocr"
    joined = "".join(raw.lines).replace(" ", "")
    assert "문제정의" in joined and "기여한점" in joined


def test_docx_paragraphs_and_table_in_order(tmp_path: Path) -> None:
    raw = extract_raw(docs.docx_file(tmp_path / "d.docx"))
    assert raw.method == "docx"
    text, sections = structure(raw.lines)
    labels = [f.label for s in sections for f in s.fields]
    assert labels == ["문제 정의", "과정", "기여한 점", "사용 기술"]
    assert text.index("⟦과정⟧") < text.index("⟦기여한 점⟧")


def test_hwpx_paragraphs(tmp_path: Path) -> None:
    raw = extract_raw(docs.hwpx_file(tmp_path / "e.hwpx"))
    assert raw.method == "hwpx"
    _, sections = structure(raw.lines)
    assert sections[0].kind == "경력" and sections[0].fields[0].label == "기여한 점"


def test_hwp_binary_unsupported(tmp_path: Path) -> None:
    p = tmp_path / "f.hwp"
    p.write_bytes(b"HWP Document File")
    with pytest.raises(UnsupportedFormat):
        extract_raw(p)


def test_hwpx_without_sections_rejected(tmp_path: Path) -> None:
    p = tmp_path / "g.hwpx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
    with pytest.raises(UnsupportedFormat):
        extract_raw(p)


def test_scanned_pdf_without_work_dir_fails_closed(tmp_path: Path) -> None:
    from task_spec.nodes.ingest.extractors import OcrError

    with pytest.raises(OcrError):
        extract_raw(docs.scanned_pdf(tmp_path / "c.pdf"))


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract 미설치")
def test_ocr_leaves_no_temp_images(tmp_path: Path) -> None:
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("tess_*"))
    work = tmp_path / "work"
    extract_raw(docs.scanned_pdf(tmp_path / "c.pdf"), work)
    assert list(work.glob("*")) == []  # 작업 이미지는 지워진다
    assert set(Path(tempfile.gettempdir()).glob("tess_*")) == before  # /tmp 에 남기지 않는다


def test_docx_zip_bomb_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from task_spec.nodes.ingest import extractors

    monkeypatch.setattr(extractors, "ZIP_MAX_UNCOMPRESSED", 100)
    with pytest.raises(UnsupportedFormat):
        extract_raw(docs.docx_file(tmp_path / "d.docx"))


def test_hwpx_entity_expansion_rejected(tmp_path: Path) -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
        '<hs:sec xmlns:hs="h" xmlns:hp="p"><hp:p><hp:run><hp:t>&b;</hp:t></hp:run></hp:p></hs:sec>'
    )
    p = tmp_path / "bomb.hwpx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("Contents/section0.xml", bomb)
    from defusedxml import EntitiesForbidden

    with pytest.raises(EntitiesForbidden):  # defusedxml 이 엔티티 선언을 거부한다
        extract_raw(p)
