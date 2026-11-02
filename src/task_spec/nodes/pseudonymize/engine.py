"""가명화 엔진: 탐지 → 겹침 해소 → 치환 → 알려진 값 전역 재치환 → 누락 검사.

- 한 실행(run) 안에서는 같은 실체 → 같은 토큰 (`RunMapping`, 문서 사이 공유). 실행 사이에는 공유하지 않는다.
- 인구통계(나이·성별·병역 등)는 토큰 없이 지운다.
- 기술·도구명, 수치, 기간·날짜는 바꾸지 않는다 (T2 기간 대조에 필요).
- 매핑의 원 값은 이 모듈과 mapping.json 밖으로 나가지 않는다. 결과에는 범주별 건수만.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from task_spec.nodes.pseudonymize.detectors import (
    REMOVE,
    TOKEN_RE,
    Span,
    detect_demographics,
    detect_dictionary,
    detect_intro_names,
    detect_kiwi_names,
    detect_labels,
    detect_patterns,
    detect_titled_names,
)
from task_spec.nodes.pseudonymize.dictionaries import Dictionaries

_NORMALIZE_RX = re.compile(r"\s+|\(주\)|㈜|주식회사")


def _norm(value: str) -> str:
    return _NORMALIZE_RX.sub("", value).casefold()


@dataclass
class RunMapping:
    """토큰 → 원 값 목록. mapping.json 으로만 저장한다 (권한 600)."""

    tokens: dict[str, list[str]] = field(default_factory=dict)
    index: dict[str, str] = field(default_factory=dict)  # 정규화 키 → 토큰
    counters: dict[str, int] = field(default_factory=dict)

    def token_for(self, category: str, value: str, canonical: str | None) -> str:
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
        if value not in self.tokens[tok]:
            self.tokens[tok].append(value)
        return tok

    def values(self) -> list[str]:
        return sorted(
            {v for vs in self.tokens.values() for v in vs if len(v) >= 2}, key=len, reverse=True
        )

    def value_to_token(self) -> dict[str, str]:
        return {v: t for t, vs in self.tokens.items() for v in vs}

    @classmethod
    def load(cls, path: Path) -> RunMapping:
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(tokens=data["tokens"], index=data["index"], counters=data["counters"])

    def save(self, path: Path) -> None:
        """고유 임시 파일(mkstemp, 600) → fsync → 원자적 교체. 기존 파일 권한과 무관하게 600 이 된다."""
        ensure_private_dir(path.parent)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mapping-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {"tokens": self.tokens, "index": self.index, "counters": self.counters},
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
        spans = (
            detect_patterns(text)
            + detect_demographics(text)
            + detect_labels(text)
            + detect_dictionary(text, self.dicts)
            + detect_titled_names(text, self.dicts)
            + detect_intro_names(text, self.dicts)
        )
        if self.use_kiwi:
            spans += detect_kiwi_names(text, self.dicts)
        protected = [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]
        return [s for s in spans if not any(s.start < e and b < s.end for b, e in protected)]


def resolve(spans: list[Span]) -> list[Span]:
    """겹침 해소: 우선순위 높은 것, 같으면 긴 것부터 받고 겹치는 나머지는 버린다."""
    accepted: list[Span] = []
    for s in sorted(spans, key=lambda s: (-s.priority, -(s.end - s.start), s.start)):
        if all(s.end <= a.start or a.end <= s.start for a in accepted):
            accepted.append(s)
    return sorted(accepted, key=lambda s: s.start)


@lru_cache(maxsize=256)
def _value_rx(values: tuple[str, ...]) -> re.Pattern[str] | None:
    if not values:
        return None
    return re.compile("|".join(re.escape(v) for v in values))


def _replace_known(text: str, mapping: RunMapping) -> str:
    """이미 매핑된 값이 다른 곳(라벨 없는 본문 등)에 또 나오면 같은 토큰으로 바꾼다. 토큰 안은 건드리지 않는다."""
    rx = _value_rx(tuple(mapping.values()))
    if rx is None:
        return text
    v2t = mapping.value_to_token()
    out, last = [], 0
    for tok in TOKEN_RE.finditer(text):
        out.append(rx.sub(lambda m: v2t[m.group(0)], text[last : tok.start()]))
        out.append(tok.group(0))
        last = tok.end()
    out.append(rx.sub(lambda m: v2t[m.group(0)], text[last:]))
    return "".join(out)


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


def pseudonymize_text(text: str, mapping: RunMapping, detectors: Detectors) -> PseudoResult:
    text = neutralize_token_lookalikes(text)
    spans = resolve(detectors.detect(text))
    counts: Counter[str] = Counter()
    pieces, last = [], 0
    for s in spans:
        pieces.append(text[last : s.start])
        if s.category == REMOVE:
            pieces.append("")
        else:
            pieces.append(mapping.token_for(s.category, s.value(text), s.canonical))
        counts[s.category] += 1
        last = s.end
    pieces.append(text[last:])
    out = _tidy(_replace_known("".join(pieces), mapping))
    residual = count_residual(out, mapping, detectors)
    return PseudoResult(
        text=out,
        counts=dict(counts),
        residual=residual,
        detectors=sorted({s.detector.split(":")[0] for s in spans}),
    )


def count_residual(text: str, mapping: RunMapping, detectors: Detectors) -> int:
    """누락 검사 = 재탐지 건수 + 매핑 원 값이 토큰 밖에 남은 건수 + 엔진이 발급하지 않은 토큰 수.

    마지막 항목은 neutralize 를 거치지 않은 텍스트(위조 토큰)를 막는 두 번째 방어선이다.
    """
    stripped = TOKEN_RE.sub(lambda m: " " * len(m.group(0)), text)
    redetected = len(resolve(detectors.detect(text)))
    leftovers = sum(stripped.count(v) for v in mapping.values())
    forged = sum(1 for m in TOKEN_RE.finditer(text) if m.group(0) not in mapping.tokens)
    return redetected + leftovers + forged
