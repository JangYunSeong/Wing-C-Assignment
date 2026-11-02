"""추출 평가용 가상 지원서 생성 (시드 고정, 파일은 평가 때마다 임시 폴더에 다시 만든다).

정답(gold)은 생성기가 안다: 칸 표시 본문(⟦라벨⟧), 칸 라벨 순서, 머리말 기간, 2단 블록 순서.
칸 라벨은 여러 표기(문제 정의: / [문제정의] / ■ 배경 / 담당 업무 - …)로 쓴다.
"""

from __future__ import annotations

import random
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

FONT = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
FONT_KR_INDEX = 1
LAYOUTS = [
    "pdf_basic",
    "pdf_right_date",
    "pdf_two_col",
    "docx_para",
    "docx_table",
    "hwpx",
    "pdf_scan",
]

COMPANIES = ["가상데이터랩", "누리솔루션", "한빛가상", "다온테크", "가람소프트"]
ROLES = ["데이터 엔지니어", "백엔드 개발자", "ML 엔지니어", "데이터 분석가"]
SURFACES = {
    "문제 정의": ["문제 정의: {}", "[문제정의] {}", "■ 배경 {}", "- 문제: {}"],
    "과정": ["과정: {}", "[과정] {}", "▶ 수행 내용: {}", "- 해결 과정 - {}"],
    "기여한 점": ["기여한 점: {}", "[기여도] {}", "■ 기여: {}", "- 기여한 점 : {}"],
    "담당업무": ["담당업무: {}", "[역할] {}", "▶ 담당 업무 - {}"],
    "성과": ["성과: {}", "[결과] {}"],
}
SENTENCES = {
    "문제 정의": [
        "주문 로그 집계가 하루 {a}시간 걸렸다.",
        "추천 클릭률이 {a}%에 머물렀다.",
        "배치 실패가 주 {a}회 발생했다.",
    ],
    "과정": [
        "Airflow DAG 를 재구성했다.",
        "Spark 집계를 파티션 단위로 나눴다.",
        "피처 저장소를 도입했다.",
    ],
    "기여한 점": [
        "집계 시간을 {a}시간에서 {b}분으로 줄였다.",
        "클릭률을 {a}% 올렸다.",
        "장애를 {a}건 줄였다.",
    ],
    "담당업무": ["데이터 파이프라인 설계와 운영", "추천 모델 학습과 배포"],
    "성과": ["월 비용 {a}% 절감", "처리량 {a}배 향상"],
}
INTRO = [f"오른쪽 단 자기소개 문장 {i}: 협업과 데이터 품질을 중시합니다." for i in range(12)]


@dataclass
class Gold:
    id: str
    layout: str
    lines: list[str]  # 문서에 쓸 원문 줄 (라벨 표기 포함)
    expected_text: list[str]  # 칸 표시(⟦⟧) 정답 줄
    labels: list[str]
    periods: list[str]
    right_block: list[str] = field(default_factory=list)  # 2단: 오른쪽 단 줄
    right_aligned: dict[int, str] = field(default_factory=dict)  # 줄 번호 → 오른쪽 정렬할 기간


def _period(r: random.Random) -> str:
    y = r.randint(2016, 2022)
    end = r.choice([f"{y + r.randint(1, 3)}.{r.randint(1, 12):02d}", "현재"])
    return f"{y}.{r.randint(1, 12):02d} ~ {end}"


def build(r: random.Random, idx: int, split: str, layout: str) -> Gold:
    lines = ["가상 데이터 — 실존 인물 아님", "경력"]
    expected = list(lines)
    labels: list[str] = []
    periods: list[str] = []
    right_aligned: dict[int, str] = {}
    for _ in range(r.randint(1, 2)):
        period = _period(r)
        periods.append(period)
        head = f"{r.choice(COMPANIES)} {r.choice(ROLES)}"
        if layout == "pdf_right_date":
            right_aligned[len(lines)] = period
            lines.append(head)
            expected.append(f"{head} {period}")
        else:
            lines.append(f"{head} {period}")
            expected.append(f"{head} {period}")
        for label in r.sample(list(SURFACES), r.randint(2, 4)):
            body = r.choice(SENTENCES[label]).format(a=r.randint(2, 30), b=r.randint(10, 50))
            surface = r.choice(SURFACES[label])
            if layout == "docx_table":  # 표: 왼쪽 칸 = 라벨 표기, 오른쪽 칸 = 내용
                label_text = " ".join(re.sub(r"[\[\]■▶:{}]|(?<!\S)-|-(?!\S)", " ", surface).split())
                surface = label_text + " | {}"
            lines.append(surface.format(body))
            expected.append(f"⟦{label}⟧ {body}")
            labels.append(label)
    right = INTRO[: r.randint(8, 12)] if layout == "pdf_two_col" else []
    return Gold(
        f"ing-{split}-{idx:03d}", layout, lines, expected, labels, periods, right, right_aligned
    )


# ------------------------------------------------------------------ 렌더러


def _pdf():  # type: ignore[no-untyped-def]
    from fpdf import FPDF

    pdf = FPDF(unit="pt", format="A4")
    pdf.add_font("noto", fname=FONT, collection_font_number=FONT_KR_INDEX)
    pdf.set_font("noto", size=10)
    pdf.add_page()
    return pdf


def render(g: Gold, out_dir: Path) -> Path:
    if g.layout in ("pdf_basic", "pdf_right_date", "pdf_two_col"):
        pdf = _pdf()
        x_left = 40 if g.layout == "pdf_two_col" else 60
        for i, line in enumerate(g.lines):
            pdf.text(x=x_left, y=60 + 15 * i, text=line)
            if i in g.right_aligned:
                p = g.right_aligned[i]
                pdf.text(x=595 - 50 - pdf.get_string_width(p), y=60 + 15 * i, text=p)
        for i, line in enumerate(g.right_block):
            pdf.text(x=320, y=60 + 15 * i, text=line[:24])
        path = out_dir / f"{g.id}.pdf"
        pdf.output(str(path))
        return path
    if g.layout == "pdf_scan":
        from PIL import Image, ImageDraw, ImageFont

        img = Image.new("RGB", (1654, 2339), "white")
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(FONT, 34, index=FONT_KR_INDEX)
        for i, line in enumerate(g.lines):
            draw.text((140, 160 + 62 * i), line, fill="black", font=font)
        path = out_dir / f"{g.id}.pdf"
        img.save(path, "PDF", resolution=200.0)
        return path
    if g.layout in ("docx_para", "docx_table"):
        import docx

        d = docx.Document()
        rows = []
        for line in g.lines:
            if g.layout == "docx_table" and " | " in line:
                rows.append(line.split(" | ", 1))
                continue
            if rows:
                t = d.add_table(rows=len(rows), cols=2)
                for ri, (a, b) in enumerate(rows):
                    t.cell(ri, 0).text, t.cell(ri, 1).text = a, b
                rows = []
            d.add_paragraph(line)
        if rows:
            t = d.add_table(rows=len(rows), cols=2)
            for ri, (a, b) in enumerate(rows):
                t.cell(ri, 0).text, t.cell(ri, 1).text = a, b
        path = out_dir / f"{g.id}.docx"
        d.save(str(path))
        return path
    if g.layout == "hwpx":
        paras = "\n".join(f"<hp:p><hp:run><hp:t>{ln}</hp:t></hp:run></hp:p>" for ln in g.lines)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
            f'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">{paras}</hs:sec>'
        )
        path = out_dir / f"{g.id}.hwpx"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("mimetype", "application/hwp+zip")
            zf.writestr("Contents/section0.xml", xml)
        return path
    raise ValueError(g.layout)


def samples(split: str, per_layout: int) -> list[Gold]:
    r = random.Random({"dev": 101, "test": 202}[split])
    out = []
    for layout in LAYOUTS:
        for _ in range(per_layout):
            out.append(build(r, len(out), split, layout))
    return out
