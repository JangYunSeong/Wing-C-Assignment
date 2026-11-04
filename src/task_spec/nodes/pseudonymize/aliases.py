"""사람 이름의 다른 표기 정규식 (2026-11-04 2차, privacy-auditor H1·M2, 재점검 M-B·M-C, code review H1·M1·M2). 외부 호출 없음.

엔진은 이 run 에서 잡힌 사람 이름마다 아래 표기를 같은 토큰으로 문서 어디서든 바꾸고(`engine._replace_known`),
누락 검사에서도 같은 정규식으로 센다 — 바꾸는 규칙과 세는 규칙이 같아야 조용한 누출도, 불필요한 실행 정지도 없다.

- 전체 표기 (먼저 적용, 긴 이름부터):
  - 한글 이름 "서가온": 로마자 성+이름 ("Seo Gaon", "Gaon Seo", "SEO-GAON", "seogaon", "gaon_seo" …), 이니셜+성
    ("G. Seo", "Seo G.", "G.O. Seo"), 띄어 쓴 한글 성+이름 ("서 가온은").
  - 영문 이름 "Seo Gaon": 같은 단어 대소문자 무시, 두세 단어면 거꾸로 쓴 순서.
- 이름만 표기 (나중에 적용): 한글 이름의 로마자 이름("Gaon", 두 음절 이상), 한글 이름("가온은" — 두 음절 이상,
  공개 어휘가 아니고 Kiwi 가 일반 명사로 보지 않을 때), 영문 이름의 성씨 아닌 4자 이상 단어.
  흔한 영어·기술·공개 어휘 단어(`Dictionaries.alias_stop_words`)와 겹치면 만들지 않는다.
  **같은 run 에 이름이 같은 사람이 둘 이상이면 이름만 표기를 끈다** — "김가온"·"서가온" 이 함께 있으면 "Gaon" 이 누구인지
  모르고, 앞사람 토큰으로 바꾸면 "Seo [PERSON_1]" 처럼 성이 새고 토큰도 틀린다 (code review H1).
- 연결(같은 사람의 한글·영문 표기를 한 토큰으로)은 전체 표기만 쓴다.

정규식은 run 단위로 컴파일해 `RunMapping` 에 둔다 — 지원자 이름이 키가 되는 프로세스 전역 캐시를 두지 않는다.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import product

from task_spec.nodes.pseudonymize.detectors import TOKEN_RE
from task_spec.nodes.pseudonymize.dictionaries import Dictionaries
from task_spec.nodes.pseudonymize.kiwi_tools import get_kiwi, oov_ids
from task_spec.nodes.pseudonymize.romanize import (
    canonical_given,
    full_name_pattern,
    given_only_pattern,
    initials_pattern,
    korean_given_patterns,
    korean_spaced_pattern,
    split_name,
    surname_variants,
    syllable_variants,
)

_LATIN_NAME = re.compile(r"[A-Za-z]+(?:[ \t,\-]+[A-Za-z]+){1,3}")
_WORD_SEP = r"[ \t]*[,\-_.]?[ \t]*"


def _words_rx(words: list[str]) -> str:
    return r"(?<![A-Za-z])" + _WORD_SEP.join(re.escape(w) for w in words) + r"(?![A-Za-z])"


def _latin_words(value: str) -> list[str] | None:
    if not _LATIN_NAME.fullmatch(value):
        return None
    return [w for w in re.split(r"[ \t,\-]+", value.strip()) if w]


# Kiwi 가 한 단어로 볼 때 이 점수보다 높으면(흔하면) 일반어로 본다 — "사랑" -7.9, "하늘" -8.4, "바람" -8.4, "바다" -10.7 /
# "누리" -11.1, "서연" -11.5, "가온" -15.4, "라온" -15.4 (kiwipiepy 0.24.1, 단어 하나만 넣은 분석 점수)
_COMMON_SCORE = -11.0


def _given_level(given: str, dicts: Dictionaries) -> str:
    """한글 이름만 표기의 범위: bare / particle / title (`romanize.korean_given_patterns`)."""
    if given in dicts.public_vocab or given in dicts.tech_terms:
        return "title"
    toks = get_kiwi().tokenize(given)
    oov = oov_ids()
    if toks and all(t.id not in oov for t in toks) and sum(t.score for t in toks) > _COMMON_SCORE:
        return "title"  # 흔한 일반어 ("하늘", "바람")
    if len(toks) == 1 and toks[0].tag in ("NNG", "NNP"):
        return "bare"  # 이름처럼 쓰이는 한 명사 ("가온", "라온", "시우")
    return "particle"  # 동사·여러 조각 ("해온", "다온", "윤슬")


@dataclass
class _Person:
    token: str
    length: int
    full: list[str] = field(default_factory=list)
    given: list[str] = field(default_factory=list)
    given_keys: set[str] = field(default_factory=set)
    # 문서에 이 사람의 다른 표기가 있을 수 있는지 싸게 거르는 열쇠 (정규식은 후보만 컴파일 — code review M1·M2)
    squashed: set[str] = field(default_factory=set)  # 구분자 없앤 로마자 이름 변형 ("gaon")
    words: set[str] = field(
        default_factory=set
    )  # 단어로 나와야 하는 성 로마자·영문 이름 단어 ("seo")
    hangul: set[str] = field(default_factory=set)  # 한글 이름 ("가온")
    surnames: set[str] = field(
        default_factory=set
    )  # 성 로마자 변형 (토큰 바로 옆에 남는지 누락 검사용)


def _given_variants(given: str) -> set[str]:
    return {"".join(p) for p in product(*(syllable_variants(c) for c in given))}


def _person(token: str, values: list[str], dicts: Dictionaries) -> _Person:
    p = _Person(token=token, length=max(len(v) for v in values))
    stop = dicts.alias_stop_words
    for value in values:
        parts = split_name(value, dicts.surnames)
        if parts:
            surname, given = parts
            p.full += [
                full_name_pattern(surname, given),
                initials_pattern(surname, given),
                korean_spaced_pattern(surname, given),
            ]
            variants = _given_variants(given)
            p.squashed |= variants
            p.words |= surname_variants(surname)
            p.surnames |= surname_variants(surname)
            p.hangul.add(given)
            p.given_keys |= {given, canonical_given(given)}
            if len(given) >= 2 and not (variants & stop):
                p.given.append(given_only_pattern(given))
            if len(given) >= 2:  # 성 뺀 한글 이름 (마지막 점검 H1)
                p.given += korean_given_patterns(given, _given_level(given, dicts))
            continue
        words = _latin_words(value)
        if words:
            p.full.append(_words_rx(words))
            if len(words) <= 3:
                p.full.append(_words_rx(words[::-1]))
            for w in words:
                lw = w.casefold()
                p.words.add(lw)
                if lw in dicts.roman_surnames:
                    continue
                p.given_keys.add(lw.replace("-", ""))
                if len(w) >= 4 and lw not in stop:
                    p.given.append(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])")
    return p


def _combined(persons: list[_Person], attr: str) -> tuple[re.Pattern[str] | None, list[str]]:
    groups, tokens = [], []
    for p in persons:
        pats = list(dict.fromkeys(getattr(p, attr)))
        if pats:
            groups.append(f"(?P<g{len(tokens)}>" + "|".join(f"(?:{x})" for x in pats) + ")")
            tokens.append(p.token)
    if not groups:
        return None, []
    return re.compile("|".join(groups), re.IGNORECASE), tokens


def sub_outside_tokens(text: str, rx: re.Pattern[str], repl: Callable[[re.Match[str]], str]) -> str:
    out, last = [], 0
    for tok in TOKEN_RE.finditer(text):
        out.append(rx.sub(repl, text[last : tok.start()]))
        out.append(tok.group(0))
        last = tok.end()
    out.append(rx.sub(repl, text[last:]))
    return "".join(out)


_SQUASH = re.compile(r"[^a-z]")
_WORDS = re.compile(r"[a-z]+")


@dataclass
class _Compiled:
    full_rx: re.Pattern[str] | None
    full_tokens: list[str]
    given_rx: re.Pattern[str] | None
    given_tokens: list[str]


@dataclass
class AliasSet:
    """한 run 의 사람 이름 다른 표기. 문서마다 그 문서에 나올 수 있는 사람만 골라 전체 표기 정규식 하나,
    이름만 표기 정규식 하나로 합쳐 컴파일한다 (code review M1·M2). 매치한 그룹으로 토큰을 찾는다."""

    persons: list[_Person]
    _compiled: dict[tuple[int, ...], _Compiled] = field(default_factory=dict, repr=False)

    def _for(self, text: str) -> _Compiled:
        low = text.casefold()
        squashed = _SQUASH.sub("", low)
        words = set(_WORDS.findall(low))
        idx = tuple(
            i
            for i, p in enumerate(self.persons)
            if any(h in text for h in p.hangul)
            or (p.words & words)
            or any(v in squashed for v in p.squashed)
        )
        c = self._compiled.get(idx)
        if c is None:
            chosen = [self.persons[i] for i in idx]
            full_rx, full_tokens = _combined(chosen, "full")
            given_rx, given_tokens = _combined(chosen, "given")
            c = self._compiled[idx] = _Compiled(full_rx, full_tokens, given_rx, given_tokens)
        return c

    @staticmethod
    def _repl(tokens: list[str], count: Counter[str]) -> Callable[[re.Match[str]], str]:
        def repl(m: re.Match[str]) -> str:
            tok = tokens[int(str(m.lastgroup)[1:])]
            count[tok] += 1
            return tok

        return repl

    def replace(self, text: str, count: Counter[str] | None = None) -> str:
        """전체 표기(긴 이름부터) → 이름만 표기 순서로 바꾼다. `count` 에 토큰별 치환 수를 더한다."""
        tally = count if count is not None else Counter()
        c = self._for(text)
        if c.full_rx is not None:
            text = sub_outside_tokens(text, c.full_rx, self._repl(c.full_tokens, tally))
        if c.given_rx is not None:
            text = sub_outside_tokens(text, c.given_rx, self._repl(c.given_tokens, tally))
        return text

    def count(self, stripped: str) -> int:
        """토큰을 공백으로 지운 텍스트에서 남은 다른 표기 수 (누락 검사)."""
        c = self._for(stripped)
        return sum(
            sum(1 for _ in rx.finditer(stripped))
            for rx in (c.full_rx, c.given_rx)
            if rx is not None
        )

    def adjacent_surnames(self, text: str) -> int:
        """토큰 바로 옆(구분자 -_. 하나까지)에 그 사람 성 로마자가 남은 수 ("[PERSON_1].seo", "seo_[PERSON_1]").
        이름만 바뀌고 성이 남은 것이므로 누락으로 센다 (마지막 점검 M1)."""
        n = 0
        for p in self.persons:
            if not p.surnames or p.token not in text:
                continue
            sur = "|".join(re.escape(v) for v in sorted(p.surnames, key=len, reverse=True))
            tok = re.escape(p.token)
            rx = re.compile(
                rf"{tok}[-_.]?(?:{sur})(?![A-Za-z])|(?<![A-Za-z])(?:{sur})[-_.]?{tok}",
                re.IGNORECASE,
            )
            n += sum(1 for _ in rx.finditer(text))
        return n

    @property
    def given_patterns(self) -> list[str]:
        return [x for p in self.persons for x in p.given]


def build_alias_set(person_values: list[tuple[str, str]], dicts: Dictionaries) -> AliasSet:
    by_token: dict[str, list[str]] = {}
    for tok, value in person_values:
        by_token.setdefault(tok, []).append(value)
    persons = [_person(tok, vals, dicts) for tok, vals in by_token.items()]
    # 이름이 같은 사람이 둘 이상이면 그 이름만 표기를 끈다 (code review H1)
    owners: dict[str, set[str]] = {}
    for p in persons:
        for k in p.given_keys:
            if k:
                owners.setdefault(k, set()).add(p.token)
    shared = {p.token for p in persons for k in p.given_keys if len(owners.get(k, ())) > 1}
    for p in persons:
        if p.token in shared:
            p.given = []
    persons.sort(
        key=lambda p: -p.length
    )  # 긴 이름부터 (같은 자리에서 짧은 이름이 먼저 잡히지 않게)
    return AliasSet(persons)


def same_person(value: str, other: str, dicts: Dictionaries) -> bool:
    """두 표기가 같은 사람의 전체 이름 표기인가 ("서가온" ↔ "Seo Gaon", "Seo Gaon" ↔ "Gaon Seo")."""
    if value == other:
        return True
    a_words, b_words = _latin_words(value), _latin_words(other)
    if a_words is not None and b_words is not None:
        return sorted(w.casefold() for w in a_words) == sorted(w.casefold() for w in b_words)
    if a_words is None and b_words is None:
        return False  # 한글끼리는 엔진 정규화 키로 묶는다
    korean, latin = (value, other) if a_words is None else (other, value)
    parts = split_name(korean, dicts.surnames)
    return bool(parts) and bool(re.fullmatch(full_name_pattern(*parts), latin, re.IGNORECASE))
