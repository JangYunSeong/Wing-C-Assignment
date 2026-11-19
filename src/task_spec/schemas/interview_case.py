"""한 사건의 확인된 인터뷰 내용과 실패한 첫 시도(함정 재료).

Wing-C-Interview 포크(9ef2f50)에서 옮김 (#262). 클래스·필드·제약은 포크와 같게 둔다 —
`PublicIncident` 를 통해 사례 인터뷰 프롬프트의 출력 스키마 글에 렌더된다.
"""

from __future__ import annotations

from pydantic import Field

from task_spec.schemas.base import Schema


class KnownIncidentCase(Schema):
    case_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    interview_text: str = Field(min_length=1, max_length=12000)
    answer_text: str = Field(default="", max_length=12000)
    is_trap: bool = Field(strict=True)
