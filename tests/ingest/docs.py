"""테스트용 가상 지원서 파일 생성기 (PDF·스캔 PDF·DOCX·HWPX). 내용은 전부 가상."""

from __future__ import annotations

import zipfile
from pathlib import Path

FONT = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
FONT_KR_INDEX = 1

LINES = [
    "가상 데이터 — 실존 인물 아님",
    "경력",
    "가상데이터랩 데이터 엔지니어",
    "⟨right⟩2023.01 ~ 2024.12",
    "문제 정의: 주문 로그 집계가 하루 6시간 걸렸다.",
    "과정: Airflow DAG 를 재구성하고 Spark 집계를 파티션 단위로 나눴다.",
    "기여한 점: 집계 시간을 6시간에서 40분으로 줄였다.",
    "학력",
    "가상대학교 컴퓨터공학과 2016.03 ~ 2020.02",
]


def _pdf():  # type: ignore[no-untyped-def]
    from fpdf import FPDF

    pdf = FPDF(unit="pt", format="A4")
    pdf.add_font("noto", fname=FONT, collection_font_number=FONT_KR_INDEX)
    pdf.set_font("noto", size=11)
    pdf.add_page()
    return pdf


def one_column_pdf(path: Path) -> Path:
    """머리말 줄 오른쪽 끝에 기간을 정렬한 1단 이력서."""
    pdf = _pdf()
    y = 60.0
    for line in LINES:
        if line.startswith("⟨right⟩"):
            text = line.removeprefix("⟨right⟩")
            pdf.text(
                x=595 - 60 - pdf.get_string_width(text), y=y - 16, text=text
            )  # 앞 줄과 같은 높이
            continue
        pdf.text(x=60, y=y, text=line)
        y += 16
    pdf.output(str(path))
    return path


def two_column_pdf(path: Path) -> Path:
    """왼쪽 단: 경력 칸, 오른쪽 단: 자기소개 — 줄이 섞이면 안 된다."""
    left = [
        "경력",
        "가상데이터랩 2023.01 ~ 2024.12",
        "문제 정의: 집계가 느렸다.",
        "과정: 파이프라인을 재구성했다.",
        "기여한 점: 6시간에서 40분으로.",
    ] + [f"왼쪽 단 내용 줄 {i} 데이터 품질 점검" for i in range(10)]
    right = ["자기소개", "협업을 중시합니다."] + [
        f"오른쪽 단 내용 줄 {i} 고객 문제 해결" for i in range(10)
    ]
    pdf = _pdf()
    for i, line in enumerate(left):
        pdf.text(x=40, y=60 + 16 * i, text=line)
    for i, line in enumerate(right):
        pdf.text(x=320, y=60 + 16 * i, text=line)
    pdf.output(str(path))
    return path


def scanned_pdf(path: Path) -> Path:
    """텍스트층이 없는 이미지 PDF (OCR 경로)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1654, 1000), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 44, index=FONT_KR_INDEX)
    for i, line in enumerate(
        ["경력", "문제 정의: 주문 로그 집계가 느렸다", "기여한 점: 집계 시간 단축"]
    ):
        draw.text((120, 120 + 90 * i), line, fill="black", font=font)
    img.save(path, "PDF", resolution=200.0)
    return path


def docx_file(path: Path) -> Path:
    import docx

    d = docx.Document()
    d.add_paragraph("가상 데이터 — 실존 인물 아님")
    d.add_paragraph("경력")
    d.add_paragraph("가상데이터랩 데이터 엔지니어 2023.01 ~ 2024.12")
    d.add_paragraph("[문제 정의] 주문 로그 집계가 하루 6시간 걸렸다.")
    d.add_paragraph("[과정] Airflow DAG 를 재구성했다.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "기여한 점", "집계 시간을 40분으로 줄였다."
    t.cell(1, 0).text, t.cell(1, 1).text = "사용 기술", "Airflow, Spark"
    d.save(str(path))
    return path


_HWPX_SECTION = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
{paras}
</hs:sec>"""


def hwpx_file(path: Path, lines: list[str] | None = None) -> Path:
    lines = lines or [
        "가상 데이터 — 실존 인물 아님",
        "경력",
        "가상데이터랩 2023.01 ~ 2024.12",
        "■ 기여한 점",
        "집계 시간을 40분으로 줄였다.",
    ]
    paras = "\n".join(f"<hp:p><hp:run><hp:t>{ln}</hp:t></hp:run></hp:p>" for ln in lines)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("Contents/section0.xml", _HWPX_SECTION.format(paras=paras))
    return path
