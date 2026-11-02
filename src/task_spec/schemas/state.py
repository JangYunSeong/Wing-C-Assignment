"""그래프 State (pipeline-graph 스킬 "State").

규칙
- 원문·가명화 매핑 필드를 두지 않는다. 체크포인터가 State 를 영속화한다 (State = 디스크).
- fan-out 결과를 모으는 필드만 reducer(`add`)를 쓴다.
"""

from __future__ import annotations

from operator import add
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from task_spec.schemas.approval import ApprovalDecision
from task_spec.schemas.documents import DocumentRef, PseudoDocRef
from task_spec.schemas.domain import (
    Claim,
    ClaimCandidates,
    Comparison,
    CompetencyEvidence,
    CrossDocLink,
    PostingProfile,
    TaskSpec,
)
from task_spec.schemas.provenance import NodeError, Provenance


class RunState(BaseModel):
    """실행 1건의 State. thread_id = run_id."""

    # 노드가 State 에 없는 키를 돌려주는 실수를 막는다.
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    ontology_version: str = Field(min_length=1)  # 실행 시작 때 고정, 이후 불변
    posting_id: str = Field(min_length=1)
    documents: list[DocumentRef] = Field(default_factory=list)
    posting_profile: PostingProfile | None = None
    pseudo_docs: list[PseudoDocRef] = Field(default_factory=list)
    claims: Annotated[list[Claim], add] = Field(default_factory=list)
    candidates: list[ClaimCandidates] = Field(default_factory=list)
    links: list[CrossDocLink] = Field(default_factory=list)
    evidence: list[CompetencyEvidence] = Field(default_factory=list)
    comparison: Comparison | None = None
    spec: TaskSpec | None = None
    approval: ApprovalDecision | None = None
    provenance: Annotated[list[Provenance], add] = Field(default_factory=list)
    errors: Annotated[list[NodeError], add] = Field(default_factory=list)

    def has_documents(self) -> bool:
        return bool(self.documents)

    def has_both_kinds(self) -> bool:
        kinds = {d.kind for d in self.documents}
        return {"resume", "cover_letter"} <= kinds


class RunInput(BaseModel):
    """그래프 시작 입력. State 의 부분집합 — 실행 시작 때 고정되는 값만."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    ontology_version: str = Field(min_length=1)
    posting_id: str = Field(min_length=1)
    documents: list[DocumentRef] = Field(default_factory=list)
