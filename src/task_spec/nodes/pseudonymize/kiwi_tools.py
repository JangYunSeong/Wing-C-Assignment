"""Kiwi(로컬 형태소 분석기) 공용 도우미 — 외부 호출 없음.

- `get_kiwi()`: 프로세스에 하나만 올린다.
- `oov_ids()`: 사전 밖 명사(일반·고유)에 붙는 형태소 id. 런타임 탐지기와 `eval/pseudonymize/build_public_vocab.py` 가 같이 쓴다.
- `line_tokens()` + `document_cache()`: 줄 단위 분석 결과를 **문서 하나를 처리하는 동안만** 캐시한다. 줄 원문이 키라서
  프로세스 전역 캐시에 두면 지원자 문장이 메모리에 남는다 (code review M4). 캐시 밖에서 부르면 매번 분석한다.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

Token = tuple[int, int, str, int]  # (시작, 끝, 품사, 형태소 id) — 줄 안 위치

_kiwi: Any = None
_kiwi_lock = threading.Lock()
_oov: frozenset[int] | None = None
_line_cache: ContextVar[dict[str, tuple[Token, ...]] | None] = ContextVar(
    "pseudonymize_line_cache", default=None
)


def get_kiwi() -> Any:
    global _kiwi
    with _kiwi_lock:
        if _kiwi is None:
            from kiwipiepy import Kiwi

            _kiwi = Kiwi()
        return _kiwi


def oov_ids(kiwi: Any | None = None) -> frozenset[int]:
    """사전 밖 명사 id (일반 명사·고유 명사). 버전마다 다를 수 있어 실행 중에 확인한다."""
    global _oov
    if kiwi is not None:
        return frozenset(
            t.id for t in kiwi.tokenize("뷁뛟쀍에서 퀣뷁이") if t.tag in ("NNG", "NNP")
        )
    if _oov is None:
        _oov = oov_ids(get_kiwi())
    return _oov


def kiwi_version() -> str:
    try:
        import kiwipiepy

        return str(getattr(kiwipiepy, "__version__", "unknown"))
    except ImportError:  # pragma: no cover - 의존성에 있다
        return "unknown"


@contextmanager
def document_cache() -> Iterator[None]:
    """이 안에서 분석한 줄만 캐시하고, 나가면 버린다 (원문이 프로세스에 남지 않게)."""
    token = _line_cache.set({})
    try:
        yield
    finally:
        cache = _line_cache.get()
        if cache is not None:
            cache.clear()
        _line_cache.reset(token)


def line_tokens(line: str) -> tuple[Token, ...]:
    cache = _line_cache.get()
    if cache is not None and line in cache:
        return cache[line]
    toks = tuple((t.start, t.start + t.len, t.tag, t.id) for t in get_kiwi().tokenize(line))
    if cache is not None:
        cache[line] = toks
    return toks
