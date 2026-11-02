"""줄 목록 → 절·머리말·칸 구조 + ⟦칸⟧ 표시 본문.

- 절 제목: 짧은 줄이 제목 사전과 같으면 (경력·학력·프로젝트·교육·자기소개·기술·기타)
- 머리말: 기간(2023.01 ~ 2024.12, ~ 현재)이 있는 줄 — 회사·프로젝트 줄
- 칸: '문제 정의:' '[과정]' '■ 기여한 점' 처럼 라벨 + 구분자(괄호·콜론·대시) 또는 라벨만 있는 줄.
  라벨로 시작하는 일반 문장("과정에서 …")은 칸으로 보지 않는다.
- 본문 표시: 칸 라벨 줄 → `⟦정규 라벨⟧ 나머지`. 가명화·인용 대조가 이 형식을 안다 (llm/quotes.py).
"""

from __future__ import annotations

import re

from task_spec.schemas import ExtractedField, ExtractedSection, SectionHeader

_SECTION_TITLES = {
    "경력": "경력",
    "경력사항": "경력",
    "주요경력": "경력",
    "경력기술서": "경력",
    "workexperience": "경력",
    "experience": "경력",
    "학력": "학력",
    "학력사항": "학력",
    "education": "학력",
    "프로젝트": "프로젝트",
    "주요프로젝트": "프로젝트",
    "프로젝트경험": "프로젝트",
    "projects": "프로젝트",
    "교육": "교육",
    "교육사항": "교육",
    "교육이수": "교육",
    "자기소개": "자기소개",
    "자기소개서": "자기소개",
    "지원동기": "자기소개",
    "기술": "기술",
    "기술스택": "기술",
    "보유기술": "기술",
    "skills": "기술",
    "수상": "기타",
    "수상경력": "기타",
    "자격증": "기타",
    "활동": "기타",
}

_LABELS = {
    "문제정의": "문제 정의",
    "문제": "문제 정의",
    "배경": "문제 정의",
    "과제": "문제 정의",
    "과정": "과정",
    "수행내용": "과정",
    "진행과정": "과정",
    "해결과정": "과정",
    "기여한점": "기여한 점",
    "기여": "기여한 점",
    "기여도": "기여한 점",
    "담당업무": "담당업무",
    "담당": "담당업무",
    "역할": "담당업무",
    "성과": "성과",
    "결과": "성과",
    "사용기술": "사용 기술",
    "기술스택": "사용 기술",
    "사용도구": "사용 기술",
}
_LABEL_ALT = "|".join(
    sorted((r"\s?".join(map(re.escape, k)) for k in _LABELS), key=len, reverse=True)
)
_BULLET = r"[-•·*▶▷■□◆◇○●ㆍ]"
_FIELD_RX = re.compile(
    # 글머리 자리: 알려진 기호 + OCR 이 바꿔 읽은 기호(『 「 » 등) — 문자·숫자가 아닌 기호 한 개
    rf"^\s*(?:(?:{_BULLET}|[^\w\s\[【(<])\s*)?"
    rf"(?:[\[【(<]\s*(?P<l1>{_LABEL_ALT})\s*[\]】)>]\s*[:：\-–]?"
    rf"|(?P<l2>{_LABEL_ALT})\s*(?:[:：\-–|]|$)"
    # 상자형 글머리(■ ▶ ◆) 뒤에서는 공백만으로도 라벨로 본다 ("■ 배경 주문 로그…"). '-'·'•' 는 일반 문장이 많아 제외
    rf"|(?<=[■□▶▷◆◇])\s*(?P<l3>{_LABEL_ALT})\s+(?=\S))"
    rf"\s*(?P<rest>.*)$"
)
_PERIOD = re.compile(
    r"(?:19|20)\d{2}\s?[.\-/년]\s?\d{1,2}(?:\s?[.\-/월]\s?\d{0,2})?\.?\s*[~–—\-]\s*"
    r"(?:(?:19|20)\d{2}\s?[.\-/년]\s?\d{1,2}(?:\s?[.\-/월]\s?\d{0,2})?\.?|\d{1,2}\b|현재|재직\s?중|present|now)",
    re.IGNORECASE,
)
_TITLE_STRIP = re.compile(r"[\s\[\]【】<>()#■□▶▷◆◇●○:：\-–·ㆍ*|]")


def _section_kind(line: str) -> str | None:
    if len(line) > 20:
        return None
    return _SECTION_TITLES.get(_TITLE_STRIP.sub("", line).casefold())


def structure(lines: list[str]) -> tuple[str, list[ExtractedSection]]:
    out: list[str] = []
    sections: list[dict] = []
    cur: dict | None = None
    field: dict | None = None

    def ensure_section() -> dict:
        nonlocal cur
        if cur is None:
            cur = {"kind": "기타", "title": None, "headers": [], "fields": []}
            sections.append(cur)
        return cur

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            out.append("")
            field = None
            continue
        kind = _section_kind(stripped)
        if kind is not None:
            cur = {"kind": kind, "title": stripped, "headers": [], "fields": []}
            sections.append(cur)
            field = None
            out.append(stripped)
            continue
        m = _FIELD_RX.match(stripped)
        if m:
            label = _LABELS[re.sub(r"\s", "", m.group("l1") or m.group("l2") or m.group("l3"))]
            rest = m.group("rest").strip()
            field = {"label": label, "lines": [rest] if rest else []}
            ensure_section()["fields"].append(field)
            out.append(f"⟦{label}⟧ {rest}".rstrip())
            continue
        period = _PERIOD.search(stripped)
        if period:
            ensure_section()["headers"].append(
                {"line": stripped, "period": period.group(0).strip()}
            )
            field = None
            out.append(stripped)
            continue
        if field is not None:
            field["lines"].append(stripped)
        out.append(stripped)

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"
    result = [
        ExtractedSection(
            kind=s["kind"],
            title=s["title"],
            headers=[SectionHeader(**h) for h in s["headers"]],
            fields=[
                ExtractedField(label=f["label"], text="\n".join(f["lines"])) for f in s["fields"]
            ],
        )
        for s in sections
    ]
    return text, result
