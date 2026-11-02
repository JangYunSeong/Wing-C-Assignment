"""형식별 텍스트 추출 (블로킹 — 노드에서 asyncio.to_thread 로 부른다). 외부 호출 없음.

| 형식 | 방법 |
| .pdf  | pypdfium2 텍스트층 → 글자 좌표로 레이아웃 복원. 텍스트층이 빈 페이지만 tesseract OCR (kor+eng, 직접 호출) |
| .docx | python-docx — 본문 문단과 표를 문서 순서대로 |
| .hwpx | ZIP 안 Contents/section*.xml 의 문단 (표준 라이브러리) |
| .txt  | UTF-8 |
| .hwp  | 미지원 (unsupported_format) — 구형 바이너리, 추후 과제 |
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree  # 지원자 업로드 XML — 엔티티 확장 공격 방어

from task_spec.nodes.ingest.layout import Word, words_to_lines

OCR_LANG = "kor+eng"
OCR_DPI = (
    400  # 300dpi 는 한영 혼용 줄에서 짧은 한글 라벨([과정])을 영문으로 오인식 (2026-11-02 dev 비교)
)
# psm 6(한 블록): 여백 넓은 이력서에서 psm 3(자동 분할)은 블록을 잘못 나눠 글자가 깨진다. 대신 2단 스캔본은 단이 섞일 수 있다.
OCR_CONFIG = "--psm 6"
MIN_PAGE_CHARS = 10  # 이보다 적으면 텍스트층이 없는 페이지로 보고 OCR
ZIP_MAX_UNCOMPRESSED = 50 * 1024 * 1024  # HWPX·DOCX 압축 해제 합계 상한
ZIP_MAX_ENTRIES = 2000
HWPX_MAX_SECTION = 10 * 1024 * 1024  # 섹션 XML 하나 상한 (통째로 메모리에 올라간다)
MAX_PAGES = 30
MAX_PAGE_PT = 2000.0  # 이보다 큰 페이지는 400dpi 렌더링 시 메모리 폭증 — OCR 거부
OCR_TIMEOUT_S = 60


class UnsupportedFormat(Exception):
    pass


class OcrError(Exception):
    pass


@dataclass
class RawExtraction:
    lines: list[str]
    method: str
    pages: int = 0
    warnings: list[str] = field(default_factory=list)


def _page_words(textpage: object, page_height: float) -> list[Word]:
    """글자 상자 → 단어. 공백·줄바꿈이거나 글자 간격·높이가 크게 바뀌면 단어를 끊는다."""
    words: list[Word] = []
    buf: list[str] = []
    x0 = x1 = top = size = 0.0

    def flush() -> None:
        if buf:
            words.append(Word("".join(buf), x0, x1, top, size))
            buf.clear()

    for i in range(textpage.count_chars()):  # type: ignore[attr-defined]
        ch = textpage.get_text_range(i, 1)  # type: ignore[attr-defined]
        if not ch or ch.isspace():
            flush()
            continue
        # loose=True: 폰트 전체 높이 상자 — 문장부호(. ~ —)도 같은 줄 높이를 받는다
        left, bottom, right, ctop = textpage.get_charbox(i, loose=True)  # type: ignore[attr-defined]
        h = max(ctop - bottom, 1.0)
        t = page_height - ctop
        if buf and (abs(t - top) > 0.5 * max(h, size) or left - x1 > 0.6 * max(h, size)):
            flush()
        if not buf:
            x0, top, size = left, t, h
        buf.append(ch)
        x1 = right
        size = max(size, h)
    flush()
    return words


def extract_pdf(path: Path, work_dir: Path | None = None) -> RawExtraction:
    """pypdfium2(PDFium). pdfminer 계열은 한글 PDF 에 흔한 CFF CJK 폰트에서 글자를 0개 뽑는 경우가 있어 쓰지 않는다."""
    import pypdfium2 as pdfium

    lines: list[str] = []
    warnings: list[str] = []
    ocr_pages = 0
    pdf = pdfium.PdfDocument(path)
    try:
        pages = len(pdf)
        if pages > MAX_PAGES:
            raise UnsupportedFormat("too_many_pages")
        for i in range(pages):
            page = pdf[i]
            width, height = page.get_size()
            words = _page_words(page.get_textpage(), height)
            if sum(len(w.text) for w in words) < MIN_PAGE_CHARS:
                lines += _ocr_page(page, work_dir)
                ocr_pages += 1
                continue
            page_lines, cols = words_to_lines(words, float(width))
            if cols > 1:
                warnings.append(f"page{i + 1}:two_columns")
            lines += page_lines
    finally:
        pdf.close()
    if ocr_pages:
        warnings.append(f"ocr_pages={ocr_pages}")
    method = "ocr" if pages and ocr_pages == pages else "text_layer"
    return RawExtraction(lines, method, pages, warnings)


def _ocr_page(page: object, work_dir: Path | None) -> list[str]:
    """tesseract 를 직접 부른다 (pytesseract 는 /tmp 에 원문 이미지를 남길 수 있어 쓰지 않는다).

    입력 이미지는 비공개 작업 폴더(700)에 600 으로 쓰고 끝나면 지운다. 결과는 stdout 으로 받는다.
    """
    if work_dir is None:
        raise OcrError("no_work_dir")
    width, height = page.get_size()  # type: ignore[attr-defined]
    if max(width, height) > MAX_PAGE_PT:
        raise OcrError("page_too_large")
    work_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(work_dir, 0o700)
    img_path = work_dir / f"ocr-{uuid.uuid4().hex}.png"
    try:
        image = page.render(scale=OCR_DPI / 72).to_pil().convert("L")  # type: ignore[attr-defined]
        fd = os.open(img_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            image.save(f, format="PNG")
        proc = subprocess.run(
            ["tesseract", str(img_path), "stdout", "-l", OCR_LANG, *OCR_CONFIG.split()],
            capture_output=True,
            timeout=OCR_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # 미설치·시간 초과
        raise OcrError(type(exc).__name__) from None
    finally:
        img_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise OcrError(f"exit_{proc.returncode}")  # stderr 는 담지 않는다
    text = proc.stdout.decode("utf-8", errors="replace")
    return [ln for ln in (s.strip() for s in text.splitlines()) if ln]


def _check_zip(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > ZIP_MAX_ENTRIES or sum(i.file_size for i in infos) > ZIP_MAX_UNCOMPRESSED:
        raise UnsupportedFormat("zip_too_large")


def extract_docx(path: Path, work_dir: Path | None = None) -> RawExtraction:
    import docx

    with zipfile.ZipFile(path) as zf:
        _check_zip(zf)
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            lines.append(Paragraph(child, document).text)
        elif tag == "tbl":
            for row in Table(child, document).rows:
                cells: list[str] = []
                for cell in row.cells:  # 병합 셀은 같은 텍스트가 반복된다
                    t = cell.text.strip()
                    if t and (not cells or cells[-1] != t):
                        cells.append(t)
                lines.append(" | ".join(cells))
    return RawExtraction(lines, "docx", 0, [])


_SECTION_FILE = re.compile(r"^Contents/section(\d+)\.xml$")


def extract_hwpx(path: Path, work_dir: Path | None = None) -> RawExtraction:
    with zipfile.ZipFile(path) as zf:
        _check_zip(zf)
        sections = sorted(
            (int(m.group(1)), n) for n in zf.namelist() if (m := _SECTION_FILE.match(n))
        )
        if not sections:
            raise UnsupportedFormat("hwpx_no_sections")
        lines: list[str] = []
        for _, name in sections:
            if zf.getinfo(name).file_size > HWPX_MAX_SECTION:
                raise UnsupportedFormat("hwpx_section_too_large")
            root = ElementTree.fromstring(zf.read(name))
            for p in root.iter():
                if not p.tag.endswith("}p"):
                    continue
                # 문단의 직속 run/t 만 — 표 안 문단은 따로 순회되므로 중복되지 않는다
                text = "".join(
                    t.text or ""
                    for run in p
                    if run.tag.endswith("}run")
                    for t in run
                    if t.tag.endswith("}t")
                )
                lines.append(text)
    return RawExtraction(lines, "hwpx", len(sections), [])


def extract_txt(path: Path, work_dir: Path | None = None) -> RawExtraction:
    return RawExtraction(path.read_text(encoding="utf-8").splitlines(), "txt", 0, [])


EXTRACTORS = {
    ".pdf": extract_pdf,
    ".docx": extract_docx,
    ".hwpx": extract_hwpx,
    ".txt": extract_txt,
}


def extract_raw(path: Path, work_dir: Path | None = None) -> RawExtraction:
    """work_dir: OCR 임시 이미지를 둘 비공개 폴더 (없으면 스캔본은 ocr_error)."""
    fn = EXTRACTORS.get(path.suffix.lower())
    if fn is None:
        raise UnsupportedFormat(path.suffix.lower() or "none")
    return fn(path, work_dir)
