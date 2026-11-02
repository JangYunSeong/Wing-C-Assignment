"""가명화된 텍스트 타입 (pseudonymization 스킬 "타입으로 강제").

`PseudonymizedText` 는 가명화 모듈(`task_spec.nodes.pseudonymize`)만 만든다. 생성에는 모듈 private
토큰 `_MINT` 가 필요하다 — 다른 곳에서 `_MINT` 를 import 하는 것은 리뷰에서 Critical (테스트 헬퍼 제외).

LLM·임베딩 경계(`prompting.assemble`, 모델 입력 스키마)는 이 타입만 받는다. 일반 `str` 은 런타임에 거부된다.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import PlainSerializer, PlainValidator

_MINT = object()


class PseudonymizedText(str):
    __slots__ = ()

    def __new__(cls, value: str, *, _token: object = None) -> PseudonymizedText:
        if _token is not _MINT:
            raise TypeError("PseudonymizedText 는 가명화 모듈만 만들 수 있습니다")
        return super().__new__(cls, value)


def _require_pseudonymized(value: Any) -> PseudonymizedText:
    if not isinstance(value, PseudonymizedText):
        raise ValueError("가명화되지 않은 텍스트는 받을 수 없습니다 (PseudonymizedText 필요)")
    return value


PseudoTextField = Annotated[
    PseudonymizedText,
    PlainValidator(_require_pseudonymized),
    PlainSerializer(str, return_type=str),
]
