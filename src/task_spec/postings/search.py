"""공고 찾기 — 회사·직무명·id 로 (GET /postings?q=). 온톨로지 공고와 임시 공고를 같은 모양으로 돌려준다.

검색어는 공백으로 나눠 모두 들어 있는 공고만 (AND). LIKE 특수문자(% _ \\)는 글자 그대로 찾게 이스케이프한다.
검색어가 직군 이름(예: "백엔드·풀스택")에 들어 있으면 그 직군 공고도 맞는 것으로 본다 — 직무명이 영어("Backend")여도
"백엔드" 로 찾게.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from task_spec.schemas import CareerLevel, CareerSource

MAX_TERMS = 5


def like_patterns(q: str) -> list[str]:
    """'네이버  백엔드' → ['%네이버%', '%백엔드%'] (ESCAPE '\\' 로 쓴다)."""
    terms = [t for t in q.split() if t][:MAX_TERMS]
    out = []
    for t in terms:
        esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        out.append(f"%{esc}%")
    return out


COMPANY_NOISE = r"\(주\)|㈜|주식회사|\(유\)|유한회사|\s"  # postings/company.company_id 와 같은 규칙 (SQL regexp_replace 용)


@dataclass(frozen=True)
class CompanyFilter:
    """한 회사 공고만 (GET /postings?company=). 사람이 정한 회사(posting_company)가 공고 데이터보다 먼저다 —
    include 는 정한 회사가 이 회사인 공고, exclude 는 다른 회사로 정한 공고."""

    key: str  # company_id 값
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def sql(self, company_col: str = "company", id_col: str = "id") -> tuple[str, list[object]]:
        clause = (
            f"((lower(regexp_replace(coalesce({company_col}, ''), %s, '', 'g')) = %s OR {id_col} = ANY(%s))"
            f" AND NOT {id_col} = ANY(%s))"
        )
        return clause, [COMPANY_NOISE, self.key, list(self.include), list(self.exclude)]


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
    # 신입·경력 (postings/career.py) — 사람이 정한 값(confirmed)이 먼저, 없으면 공고 문구 추정(posting_text), 둘 다 없으면 null
    career_level: CareerLevel | None = None
    career_level_source: CareerSource | None = None
