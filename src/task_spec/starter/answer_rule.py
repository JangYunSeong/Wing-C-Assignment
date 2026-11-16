"""진행자 답 규칙 — 질문 하나에 승인된 문장 하나를 고른다 (docs/design/analysis-stage.md §4.6, §9.1 판 3).

진행자는 프로그램이다 [확정: 2026-11-13 사용자]. 같은 질문에는 늘 같은 답이 나가야 한다 (같은 공고 지원자 간 비교, 재현성).
규칙은 둘이다:

- `answer` (§4.6): 요청자 지식(`f#`)과 추가 요구(`c#`). 지원자마다 항목 상태(없음 → 모호 → 열림)를 두고,
  위에서부터 처음 맞는 규칙 하나를 적용한다. 몰아넣은 질문은 "한 가지씩"으로 거절한다.
- `answer_legacy` (§9.1 판 3 "지금 규칙 모드"): 지금 `ANSWER_RULE`(starter/facilitator.py)을 정해진 규칙으로 바꿔 적은 것.
  추가 요구(`c#`)만 넘긴다. 상태·"한 가지씩" 거절이 없다. 질문·자료 한도는 부르는 쪽이 센다 — 모든 질문(정보 없음 포함)이 한도에 든다.

낱말 맞추기 — §4.6 규칙 자체다 (플랫폼이 붙일 규칙이고, 손 시범 판 1·4 가 이것을 쓴다) [확정: 2026-11-13 사용자 — #151 검토 4차]:
- 매칭 낱말의 **단어가 질문에 모두 있으면** 걸린다 — 순서·조사·사이에 낀 말과 상관없이
  ("limit 기본값" ← "limit 의 기본값이…", "정렬 순서" ← "어떤 순서로 정렬해야…"). 단어 하나는 한글이면 띄어쓰기를 무시한
  부분 문자열(조사가 붙어도 걸림), 영문이면 대소문자 무시·단어 경계.
- 주제(`topic`) 낱말은 끝 조사를 떼고, 두 글자 이하 일반어(`too_general`)는 **추가 요구(`c#`)에서만** 뺀다 — 이 필터는 함정 단서가
  일반 질문에 새지 않게 정한 것(2026-11-06)이고, 요청자 지식(`f#`)은 잘못 열려도 함정이 아니다. `f#` 은 한 글자 낱말만 뺀다.
- `answer_legacy`(판 3 비교용)만 지금 규칙 그대로 구절째 맞춘다 (`spec.feature.find_terms`).

§4.6 낱말 다듬기 [확정: 2026-11-13 사용자 — #151 손 시범 질문 목록 미달(kb005 구체 답 25%)]:
- (i) 여러 단어로 된 매칭 낱말 안의 **한 글자 단어는 무시**한다 — 한글 단어는 부분 문자열로 맞추므로 "동시 요청 수"의 "수"가
  거의 모든 질문에 걸려 락을 묻는 질문에 함정 단서(c1)가 열렸다. 낱말이 한 글자 단어뿐이면 그대로 둔다.
- (ii) 주제 낱말에서 **요청 글에 있는 말**은 뺀다 — §5.1 이 매칭 낱말에서 공개 글의 말을 빼는 것과 같은 원칙
  ("감사·기록"이 요청 글에 있어 거의 모든 질문에 걸렸다).
- (iii) **두 항목 이상의 주제에 함께 나오는 말**도 뺀다 — 항목을 가르지 못하고 동점이면 id 순으로 엉뚱한 항목이 열린다.
  (ii)(iii) 은 `answer` 의 규칙 4 에만 — 판 3 지금 규칙 모드는 그대로.
- (iv) 규칙 4 에서 **모호한 답이 없는 요청자 지식(f#)은 후보가 아니다** — 주제 겹침으로는 사실을 통째로 열지 않는다
  [확정: 2026-11-13 사용자 — 확인용 질문 목록 2차 미달(구체 답 71%) 뒤 §4.6 재검토 A].

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
_HANGUL = re.compile(r"[가-힣]")
# 주제 낱말 끝에서 떼는 조사 — 긴 것부터 (하나만)
_PARTICLES = (
    "에서",
    "에게",
    "으로",
    "까지",
    "부터",
    "과",
    "와",
    "의",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "로",
    "도",
    "만",
)
_ID = re.compile(r"([a-z]+)(\d+)")
_KIND_ORDER = {"f": 0, "c": 1}


class Reply(BaseModel):
    """질문 하나에 대한 진행자 답과 그 근거."""

    text: str
    rule: int  # 적용한 규칙 번호 — §4.6 은 1~5, 지금 규칙 모드는 1~3
    layer: Layer
    item_id: str | None = None
    # 답을 준 항목이 여럿일 때 전부 (콕 집어 함께 물은 항목 — 진행자 LLM 판 규칙 1, eval/pilot/llm_facilitator.py). 하나면 비워 둔다
    item_ids: list[str] = Field(default_factory=list)
    hit: float | None = (
        None  # 적중 1 · 0.5 · 0. 반복이거나 지금 규칙 모드면 None (적중률 분모에서 뺀다)
    )
    repeat: bool = False  # 이미 받은 층의 답을 다시 줌
    candidates: list[str] = Field(default_factory=list)  # 결정에 걸린 항목 (규칙 1·3·4)
    states: dict[str, State] = Field(
        default_factory=dict
    )  # 이 답 뒤의 항목 상태 (지금 규칙 모드는 비어 있음)

    def answered(self) -> list[str]:
        """답(구체·모호)을 준 항목 — 여럿을 함께 답했으면 전부."""
        return self.item_ids or ([self.item_id] if self.item_id else [])


def item_order(item_id: str) -> tuple[int, int, str]:
    """같으면 id 순, `f` 먼저 (§4.6) — f1 < f2 < f10 < c1."""
    m = _ID.fullmatch(item_id)
    if m is None:
        return (len(_KIND_ORDER), 0, item_id)
    kind, num = m.groups()
    return (_KIND_ORDER.get(kind, len(_KIND_ORDER)), int(num), item_id)


def strip_particle(word: str) -> str:
    """한글 낱말 끝 조사 하나를 뗀다 (§4.6 정규화 '조사 떼기') — '환경에서' → '환경', 'DB와' → 'DB'.
    세 글자 이상이고 떼고 나서 두 글자 이상 남을 때만 (짧은 낱말을 깎지 않게)."""
    if len(word) < 3:
        return word
    for p in _PARTICLES:
        if word.endswith(p) and len(word) - len(p) >= 2:
            return word[: -len(p)]
    return word


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def word_in(word: str, text: str) -> bool:
    """단어 하나가 글에 있나 — 한글이 섞이면 띄어쓰기를 무시한 부분 문자열(조사가 붙어도 걸림), 영문·숫자면 단어 경계."""
    w = word.strip()
    if not w:
        return False
    if _HANGUL.search(w):
        return _norm(w) in _norm(text)
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(w)}(?![A-Za-z0-9_])", text, re.IGNORECASE)
        is not None
    )


def term_words(term: str) -> list[str]:
    """맞출 단어 — 여러 단어로 된 낱말이면 한 글자 단어를 뺀다 (§4.6 (i)). 한 글자 단어뿐이면 그대로."""
    words = term.split()
    if len(words) < 2:
        return words
    kept = [w for w in words if len(_norm(w)) >= 2]
    return kept or words


def term_in(term: str, question: str) -> bool:
    """매칭 낱말 하나가 걸리나 (§4.6) — 낱말의 단어가 질문에 모두 있으면 (순서·사이에 낀 말 무관)."""
    words = term_words(term)
    return bool(words) and all(word_in(w, question) for w in words)


def is_clarification(item_id: str) -> bool:
    return item_id.startswith("c")


def topic_words(topic: str, general: bool = True) -> list[str]:
    """주제 낱말 — 끝 조사를 떼고(질문은 부분 문자열로 맞추므로 질문 쪽 조사는 그대로 걸린다),
    general=True 면 일반어(띄어쓰기 없는 두 글자 이하, `too_general`)를 빼고, False 면 한 글자만 뺀다."""
    words = (strip_particle(w) for w in _WORD.findall(topic))
    if general:
        return [w for w in dict.fromkeys(words) if not too_general(w)]
    return [w for w in dict.fromkeys(words) if len(w) >= 2]


def term_hits(item: Answer, question: str, phrase: bool = False) -> int:
    """질문에 걸린 `match_terms` 수 — phrase=True 면 지금 규칙(구절째, 판 3), 아니면 §4.6 단어 단위."""
    if phrase:
        return len(find_terms(item.match_terms, question))
    return sum(term_in(t, question) for t in dict.fromkeys(item.match_terms))


def topic_hits(item: Answer, question: str, general: bool | None = None) -> int:
    """질문과 겹친 주제 낱말 수. general 을 안 주면 추가 요구(c#)만 일반어를 뺀다."""
    g = is_clarification(item.id) if general is None else general
    return sum(word_in(w, question) for w in topic_words(item.topic, general=g))


def distinct_topic_words(items: Sequence[Answer], request: str = "") -> dict[str, list[str]]:
    """규칙 4 의 항목별 주제 낱말 (§4.6 (ii)(iii)) — 요청 글에 있는 말과 두 항목 이상의 주제에 함께 나오는 말을 뺀다.
    일반어는 추가 요구(c#)에서만 뺀다 (topic_words)."""
    words = {i.id: topic_words(i.topic, general=is_clarification(i.id)) for i in items}
    seen: dict[str, int] = {}
    for ws in words.values():
        for w in ws:
            seen[w] = seen.get(w, 0) + 1
    return {
        k: [w for w in ws if seen[w] < 2 and not (request and word_in(w, request))]
        for k, ws in words.items()
    }


def topic_hint_allowed(item: Answer) -> bool:
    """규칙 4(주제 겹침) 후보인가 (§4.6 (iv)) — 모호한 답(`answer_hint`)이 없는 요청자 지식(f#)은 주제가 겹쳐도 열지 않는다.
    주제 낱말 하나("처리·내용·결과")만 겹쳐도 구체 답이 나가 엉뚱한 사실을 통째로 열었다 (2026-11-13 확인용 목록 2차).
    추가 요구(c#)는 2026-11-06 확정대로 모호한 답이 없으면 구체 답."""
    return is_clarification(item.id) or item.answer_hint is not None


def _best(ids: Iterable[str], counts: Mapping[str, int]) -> str:
    """가장 많이 맞은 항목, 같으면 id 순."""
    return min(ids, key=lambda k: (-counts[k], item_order(k)))


def _sorted(ids: Iterable[str]) -> list[str]:
    return sorted(ids, key=item_order)


def answer(
    question: str, items: Sequence[Answer], states: Mapping[str, State], request: str = ""
) -> Reply:
    """§4.6 답 규칙. items = 요청자 지식(f#)과 추가 요구(c#), states = 이 지원자의 항목 상태 (없는 항목은 '없음'),
    request = 지원자가 받은 요청 글 (규칙 4 주제 낱말에서 뺀다)."""
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
    topic = distinct_topic_words(items, request)
    overlap = {
        k: sum(word_in(w, question) for w in topic[k])
        for k in by_id
        if state[k] != "open" and topic_hint_allowed(by_id[k])
    }
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
    hits = {k: term_hits(i, question, phrase=True) for k, i in by_id.items()}
    matched = [k for k, n in hits.items() if n]
    if matched:  # 1. 낱말이 걸림 — 가장 많이 맞은 하나의 구체 답
        k = _best(matched, hits)
        return Reply(
            text=by_id[k].answer, rule=1, layer="answer", item_id=k, candidates=_sorted(matched)
        )
    overlap = {k: topic_hits(i, question, general=True) for k, i in by_id.items()}
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
