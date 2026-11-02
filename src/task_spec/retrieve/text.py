"""retrieve 용 토큰화 (로컬 Kiwi — 외부 호출 없음).

명사·동사/형용사 어간·어근·외국어(영문) 토큰만 남긴다. 영문은 소문자.
매처 정규식에서 글자 그대로의 어휘(패턴 어휘)를 뽑는 함수도 여기 둔다.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

_KEEP = ("NNG", "NNP", "VV", "VA", "XR", "SL", "SH")
_LOOKAROUND = re.compile(r"\(\?<?[!=][^)]*\)")
_SPLIT = re.compile(r"[^0-9a-z가-힣+#.]+")
_EN = re.compile(r"[a-z][a-z0-9+#.]*")


@lru_cache(maxsize=1)
def _kiwi() -> Any:
    from kiwipiepy import Kiwi

    return Kiwi()


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for t in _kiwi().tokenize(text):
        tag = t.tag.split("-")[0]
        if tag not in _KEEP:
            continue
        form = t.form.lower()
        if tag in ("SL", "SH"):
            out.extend(_EN.findall(form))
        elif len(form) >= 1:
            out.append(form)
    return out


def pattern_terms(pattern: str | None) -> list[str]:
    """매처 정규식 → 글자 그대로의 어휘 ('ai ?(모델|기술)' → ['ai', '모델', '기술'])."""
    if not pattern:
        return []
    s = _LOOKAROUND.sub(" ", pattern.lower())
    s = re.sub(r"\\[a-z]", " ", s)  # \s \b \d …
    return [w.strip(".") for w in _SPLIT.split(s) if len(w.strip(".")) >= 2]
