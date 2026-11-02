"""판단 기준·few-shot 예시 제공자.

온톨로지 레포가 판정 사례·결정 기록 문장을 export 하기 전까지는 빈 제공자를 쓴다 (prompt-registry §2·§6).
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class FewshotContext(BaseModel):
    """예시 검색 조건. 평가 모드에서는 exclude_positions 가 비면 안 된다 (정답 누설 방지)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sub_family: str | None = None
    industry: str | None = None
    exclude_positions: frozenset[str] = Field(default_factory=frozenset)
    mode: Literal["run", "eval"] = "run"


class Criterion(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    text: str


class Example(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    text: str


class CriteriaProvider(Protocol):
    def get(self, keys: list[str]) -> list[Criterion]: ...


class FewshotProvider(Protocol):
    def search(
        self, source: str, k: int, match: list[str], ctx: FewshotContext
    ) -> list[Example]: ...


class EmptyCriteria:
    def get(self, keys: list[str]) -> list[Criterion]:
        return []


class EmptyFewshot:
    def search(self, source: str, k: int, match: list[str], ctx: FewshotContext) -> list[Example]:
        return []
