"""공고 id → 회사 (실행 목록을 회사로 묶고 거르기 — GET /runs).

회사 id 는 회사 표기에서 공백·법인 표기((주)·㈜·주식회사 등)를 빼고 소문자로 맞춘 값이다. 온톨로지에 없는 새 회사(임시 공고)도
같은 규칙으로 id 가 생기고, 나중에 온톨로지에 같은 회사가 들어와도 id 가 바뀌지 않는다. 표시 이름은 온톨로지 `company.name`
(기본 키)과 id 가 같으면 그 표기, 아니면 공고에 적힌 표기. `known` = 온톨로지 company 표에 있는 회사인지.

사람이 정한 회사(task_db `posting_company`, PUT /postings/{id}/company·실행 만들기 POST /runs 의 company)가 공고 데이터
(온톨로지 공고의 회사·임시 공고의 회사 칸)보다 먼저다 [확정: 2026-11-19 사용자 — 인증 전에는 입력한 회사를 그대로 믿는다].
회사 결정적 사건을 고를 때(spec/company_incidents.py)와 실행 목록에 같은 값을 쓴다. 인증 때 입력자와 회사를 묶는다 [미정].
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from task_spec.ontology.queries import company_names, position_companies
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.store import is_provisional

_LEGAL = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사")


CompanySource = Literal["entered", "posting"]  # 사람이 정함 / 공고 데이터


class ProvisionalLookup(Protocol):
    async def get_provisional(self, pid: str) -> dict[str, Any] | None: ...


class CompanyLookup(ProvisionalLookup, Protocol):
    async def entered_companies(self, pids: list[str]) -> dict[str, str]: ...


@dataclass(frozen=True)
class PostingCompany:
    company_id: str
    name: str
    known: bool
    source: CompanySource = "posting"


def company_id(name: str | None) -> str | None:
    """회사 표기 → id. 법인 표기·공백만 있으면 None."""
    if not name:
        return None
    key = "".join(_LEGAL.sub("", name).split()).casefold()
    return key or None


async def posting_companies(
    posting_ids: Iterable[str],
    *,
    ontology: OntologyRepository | None,
    store: ProvisionalLookup | None,
) -> dict[str, PostingCompany]:
    """회사를 알 수 없는 공고(없는 공고·회사 칸이 빈 임시 공고, 사람이 정하지도 않음)는 결과에 넣지 않는다.
    store 가 사람이 정한 회사(entered_companies)를 주면 그 값이 먼저다."""
    ids = set(posting_ids)
    names: dict[str, str] = {}
    canonical: dict[str, str] = {}
    if ontology is not None:
        canonical = {k: n for n in await company_names(ontology) if (k := company_id(n))}
        names |= await position_companies(ontology, sorted(p for p in ids if not is_provisional(p)))
    if store is not None:
        for pid in sorted(p for p in ids if is_provisional(p)):
            row = await store.get_provisional(pid)
            if row is not None and row.get("company"):
                names[pid] = str(row["company"])
    entered: dict[str, str] = {}
    lookup = getattr(
        store, "entered_companies", None
    )  # CompanyLookup — 테스트 가짜 저장소는 없을 수 있다
    if lookup is not None:
        entered = await lookup(sorted(ids))
    out: dict[str, PostingCompany] = {}
    for pid in sorted(ids):
        source: CompanySource = "entered" if pid in entered else "posting"
        name = entered.get(pid) or names.get(pid)
        if name and (cid := company_id(name)) is not None:
            out[pid] = PostingCompany(cid, canonical.get(cid, name), cid in canonical, source)
    return out
