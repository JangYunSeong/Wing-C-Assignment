"""LLM 호출 1건의 출처 기록 (prompt-registry 스킬 §3)과 노드 에러 기록."""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field

from task_spec.schemas.base import Schema


class TokenUsage(Schema):
    """입력/출력/추론 토큰. JSON 키는 prompt-registry 형식 그대로 `in`."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    in_: int = Field(default=0, ge=0, alias="in")
    out: int = Field(default=0, ge=0)
    reasoning: int = Field(default=0, ge=0)


class Provenance(Schema):
    """LLM 호출마다 1건. 메시지 본문·키 값은 담지 않는다."""

    provenance_id: str = Field(min_length=1)
    prompt: str = Field(pattern=r"^[a-z0-9_]+@\d+$")  # "<task>@<version>"
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(pattern=r"^[a-z0-9_-]+:.+$")  # "<provider>:<model>"
    params: dict[str, Any] = Field(default_factory=dict)
    fewshot_ids: list[str] = Field(default_factory=list)
    criteria_ids: list[str] = Field(default_factory=list)
    ontology_version: str = Field(min_length=1)
    retries: int = Field(default=0, ge=0)
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = Field(default=0, ge=0)
    cost_usd: float | None = None


class NodeError(Schema):
    """노드 에러 기록. message 에 원문·가명화 매핑 조각을 넣지 않는다 (id·코드만)."""

    node: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message: str = Field(default="", max_length=500)
    ref_id: str | None = None  # 관련 doc_id / claim_id
