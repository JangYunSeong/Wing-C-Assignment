"""가명화 엔진: 탐지 → 겹침 해소 → 치환 → 알려진 값 전역 재치환 → 누락 검사.

- 한 실행(run) 안에서는 같은 실체 → 같은 토큰 (`RunMapping`, 문서 사이 공유). 실행 사이에는 공유하지 않는다.
- 인구통계(나이·성별·병역 등)는 토큰 없이 지운다.
- 기술·도구명, 수치, 기간·날짜는 바꾸지 않는다 (T2 기간 대조에 필요).
- 매핑의 원 값은 이 모듈과 mapping.json 밖으로 나가지 않는다. 결과에는 범주별 건수만.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from task_spec.nodes.pseudonymize.aliases import (
    AliasSet,
    build_alias_set,
    same_person,
    sub_outside_tokens,
)
from task_spec.nodes.pseudonymize.detectors import (
    REMOVE,
    TOKEN_RE,
    Span,
    Spread,
    detect_compound_names,
    detect_courses,
    detect_demographics,
    detect_dictionary,
    detect_english_names,
    detect_intro_names,
    detect_kiwi_names,
    detect_label_cells,
    detect_labels,
    detect_name_shapes,
    detect_org_context,
    detect_patterns,
    detect_titled_names,
)
from task_spec.nodes.pseudonymize.dictionaries import Dictionaries
from task_spec.nodes.pseudonymize.kiwi_tools import document_cache
from task_spec.nodes.pseudonymize.romanize import KO_PARTICLES, split_name

logger = logging.getLogger(__name__)

_NORMALIZE_RX = re.compile(r"\s+|\(주\)|㈜|주식회사")


def _norm(value: str) -> str:
    return _NORMALIZE_RX.sub("", value).casefold()


@dataclass
class RunMapping:
    """토큰 → 원 값 목록. mapping.json 으로만 저장한다 (권한 600)."""

    tokens: dict[str, list[str]] = field(default_factory=dict)
    index: dict[str, str] = field(default_factory=dict)  # 정규화 키 → 토큰
    counters: dict[str, int] = field(default_factory=dict)
    # 전파 범위가 좁은 값 (탐지기 `Span.spread`). 같은 값이 더 넓게 전파할 탐지로 다시 잡히면 넓은 쪽으로 옮긴다.
    # local: 그 자리만 / context: 고용 문맥이 붙은 언급에만 ("X에서 3년간", "X에 입사" — 공개 어휘로 된 회사 이름)
    # 롤백 주의: 이 두 필드를 모르는 이전 코드는 둘 다 무시하고 전부 전파한다.
    local: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    # run 단위 정규식 캐시 (저장하지 않는다). 지원자 값이 키라 프로세스 전역 캐시에 두지 않는다.
    _cache: dict[Any, Any] = field(default_factory=dict, repr=False, compare=False)

    def token_for(
        self, category: str, value: str, canonical: str | None, *, spread: Spread = "all"
    ) -> str:
        keys = [_norm(value)] + ([_norm(canonical)] if canonical else [])
        for k in keys:
            if k in self.index:
                tok = self.index[k]
                break
        else:
            n = self.counters.get(category, 0) + 1
            self.counters[category] = n
            tok = f"[{category}_{n}]"
            self.tokens[tok] = []
        for k in keys:
            self.index.setdefault(k, tok)
        is_new = value not in self.tokens[tok]
        if is_new:
            self.tokens[tok].append(value)
        self._set_spread(value, spread, is_new)
        return tok

    def _set_spread(self, value: str, spread: Spread, is_new: bool) -> None:
        """새 값은 그 탐지의 범위, 이미 있던 값은 넓은 쪽으로만 바꾼다 (none < context < all)."""
        rank = {"none": 0, "context": 1, "all": 2}
        level = spread if is_new else max(self.spread_of(value), spread, key=rank.__getitem__)
        if level != "none" and value in self.local:
            self.local.remove(value)
        if level != "context" and value in self.context:
            self.context.remove(value)
        if level == "none" and value not in self.local:
            self.local.append(value)
        if level == "context" and value not in self.context:
            self.context.append(value)

    def spread_of(self, value: str) -> Spread:
        return "none" if value in self.local else "context" if value in self.context else "all"

    def values(self) -> list[str]:
        """run 전체로 전파할 원 값 (긴 것부터). `local`·`context` 값은 뺀다."""
        narrow = set(self.local) | set(self.context)
        return sorted(
            {v for vs in self.tokens.values() for v in vs if len(v) >= 2 and v not in narrow},
            key=len,
            reverse=True,
        )

    def context_values(self) -> list[str]:
        return sorted({v for v in self.context if len(v) >= 2}, key=len, reverse=True)

    def person_values(self) -> list[tuple[str, str]]:
        """(토큰, 원 값) — 사람 이름만. 로마자·영문 변형 전파에 쓴다."""
        local = set(self.local)
        return [
            (t, v)
            for t, vs in self.tokens.items()
            if t.startswith("[PERSON_")
            for v in vs
            if v not in local
        ]

    def state_key(self) -> tuple[Any, ...]:
        """정규식 캐시 키 — 값·전파 범위가 바뀌면 달라진다."""
        return (
            tuple((t, tuple(vs)) for t, vs in self.tokens.items()),
            tuple(self.local),
            tuple(self.context),
        )

    def value_to_token(self) -> dict[str, str]:
        return {v: t for t, vs in self.tokens.items() for v in vs}

    @classmethod
    def load(cls, path: Path) -> RunMapping:
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            tokens=data["tokens"],
            index=data["index"],
            counters=data["counters"],
            local=data.get("local", []),
            context=data.get("context", []),
        )

    def save(self, path: Path) -> None:
        """고유 임시 파일(mkstemp, 600) → fsync → 원자적 교체. 기존 파일 권한과 무관하게 600 이 된다."""
        ensure_private_dir(path.parent)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mapping-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "tokens": self.tokens,
                        "index": self.index,
                        "counters": self.counters,
                        "local": self.local,
                        "context": self.context,
                    },
                    f,
                    ensure_ascii=False,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def neutralize_token_lookalikes(text: str) -> str:
    """원문에 이미 있는 토큰 모양 문자열([PERSON_9] 등)을 ⟨PERSON_9⟩ 로 바꾼다.

    엔진이 발급한 토큰과 원문의 토큰 모양을 구분하기 위해서다. 그대로 두면 탐지기가 그 구간을
    '이미 가명화됨'으로 보고 같은 라벨 값 안의 실제 개인정보까지 건너뛴다 (privacy-auditor C1).
    """
    return TOKEN_RE.sub(lambda m: "⟨" + m.group(0)[1:-1] + "⟩", text)


@dataclass(frozen=True)
class Detectors:
    dicts: Dictionaries
    use_kiwi: bool = True

    def detect(self, text: str) -> list[Span]:
        with document_cache():  # 줄 분석 결과는 이 문서를 보는 동안만 (code review M4)
            return self._detect(text)

    def _detect(self, text: str) -> list[Span]:
        spans = (
            detect_patterns(text)
            + detect_demographics(text)
            + detect_labels(text)
            + detect_label_cells(text, self.dicts)
            + detect_dictionary(text, self.dicts)
            + detect_org_context(text, self.dicts, kiwi=self.use_kiwi)
            + detect_courses(text, self.dicts, kiwi=self.use_kiwi)
            + detect_titled_names(text, self.dicts)
            + detect_intro_names(text, self.dicts)
            + detect_compound_names(text, self.dicts)
            + detect_name_shapes(text, self.dicts, kiwi=self.use_kiwi)
            + detect_english_names(text, self.dicts)
        )
        if self.use_kiwi:
            spans += detect_kiwi_names(text, self.dicts)
        protected = [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]
        return [s for s in spans if not any(s.start < e and b < s.end for b, e in protected)]


def resolve(spans: list[Span]) -> list[Span]:
    """겹침 해소: 우선순위 높은 것, 같으면 긴 것부터 받고 겹치는 나머지는 버린다.
    같은 자리·같은 길이면 대표표기(canonical)가 있는 쪽 — 같은 실체를 한 토큰으로 묶는 정보를 잃지 않게."""
    accepted: list[Span] = []
    for s in sorted(
        spans, key=lambda s: (-s.priority, -(s.end - s.start), s.canonical is None, s.start)
    ):
        if all(s.end <= a.start or a.end <= s.start for a in accepted):
            accepted.append(s)
    return sorted(accepted, key=lambda s: s.start)


def _value_rx(values: tuple[str, ...]) -> re.Pattern[str] | None:
    if not values:
        return None
    return re.compile("|".join(re.escape(v) for v in values))


# 공개 어휘로 된 회사 이름(spread="context")은 고용 문맥이 붙은 언급에만 전파한다 (재점검 H-A).
# "X에서 3년간·근무·재직·일하·개발", "X에 입사·합류", "X 인턴·재직 중", "(전) X". 누락 검사도 같은 정규식으로 센다.
_EMPLOY_AFTER = (
    r"(?:에서(?:는|의)?[ \t]*(?:약[ \t]*)?\d+[ \t]*(?:년|개월)"
    r"|에서(?:는)?[ \t]*(?:근무|재직|일하|일했|일한|개발|인턴|퇴사|입사|근속)"
    r"|에[ \t]*(?:입사|합류|재직|근무|취업|이직)"
    r"|[ \t]+(?:인턴|재직[ \t]?중|근무[ \t]?중|입사|퇴사|재직|근무))"
)


def _employment_rx(values: list[str]) -> re.Pattern[str] | None:
    if not values:
        return None
    alt = "|".join(re.escape(v) for v in values)
    return re.compile(
        rf"(?:\(전\)|전[ \t]?직장[ \t]*[:：]?)[ \t]*(?P<v1>{alt})(?![가-힣A-Za-z0-9])"
        rf"|(?<![가-힣A-Za-z0-9])(?P<v2>{alt})(?={_EMPLOY_AFTER})"
    )


def _sub_group(text: str, rx: re.Pattern[str], v2t: dict[str, str], count: Counter[str]) -> str:
    def repl(m: re.Match[str]) -> str:
        g = "v1" if m.group("v1") is not None else "v2"
        a, b = m.start(g) - m.start(), m.end(g) - m.start()
        whole = m.group(0)
        count[v2t[m.group(g)]] += 1
        return whole[:a] + v2t[m.group(g)] + whole[b:]

    return sub_outside_tokens(text, rx, repl)


@dataclass(frozen=True)
class _Propagation:
    literal: re.Pattern[str] | None
    employment: re.Pattern[str] | None
    aliases: AliasSet
    v2t: dict[str, str]


def _propagation(mapping: RunMapping, dicts: Dictionaries) -> _Propagation:
    """run 의 전파 정규식 — 매핑이 바뀌지 않으면 캐시를 쓴다."""
    key = ("propagation", mapping.state_key())
    cached = mapping._cache.get(key)
    if cached is None:
        mapping._cache.clear()
        cached = _Propagation(
            literal=_value_rx(tuple(mapping.values())),
            employment=_employment_rx(mapping.context_values()),
            aliases=build_alias_set(mapping.person_values(), dicts),
            v2t=mapping.value_to_token(),
        )
        mapping._cache[key] = cached
    return cached


def _replace_known(
    text: str, mapping: RunMapping, dicts: Dictionaries, tally: Counter[str] | None = None
) -> str:
    """이미 매핑된 값이 다른 곳(라벨 없는 본문 등)에 또 나오면 같은 토큰으로 바꾼다. 토큰 안은 건드리지 않는다.

    순서: 원 값 그대로 → 고용 문맥 언급(공개 어휘 회사) → 사람 이름 다른 표기(전체 표기 → 이름만, `aliases`).
    `tally` 를 주면 토큰별 전파 치환 수를 센다 (값은 세지 않는다).
    """
    prop = _propagation(mapping, dicts)
    count = tally if tally is not None else Counter()
    if prop.literal is not None:

        def lit(m: re.Match[str]) -> str:
            tok = prop.v2t[m.group(0)]
            count[tok] += 1
            return tok

        text = sub_outside_tokens(text, prop.literal, lit)
    if prop.employment is not None:
        text = _sub_group(text, prop.employment, prop.v2t, count)
    return prop.aliases.replace(text, count)


_GIVEN_WITH_PARTICLE = re.compile(rf"(?P<given>[가-힣]{{2,3}}?){KO_PARTICLES}?")


def _link_person(
    value: str, people: list[tuple[str, str]], pending: list[str], dicts: Dictionaries
) -> tuple[str | None, bool]:
    """같은 사람의 다른 표기가 이미 있으면 (그 값, 성 뺀 이름인가) — 한 토큰으로 묶기 위한 canonical (2차 M2).

    성 뺀 한글 이름("하람", Kiwi 가 조사까지 붙여 잡은 "하람은")은 그 이름을 가진 사람이 run 에 한 명뿐일 때만 묶는다
    (마지막 점검 H1). 둘 이상이면 누구인지 모르므로 묶지 않는다.
    """
    others = [v for _t, v in people] + [v for v in pending if v != value]
    for other in others:
        if same_person(value, other, dicts):
            return other, False
    m = _GIVEN_WITH_PARTICLE.fullmatch(value)
    if m:
        owners = {
            o
            for o in others
            if (parts := split_name(o, dicts.surnames))
            and parts[1] == m.group("given")
            and o != value
        }
        if len(owners) == 1:
            return owners.pop(), True
    return None, False


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*[,/]?\s*\)", "", text)  # 지워진 괄호
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"[ \t]+\n", "\n", text)


@dataclass(frozen=True)
class PseudoResult:
    text: str
    counts: dict[str, int]
    residual: int
    detectors: list[str]


@dataclass
class _Assigned:
    text: str  # 탐지 구간을 토큰으로 바꾼 본문 (전파 전)
    counts: Counter[str]
    detectors: list[str]
    header_tokens: set[str]  # 경력 머리말 첫 칸에서 잡힌 회사 토큰 (본문 전파 수를 따로 센다)


def _detect(text: str, detectors: Detectors) -> tuple[str, list[Span]]:
    text = neutralize_token_lookalikes(text)
    return text, resolve(detectors.detect(text))


def _assign(
    text: str, spans: list[Span], mapping: RunMapping, detectors: Detectors, pending: list[str]
) -> _Assigned:
    """1단계: 탐지 결과로 토큰을 정한다 (매핑·전파 범위 갱신). 전파는 하지 않는다.
    `pending` 은 run 의 모든 문서에서 잡힌 사람 이름 — 뒤 문서의 성+이름에도 앞 문서의 이름만 표기를 묶는다."""
    counts: Counter[str] = Counter()
    people = mapping.person_values()
    header_tokens: set[str] = set()
    pieces, last = [], 0
    for s in spans:
        pieces.append(text[last : s.start])
        if s.category == REMOVE:
            pieces.append("")
        else:
            value = s.value(text)
            canonical = s.canonical
            spread = s.spread
            if s.category == "PERSON" and canonical is None:
                canonical, given_only = _link_person(value, people, pending, detectors.dicts)
                if given_only:
                    # 성 뺀 이름 값은 그대로 퍼뜨리지 않는다 — 다른 언급은 그 사람의 이름만 표기 규칙(조사·호칭 문맥,
                    # 흔한 말이면 호칭만)이 바꾼다. 원 값 그대로 퍼뜨리면 "하늘색"까지 바뀐다.
                    spread = "none"
            tok = mapping.token_for(s.category, value, canonical, spread=spread)
            if s.detector == "org_header":
                header_tokens.add(tok)
            pieces.append(tok)
        counts[s.category] += 1
        last = s.end
    pieces.append(text[last:])
    return _Assigned(
        text="".join(pieces),
        counts=counts,
        detectors=sorted({s.detector.split(":")[0] for s in spans}),
        header_tokens=header_tokens,
    )


# 머리말 값 하나가 본문에서 이만큼 이상 바뀌면 경고 (토큰 id·건수만 남긴다, 마지막 점검 M2)
HEADER_SPREAD_WARN = 5


def _finish(
    a: _Assigned, mapping: RunMapping, detectors: Detectors, header_tokens: set[str]
) -> PseudoResult:
    """2단계: run 전체 매핑으로 전파하고 누락 검사."""
    tally: Counter[str] = Counter()
    out = _tidy(_replace_known(a.text, mapping, detectors.dicts, tally))
    counts = dict(a.counts)
    spread = {tok: n for tok, n in tally.items() if tok in header_tokens}
    if spread:
        counts["header_propagated"] = sum(spread.values())
        for tok, n in sorted(spread.items()):
            if n >= HEADER_SPREAD_WARN:
                logger.warning("header value propagated widely token=%s count=%d", tok, n)
    return PseudoResult(
        text=out,
        counts=counts,
        residual=count_residual(out, mapping, detectors),
        detectors=a.detectors,
    )


def pseudonymize_documents(
    texts: Sequence[str], mapping: RunMapping, detectors: Detectors
) -> list[PseudoResult]:
    """한 run 의 문서들을 두 번 거쳐 처리한다: 전부 탐지 → 토큰·전파 범위를 정함 → 전파.

    문서별로 바로 전파하면, 문서 A 에서 그 자리만이던 값이 문서 B 에서 전파 대상으로 바뀌었을 때 A 의 다른 언급이
    남는다 (code review M5).
    """
    detected = [_detect(t, detectors) for t in texts]
    pending = list(
        dict.fromkeys(s.value(t) for t, spans in detected for s in spans if s.category == "PERSON")
    )
    assigned = [_assign(t, spans, mapping, detectors, pending) for t, spans in detected]
    header_tokens = set().union(*(a.header_tokens for a in assigned)) if assigned else set()
    return [_finish(a, mapping, detectors, header_tokens) for a in assigned]


def pseudonymize_text(text: str, mapping: RunMapping, detectors: Detectors) -> PseudoResult:
    return pseudonymize_documents([text], mapping, detectors)[0]


def count_residual(text: str, mapping: RunMapping, detectors: Detectors) -> int:
    """누락 검사 = 재탐지 건수 + 매핑 원 값이 토큰 밖에 남은 건수 + 엔진이 발급하지 않은 토큰 수.

    원 값은 전파 범위대로 센다 — 전체 전파 값은 어디서든, 공개 어휘 회사는 고용 문맥에서, 사람 이름은 다른 표기까지.
    마지막 항목은 neutralize 를 거치지 않은 텍스트(위조 토큰)를 막는 두 번째 방어선이다.
    """
    stripped = TOKEN_RE.sub(lambda m: " " * len(m.group(0)), text)
    redetected = len(resolve(detectors.detect(text)))
    prop = _propagation(mapping, detectors.dicts)
    leftovers = sum(stripped.count(v) for v in mapping.values())
    if prop.employment is not None:
        leftovers += sum(1 for _ in prop.employment.finditer(stripped))
    leftovers += prop.aliases.count(stripped)
    leftovers += prop.aliases.adjacent_surnames(text)
    forged = sum(1 for m in TOKEN_RE.finditer(text) if m.group(0) not in mapping.tokens)
    return redetected + leftovers + forged
