"""공고 id → 회사 (실행 목록을 회사로 묶고 거르기 — GET /runs).

회사 id 는 회사 표기에서 공백·법인 표기((주)·㈜·주식회사 등)를 빼고 소문자로 맞춘 값이다. 온톨로지에 없는 새 회사(임시 공고)도
같은 규칙으로 id 가 생기고, 나중에 온톨로지에 같은 회사가 들어와도 id 가 바뀌지 않는다. 표시 이름은 온톨로지 `company.name`
(기본 키)과 id 가 같으면 그 표기, 아니면 공고에 적힌 표기. `known` = 온톨로지 company 표에 있는 회사인지.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from task_spec.ontology.queries import company_names, position_companies
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.store import is_provisional

_LEGAL = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사")


class ProvisionalLookup(Protocol):
    async def get_provisional(self, pid: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class PostingCompany:
    company_id: str
    name: str
    known: bool


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
    """회사를 알 수 없는 공고(없는 공고·회사 칸이 빈 임시 공고)는 결과에 넣지 않는다."""
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
    out: dict[str, PostingCompany] = {}
    for pid, name in names.items():
        if (cid := company_id(name)) is not None:
            out[pid] = PostingCompany(cid, canonical.get(cid, name), cid in canonical)
    return out
