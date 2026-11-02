"""원문 인용 대조 (llm-client 스킬 "원문 인용 대조").

모델은 프롬프트가 붙인 칸 표시(⟦담당업무⟧ 등)까지 인용하는 경향이 있다. 비교 전에 양쪽에서
칸 표시를 지우고 공백을 정규화한다. 가명화 토큰([ORG_1] 등)은 지우지 않는다 — 원문의 일부다.
"""

from __future__ import annotations

import re

# 칸 표시는 이 형식만 쓴다 (가명화 토큰의 대괄호와 겹치지 않게).
SECTION_MARKER = re.compile(r"⟦[^⟧]{1,40}⟧")
_WS = re.compile(r"\s+")


def normalize_for_quote(text: str) -> str:
    return _WS.sub(" ", SECTION_MARKER.sub(" ", text)).strip()


def verify_quote(quote: str, source: str) -> bool:
    q = normalize_for_quote(quote)
    return bool(q) and q in normalize_for_quote(source)
