"""원문 인용 대조 (llm-client 스킬 "원문 인용 대조").

모델은 프롬프트가 붙인 칸 표시(⟦담당업무⟧ 등)까지 인용하는 경향이 있다. 비교 전에 양쪽에서
칸 표시를 지우고 **공백·줄바꿈을 모두 없앤다** — PDF·복사본은 단어 중간에서 줄이 끊기고("데이터베⏎이스"),
모델은 붙여서 인용한다. 공백을 하나로만 줄이면 이런 인용이 모두 폐기된다 (2026-11-04: 한 이력서에서 주장 9개 중 5개).
글자(공백 제외)는 그대로 있어야 하므로 바꿔 쓰기·수치 변경은 여전히 걸린다.
가명화 토큰([ORG_1] 등)은 지우지 않는다 — 원문의 일부다.
"""

from __future__ import annotations

import re

# 칸 표시는 이 형식만 쓴다 (가명화 토큰의 대괄호와 겹치지 않게).
SECTION_MARKER = re.compile(r"⟦[^⟧]{1,40}⟧")
_WS = re.compile(r"\s+")


def normalize_for_quote(text: str) -> str:
    return _WS.sub(" ", SECTION_MARKER.sub(" ", text)).strip()


def compact_for_quote(text: str) -> str:
    """인용 대조용: 칸 표시를 지우고 공백·줄바꿈을 모두 없앤다."""
    return _WS.sub("", SECTION_MARKER.sub("", text))


def quote_in(quote: str, source: str) -> bool:
    """인용이 원문에 (공백·줄바꿈·칸 표시만 빼고) 글자 그대로 있는가."""
    q = compact_for_quote(quote)
    return bool(q) and q in compact_for_quote(source)


def verify_quote(quote: str, source: str) -> bool:
    return quote_in(quote, source)
