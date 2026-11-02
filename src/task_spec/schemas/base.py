"""스키마 공통 베이스."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Schema(BaseModel):
    """모든 경계 모델의 베이스. 모르는 필드는 거부하고, 만든 뒤에는 바꾸지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)
