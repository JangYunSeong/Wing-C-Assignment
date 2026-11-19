"""공고 id → 신입·경력 (Wing 이 과제를 신입·경력으로 나눈다 — GET /postings·/builds·/runs) [확정: 2026-11-19 사용자 — 두 값, 추정값도 내보냄].

사람이 정한 값(confirmed — task_db `posting_meta`, PUT /postings/{id}/career-level·공고 확인 POST /postings)이 먼저이고, 없으면
공고 문구로 추정한다(posting_text). 온톨로지는 읽기만 하므로(불변식 4) 정한 값은 task_db 에 둔다. 과제 생성에는 쓰지 않는다 (표시·분류만).

추정은 직무명과 자격요건(required) 문장만 본다 — 담당업무('10년 이상 운영된 모놀리스 이관'·'신입 개발자 채용 운영')와 우대
('경력자 우대')는 공고의 신입·경력을 말하지 않는다. 2026-11-19 온톨로지 공고 733개(직무명 '(경력)' 31·'(신입)' 0)에 돌린 결과:
경력 196 · 신입 1 · 추정 없음 536 (시범 261개: 경력 70 · 없음 191). '[경력] … [신입]' 처럼 두 갈래인 공고 2개는 추정하지 않는다.
공고 id 는 단서가 아니다 ('LIG_junior_…' 인데 자격요건은 '경력 2년 이상').
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from task_spec.ontology.queries import position_career_texts
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.store import is_provisional
from task_spec.schemas import CareerLevel, CareerSource

_EXPERIENCED = re.compile(r"\(\s*경력\s*\)|경력\s*\d+\s*년|\d+\s*년\s*(?:이상|차)|경력직")
_NEW = re.compile(r"신입")
# 둘 다 받는다는 말 — 추정하지 않는다
_EITHER = re.compile(r"경력\s*무관")


class CareerStore(Protocol):
    async def get_provisional(self, pid: str) -> dict[str, Any] | None: ...

    async def career_levels(self, pids: list[str]) -> dict[str, CareerLevel]: ...


@dataclass(frozen=True)
class PostingCareer:
    level: CareerLevel
    source: CareerSource


def estimate_career(texts: Iterable[str]) -> CareerLevel | None:
    """직무명·자격요건 글 → 신입·경력. 둘 다 나오거나('신입/경력 3년 이상') '경력 무관' 이거나 단서가 없으면 None."""
    joined = "\n".join(texts)
    if _EITHER.search(joined):
        return None
    experienced, new = bool(_EXPERIENCED.search(joined)), bool(_NEW.search(joined))
    if experienced == new:
        return None
    return "경력" if experienced else "신입"


def provisional_career_texts(row: dict[str, Any]) -> list[str]:
    """임시 공고 저장 행 → 직무명(확인자가 적은 제목)과 자격요건 문장."""
    lines = row.get("lines") or []
    return [
        str(row.get("title") or ""),
        *(str(x.get("text") or "") for x in lines if x.get("section") == "required"),
    ]


async def posting_careers(
    posting_ids: Iterable[str],
    *,
    ontology: OntologyRepository | None,
    store: CareerStore | None,
) -> dict[str, PostingCareer]:
    """정한 값도 추정값도 없는 공고(없는 공고 포함)는 결과에 넣지 않는다."""
    ids = set(posting_ids)
    confirmed = await store.career_levels(sorted(ids)) if store is not None else {}
    out = {pid: PostingCareer(level, "confirmed") for pid, level in confirmed.items()}
    rest = ids - out.keys()
    texts: dict[str, list[str]] = {}
    if ontology is not None:
        texts |= await position_career_texts(
            ontology, sorted(p for p in rest if not is_provisional(p))
        )
    if store is not None:
        for pid in sorted(p for p in rest if is_provisional(p)):
            row = await store.get_provisional(pid)
            if row is not None:
                texts[pid] = provisional_career_texts(row)
    for pid, t in texts.items():
        if (level := estimate_career(t)) is not None:
            out[pid] = PostingCareer(level, "posting_text")
    return out
