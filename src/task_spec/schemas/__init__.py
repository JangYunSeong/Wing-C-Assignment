"""경계 Pydantic 모델. 노드·API·그래프는 여기 모델만 주고받는다."""

from __future__ import annotations

from task_spec.schemas.approval import ApprovalDecision, ApprovalStatus
from task_spec.schemas.claims import ClaimExtractionInput, ClaimExtractionOutput, ExtractedClaim
from task_spec.schemas.documents import DocumentKind, DocumentRef, PseudoCheck, PseudoDocRef
from task_spec.schemas.domain import (
    Claim,
    ClaimCandidates,
    Comparison,
    CompetencyEvidence,
    CrossDocLink,
    PostingProfile,
    Role,
    TaskItem,
    TaskSpec,
)
from task_spec.schemas.ingest import (
    ExtractedDocument,
    ExtractedField,
    ExtractedRef,
    ExtractedSection,
    SectionHeader,
)
from task_spec.schemas.provenance import NodeError, Provenance, TokenUsage
from task_spec.schemas.pseudo_text import PseudonymizedText
from task_spec.schemas.state import RunInput, RunState

__all__ = [
    "ApprovalDecision",
    "ApprovalStatus",
    "Claim",
    "ClaimCandidates",
    "ClaimExtractionInput",
    "ClaimExtractionOutput",
    "Comparison",
    "CompetencyEvidence",
    "CrossDocLink",
    "DocumentKind",
    "DocumentRef",
    "ExtractedClaim",
    "ExtractedDocument",
    "ExtractedField",
    "ExtractedRef",
    "ExtractedSection",
    "NodeError",
    "PostingProfile",
    "Provenance",
    "PseudoCheck",
    "PseudoDocRef",
    "PseudonymizedText",
    "Role",
    "RunInput",
    "RunState",
    "SectionHeader",
    "TaskItem",
    "TaskSpec",
    "TokenUsage",
]
