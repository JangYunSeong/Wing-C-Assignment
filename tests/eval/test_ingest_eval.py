"""추출 평가 회귀 — dev 세트 축소판(레이아웃당 3건). 기준은 2026-11-02 측정값.

텍스트층 형식(PDF·DOCX·HWPX)은 완전 일치, 스캔본(OCR)은 칸 재현율 하한만.
"""

from __future__ import annotations

import shutil

import pytest

from eval.ingest.run_eval import evaluate

TEXT_LAYOUTS = ["pdf_basic", "pdf_right_date", "pdf_two_col", "docx_para", "docx_table", "hwpx"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract 미설치")
def test_ingest_dev_floors() -> None:
    s = evaluate("dev", per_layout=3)["summary"]
    for layout in TEXT_LAYOUTS:
        m = s[layout]
        assert m["label_recall"] == 1.0 and m["labels_exact"] == 1.0, (layout, m)
        assert m["text_similarity"] >= 0.99 and m["period_recall"] == 1.0, (layout, m)
        assert m["label_extra"] == 0, (layout, m)
    assert s["pdf_two_col"]["two_col_order"] == 1.0
    assert s["pdf_scan"]["label_recall"] >= 0.6, s["pdf_scan"]
