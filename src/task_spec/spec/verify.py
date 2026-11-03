"""경험 검증 — 검증할 주장 고르기·난이도·원문 베끼기 검사 [확정: 2026-11-03].

지원자마다 확장 문항을 LLM 으로 서술하던 방식(verify_task)은 폐기했다 — 공고 단위 확장 슬롯(spec/slots.py)에
주장을 배정(spec/assign.py)하고 면접 질문만 지원자마다 만든다(spec/questions.py). 서면 확장 문항은 AI 단독 풀이 테스트에서
경험 없는 AI 도 22개 모두 수준 3 이상을 받아 경험을 가리지 못했다.

주장 고르기 [확정 — 방식 B]: compare 의 claims_to_verify(점수 순)에서 공통 과제 역량과 겹치면 W_COMMON_OVERLAP 만큼 깎고
(빼지 않음), 고른 주장끼리 역량이 겹치지 않게 최대 MAX_ITEMS.
"""

from __future__ import annotations

import re

from task_spec.nodes.pseudonymize.detectors import TOKEN_RE
from task_spec.schemas import VerifyTarget
from task_spec.spec.common import DIFFICULTY, LEVEL_RANK
from task_spec.spec.draft import _norm

MAX_ITEMS = 2
W_COMMON_OVERLAP = 1.5
_NUM = re.compile(r"\d{2,}")
COPY_SPAN = 12  # 인용과 이만큼(공백 제외 글자) 겹치면 베끼기


def pick_targets(targets: list[VerifyTarget], common_ids: set[str]) -> list[VerifyTarget]:
    """공통 과제 역량과 겹치면 감점(빼지 않음), 고른 주장끼리 역량이 겹치지 않게 최대 MAX_ITEMS."""

    def adjusted(t: VerifyTarget) -> float:
        return t.score - (W_COMMON_OVERLAP if set(t.competency_ids) & common_ids else 0.0)

    chosen: list[VerifyTarget] = []
    used: set[str] = set()
    for t in sorted(targets, key=lambda t: (-adjusted(t), t.claim_id)):
        if set(t.competency_ids) & used:
            continue
        chosen.append(t)
        used |= set(t.competency_ids)
        if len(chosen) == MAX_ITEMS:
            break
    return chosen


def item_difficulty(levels: list[str]) -> str:
    rank = max((LEVEL_RANK.get(lv, 0) for lv in levels), default=-1)
    return DIFFICULTY.get(rank, "medium" if rank < 0 else "low")


def copied(text: str, quote: str) -> list[str]:
    """지원자 원문 베끼기: 가명화 토큰, 인용의 2자리 이상 수치, 인용과 COPY_SPAN 글자 이상 겹침."""
    out: list[str] = []
    if TOKEN_RE.search(text):
        out.append("가명화 토큰")
    nums = sorted({n for n in _NUM.findall(quote) if n in text})
    if nums:
        out.append("인용의 수치 " + ", ".join(nums))
    q, t = _norm(quote), _norm(text)
    if any(q[i : i + COPY_SPAN] in t for i in range(0, max(0, len(q) - COPY_SPAN + 1))):
        out.append(f"인용과 {COPY_SPAN}자 이상 같은 문장")
    return out
