"""공고 찾기 — 회사·직무명·id 로 (GET /postings?q=). 온톨로지 공고와 임시 공고를 같은 모양으로 돌려준다.

검색어는 공백으로 나눠 모두 들어 있는 공고만 (AND). LIKE 특수문자(% _ \\)는 글자 그대로 찾게 이스케이프한다.
검색어가 직군 이름(예: "백엔드·풀스택")에 들어 있으면 그 직군 공고도 맞는 것으로 본다 — 직무명이 영어("Backend")여도
"백엔드" 로 찾게.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

MAX_TERMS = 5


def like_patterns(q: str) -> list[str]:
    """'네이버  백엔드' → ['%네이버%', '%백엔드%'] (ESCAPE '\\' 로 쓴다)."""
    terms = [t for t in q.split() if t][:MAX_TERMS]
    out = []
    for t in terms:
        esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        out.append(f"%{esc}%")
    return out


@dataclass(frozen=True)
class SearchTerm:
    pattern: str  # LIKE 패턴 (ESCAPE '\\')
    subs: tuple[str, ...] = ()  # 이름에 이 검색어가 들어 있는 직군 id


def search_terms(q: str, sub_labels: dict[str, str] | None = None) -> list[SearchTerm]:
    raw = [t for t in q.split() if t][:MAX_TERMS]
    labels = {k: v.lower() for k, v in (sub_labels or {}).items()}
    return [
        SearchTerm(
            pattern=p, subs=tuple(sorted(s for s, lab in labels.items() if t.lower() in lab))
        )
        for t, p in zip(raw, like_patterns(q), strict=True)
    ]


class PostingHit(BaseModel):
    posting_id: str
    source: Literal["ontology", "provisional"]
    company: str | None = None
    title: str | None = None  # 직무명 (온톨로지 job / 임시 공고 title)
    position_name: str | None = None  # 온톨로지 원 표기 ("회사 / 직무 (경력)")
    sub: str | None = None
    industry_id: str | None = None
    in_pilot: bool = False
