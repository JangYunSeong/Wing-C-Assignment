"""② 입력 처리 결과.

- `ExtractedDocument`: 추출·구조화 결과 전체. 원문(개인정보 포함)이 들어 있으므로 State 에 넣지 않고
  `data/extracted/<run_id>/<doc_id>.json` (권한 600) 으로만 저장한다.
- `ExtractedRef`: State 에 남기는 참조 (경로·해시·방법·실패 사유). 원문 없음.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from task_spec.schemas.base import Schema
from task_spec.schemas.documents import SHA256_PATTERN, DocumentKind

ExtractMethod = Literal["text_layer", "ocr", "docx", "hwpx", "txt"]
ExtractFailure = Literal["unsupported_format", "empty_text", "extract_error", "ocr_error"]

# 칸 라벨 정규형 (역할 규칙 판정의 입력 — assign_roles)
# 경고는 코드 목록만 (원문 조각이 State·체크포인트로 들어가지 않게, 감사 W8)
WarningCode = Annotated[str, Field(pattern=r"^(page\d+:two_columns|ocr_pages=\d+)$")]

FieldLabel = Literal["문제 정의", "과정", "기여한 점", "담당업무", "성과", "사용 기술"]


class ExtractedField(Schema):
    label: FieldLabel
    text: str


class SectionHeader(Schema):
    """경력·프로젝트 머리말 줄. 값은 원문 그대로(가명화 전)."""

    line: str
    period: str | None = None


class ExtractedSection(Schema):
    kind: str  # 경력 / 학력 / 프로젝트 / 교육 / 자기소개 / 기술 / 기타
    title: str | None = None
    headers: list[SectionHeader] = Field(default_factory=list)
    fields: list[ExtractedField] = Field(default_factory=list)


class ExtractedDocument(Schema):
    doc_id: str
    method: ExtractMethod
    text: str  # ⟦칸⟧ 표시가 붙은 본문 — 가명화 입력
    sections: list[ExtractedSection] = Field(default_factory=list)
    pages: int = 0
    warnings: list[WarningCode] = Field(default_factory=list)


class ExtractedRef(Schema):
    doc_id: str = Field(min_length=1)
    kind: DocumentKind
    ok: bool
    path: str | None = None  # 추출 텍스트 (.txt)
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    method: ExtractMethod | None = None
    reason: ExtractFailure | None = None
    sections: int = 0
    fields: int = 0
    warnings: list[WarningCode] = Field(default_factory=list)
