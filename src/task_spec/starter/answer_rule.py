"""진행자 답 규칙 — 질문 하나에 승인된 문장 하나를 고른다 (docs/design/analysis-stage.md §4.6, §9.1 판 3).

진행자는 프로그램이다 [확정: 2026-11-13 사용자]. 같은 질문에는 늘 같은 답이 나가야 한다 (같은 공고 지원자 간 비교, 재현성).
규칙은 둘이다:

- `answer` (§4.6): 요청자 지식(`f#`)과 추가 요구(`c#`). 지원자마다 항목 상태(없음 → 모호 → 열림)를 두고,
  위에서부터 처음 맞는 규칙 하나를 적용한다. 몰아넣은 질문은 "한 가지씩"으로 거절한다.
- `answer_legacy` (§9.1 판 3 "지금 규칙 모드"): 지금 `ANSWER_RULE`(starter/facilitator.py)을 정해진 규칙으로 바꿔 적은 것.
  추가 요구(`c#`)만 넘긴다. 상태·"한 가지씩" 거절이 없다. 질문·자료 한도는 부르는 쪽이 센다 — 모든 질문(정보 없음 포함)이 한도에 든다.

낱말 맞추기는 지금 `match_terms` 정리 규칙과 같다 (`spec.feature.find_terms` — 한글은 띄어쓰기를 무시한 부분 문자열이라 조사가
붙어도 걸리고, 영문은 대소문자를 무시하고 단어 경계로 본다). 주제(`topic`) 낱말은 일반어(`too_general`)를 뺀다.

순수 함수다 — 입출력·시각·기록 없음. 기록은 부르는 쪽(진행자 CLI) 몫.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, Field

from task_spec.spec.feature import find_terms, too_general
from task_spec.starter.facilitator import Answer

State = Literal["none", "hint", "open"]  # 없음 · 모호(answer_hint 를 받음) · 열림(answer 를 받음)
Layer = Literal["answer", "hint", "one_at_a_time", "none"]

ONE_AT_A_TIME = "한 가지씩 물어봐 주세요."
NO_INFO = "추가로 드릴 정보가 없습니다."

_WORD = re.compile(r"[0-9A-Za-z가-힣]+")
_ID = re.compile(r"([a-z]+)(\d+)")
_KIND_ORDER = {"f": 0, "c": 1}


class Reply(BaseModel):
    """질문 하나에 대한 진행자 답과 그 근거."""

    text: str
    rule: int  # 적용한 규칙 번호 — §4.6 은 1~5, 지금 규칙 모드는 1~3
    layer: Layer
    item_id: str | None = None
    hit: float | None = (
        None  # 적중 1 · 0.5 · 0. 반복이거나 지금 규칙 모드면 None (적중률 분모에서 뺀다)
    )
    repeat: bool = False  # 이미 받은 층의 답을 다시 줌
    candidates: list[str] = Field(default_factory=list)  # 결정에 걸린 항목 (규칙 1·3·4)
    states: dict[str, State] = Field(
        default_factory=dict
    )  # 이 답 뒤의 항목 상태 (지금 규칙 모드는 비어 있음)


def item_order(item_id: str) -> tuple[int, int, str]:
    """같으면 id 순, `f` 먼저 (§4.6) — f1 < f2 < f10 < c1."""
    m = _ID.fullmatch(item_id)
    if m is None:
        return (len(_KIND_ORDER), 0, item_id)
    kind, num = m.groups()
    return (_KIND_ORDER.get(kind, len(_KIND_ORDER)), int(num), item_id)


def topic_words(topic: str) -> list[str]:
    """주제 낱말 — 일반어(띄어쓰기 없는 두 글자 이하, `too_general`)는 뺀다."""
    return [w for w in dict.fromkeys(_WORD.findall(topic)) if not too_general(w)]


def term_hits(item: Answer, question: str) -> int:
    """질문에 걸린 `match_terms` 수."""
    return len(find_terms(item.match_terms, question))


def topic_hits(item: Answer, question: str) -> int:
    """질문과 겹친 주제 낱말 수."""
    return len(find_terms(topic_words(item.topic), question))


def _best(ids: Iterable[str], counts: Mapping[str, int]) -> str:
    """가장 많이 맞은 항목, 같으면 id 순."""
    return min(ids, key=lambda k: (-counts[k], item_order(k)))


def _sorted(ids: Iterable[str]) -> list[str]:
    return sorted(ids, key=item_order)


def answer(question: str, items: Sequence[Answer], states: Mapping[str, State]) -> Reply:
    """§4.6 답 규칙. items = 요청자 지식(f#)과 추가 요구(c#), states = 이 지원자의 항목 상태 (없는 항목은 '없음')."""
    by_id = {i.id: i for i in items}
    state: dict[str, State] = {i.id: states.get(i.id, "none") for i in items}
    hits = {k: term_hits(i, question) for k, i in by_id.items()}
    closed = [k for k, n in hits.items() if n and state[k] != "open"]

    if len(closed) >= 2:  # 1. 안 열린 항목 둘 이상에 걸림 — 넓은 질문
        return Reply(
            text=ONE_AT_A_TIME,
            rule=1,
            layer="one_at_a_time",
            hit=0.0,
            candidates=_sorted(closed),
            states=state,
        )
    if closed:  # 2. 안 열린 항목 하나에만 걸림 — 구체 답, 열림
        k = closed[0]
        return Reply(
            text=by_id[k].answer,
            rule=2,
            layer="answer",
            item_id=k,
            hit=1.0,
            candidates=[k],
            states={**state, k: "open"},
        )
    opened = [k for k, n in hits.items() if n]
    if opened:  # 3. 열린 항목에만 걸림 — 같은 구체 답을 다시
        k = _best(opened, hits)
        return Reply(
            text=by_id[k].answer,
            rule=3,
            layer="answer",
            item_id=k,
            repeat=True,
            candidates=_sorted(opened),
            states=state,
        )
    overlap = {k: topic_hits(i, question) for k, i in by_id.items() if state[k] != "open"}
    touched = [k for k, n in overlap.items() if n]
    if touched:  # 4. 낱말은 안 걸리고 주제만 겹침 — 모호한 답
        k = _best(touched, overlap)
        item = by_id[k]
        if state[k] == "hint":
            return Reply(
                text=item.answer_hint or item.answer,
                rule=4,
                layer="hint",
                item_id=k,
                repeat=True,
                candidates=_sorted(touched),
                states=state,
            )
        layer: Layer = "hint" if item.answer_hint is not None else "answer"
        return Reply(
            text=item.answer_hint if item.answer_hint is not None else item.answer,
            rule=4,
            layer=layer,
            item_id=k,
            hit=0.5 if len(touched) == 1 else 0.0,  # 주제 낱말 몰아넣기 방지
            candidates=_sorted(touched),
            states={**state, k: "hint" if layer == "hint" else "open"},
        )
    return Reply(text=NO_INFO, rule=5, layer="none", hit=0.0, states=state)  # 5


def answer_legacy(question: str, items: Sequence[Answer]) -> Reply:
    """§9.1 판 3 지금 규칙 모드. items = 추가 요구(c#)만. 같은 것을 다시 물으면 같은 답 (반복 표시 없음)."""
    by_id = {i.id: i for i in items}
    hits = {k: term_hits(i, question) for k, i in by_id.items()}
    matched = [k for k, n in hits.items() if n]
    if matched:  # 1. 낱말이 걸림 — 가장 많이 맞은 하나의 구체 답
        k = _best(matched, hits)
        return Reply(
            text=by_id[k].answer, rule=1, layer="answer", item_id=k, candidates=_sorted(matched)
        )
    overlap = {k: topic_hits(i, question) for k, i in by_id.items()}
    touched = [k for k, n in overlap.items() if n]
    if touched:  # 2. 주제만 겹침 — 가장 많이 겹친 하나의 모호한 답
        k = _best(touched, overlap)
        item = by_id[k]
        hint = item.answer_hint is not None
        return Reply(
            text=item.answer_hint if item.answer_hint is not None else item.answer,
            rule=2,
            layer="hint" if hint else "answer",
            item_id=k,
            candidates=_sorted(touched),
        )
    return Reply(text=NO_INFO, rule=3, layer="none")  # 3


def hit_rate(replies: Iterable[Reply]) -> float | None:
    """질문 적중률 — 반복(hit=None)은 분모에서 뺀다. 셀 질문이 없으면 None."""
    scored = [r.hit for r in replies if r.hit is not None]
    return sum(scored) / len(scored) if scored else None
