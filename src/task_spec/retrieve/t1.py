"""③ T1 후보 검색: 주장 1건 → 역량 후보 상위 k (결정적, LLM 없음).

채널
- pattern  매처 규칙(obj·act·not)을 범위와 무관하게 적용. 맞은 역량은 모두 1위.
- card     역량 카드 본문 BM25 (Kiwi 토큰)
- example  검수된 공고 예문 BM25 → 가장 잘 맞은 문장 순서로 역량 순위
- vector   (선택) 밖에서 계산한 순위를 그대로 받는다 — 효과가 확인될 때만 쓴다 (불변식 5: 재현율 보강용)
합치기: RRF(Σ 1/(k+rank)). 공고 역량은 가산점만 더한다 — 후보를 공고 역량으로 제한하지 않는다 (불변식 5).
범위(scope): hard = 지원 직군 범위 밖 역량 제외 / soft = 점수 ×SCOPE_SOFT / off = 무시.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from task_spec.postings.matcher import Matcher
from task_spec.retrieve.bm25 import BM25
from task_spec.retrieve.cards import CardSet
from task_spec.retrieve.text import tokens

Channel = Literal["pattern", "card", "example", "vector"]
Scope = Literal["hard", "soft", "off"]
RRF_K = 60
POSTING_BONUS = {
    True: 0.5,
    False: 0.25,
}  # 주(담당업무·자격요건) / 보조(우대) — RRF 1위 점수에 곱한다
EXAMPLE_LINES = 60
SCOPE_SOFT = 0.7  # 범위 밖 역량 점수 배율 (soft). 0.5 는 g15(범위 프로브)를 15위로 밀어냄, 0.7~ 은 1위·silver 동일


@dataclass
class Hit:
    competency_id: str
    score: float
    ranks: dict[str, int] = field(default_factory=dict)
    in_scope: bool = True
    posting: bool = False


def pattern_hits(matcher: Matcher, text: str) -> list[str]:
    lo = text.lower()
    return [
        c["id"]
        for c in matcher.comps
        if c["_obj"].search(lo)
        and (not c["_act"] or c["_act"].search(lo))
        and not (c["_not"] and c["_not"].search(lo))
    ]


def _ranks(ordered: Sequence[Sequence[str]]) -> dict[str, int]:
    """[[동률 묶음], ...] → 역량별 순위 (1부터, 동률은 같은 순위)."""
    out: dict[str, int] = {}
    rank = 1
    for group in ordered:
        new = [c for c in group if c not in out]
        for c in new:
            out[c] = rank
        rank += len(new) if new else 0
    return out


class T1Retriever:
    def __init__(self, cards: CardSet, matcher: Matcher) -> None:
        self.cards = cards
        self.matcher = matcher
        self._ids = list(cards.cards)
        self._card_bm25 = BM25([tokens(cards.cards[i].doc()) for i in self._ids])
        self._ex_bm25 = BM25([tokens(x.text) for x in cards.examples])

    def channel_ranks(
        self, text: str, *, exclude_positions: frozenset[str] = frozenset()
    ) -> dict[str, dict[str, int]]:
        q = tokens(text)
        card_s = self._card_bm25.scores(q)
        card = _ranks([[self._ids[i]] for i, _ in sorted(card_s.items(), key=lambda x: -x[1])])
        ex_s = self._ex_bm25.scores(q)
        lines = [
            self.cards.examples[i]
            for i, _ in sorted(ex_s.items(), key=lambda x: -x[1])
            if self.cards.examples[i].position_id not in exclude_positions
        ][:EXAMPLE_LINES]
        example = _ranks([x.comps for x in lines])
        pattern = _ranks([pattern_hits(self.matcher, text)])
        return {"pattern": pattern, "card": card, "example": example}

    def search(
        self,
        text: str,
        sub: str | None,
        *,
        channels: Sequence[Channel] = ("pattern", "card", "example"),
        scope: Scope = "hard",
        posting: Mapping[str, bool] | None = None,  # 공고 역량 id → 주 여부
        vector: Sequence[str] | None = None,  # 벡터 채널 순위 (밖에서 계산)
        k: int = 10,
        exclude_positions: frozenset[str] = frozenset(),
        precomputed: dict[str, dict[str, int]] | None = None,
    ) -> list[Hit]:
        per = precomputed or self.channel_ranks(text, exclude_positions=exclude_positions)
        if vector is not None:
            per = {**per, "vector": _ranks([[c] for c in vector])}
        hits: dict[str, Hit] = {}
        for ch in channels:
            for cid, r in per.get(ch, {}).items():
                if cid not in self.cards.cards:
                    continue
                h = hits.setdefault(cid, Hit(cid, 0.0))
                h.ranks[ch] = r
                h.score += 1.0 / (RRF_K + r)
        for cid, primary in (
            posting or {}
        ).items():  # 이미 걸린 후보에만 — 공고 역량을 새로 끼워 넣지 않는다
            if cid in hits:
                hits[cid].posting = True
                hits[cid].score += POSTING_BONUS[primary] / (RRF_K + 1)
        out = []
        for h in hits.values():
            h.in_scope = self.cards.cards[h.competency_id].in_scope(sub)
            if not h.in_scope:
                if scope == "hard":
                    continue
                if scope == "soft":
                    h.score *= SCOPE_SOFT
            out.append(h)
        out.sort(key=lambda h: (-h.score, h.competency_id))
        return out[:k]
