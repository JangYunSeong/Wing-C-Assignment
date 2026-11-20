"""인터뷰 업무 오류. API 계층을 import 하지 않으려고 DomainError 와 같은 모양(code·status_code·message)으로 따로 둔다 —
api/errors.install_error_handlers 가 같은 형식의 응답으로 바꾼다. 코드는 포크 API 와 같다 (화면 호환)."""

from __future__ import annotations


class InterviewError(Exception):
    code = "interview_error"
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InterviewNotFound(InterviewError):
    code = "interview_not_found"
    status_code = 404


class InterviewInvalid(InterviewError):
    code = "interview_invalid"
    status_code = 422


class InterviewConflict(InterviewError):
    """판본이 바뀌었거나 지금 단계에서 할 수 없는 작업."""

    code = "interview_conflict"
    status_code = 409


class InterviewGenerating(InterviewConflict):
    """과제를 만드는 중이라 그 인터뷰를 바꿀 수 없다 (#280) — 임시 저장만 된다."""

    code = "interview_generating"


class InterviewClosed(InterviewError):
    """폐기된 인터뷰 — 대응표를 지웠으므로 더 진행하지 않는다."""

    code = "interview_closed"
    status_code = 409


class InterviewNotReady(InterviewError):
    """인터뷰 프롬프트에 CURRENT 가 없다 (평가 전) — 새 작업은 평가를 통과한 뒤에만 돈다."""

    code = "interview_not_ready"
    status_code = 503


class InterviewUnavailable(InterviewError):
    """LLM 응답을 쓰지 못했다 (형식·근거·가명화 검사 실패, 일시 오류). 입력은 저장하지 않는다 — 다시 보내면 된다."""

    code = "service_unavailable"
    status_code = 503


class PostingMissing(InterviewError):
    code = "posting_not_found"
    status_code = 404


class OutputRejected(ValueError):
    """LLM 출력이 근거·수치·형식 검사를 넘지 못했다. 메시지는 이 코드의 고정 문구라 로그에 남겨도 된다 —
    다른 ValueError(라이브러리 등)는 입력 조각을 담을 수 있어 사유를 로그에 남기지 않는다 (privacy-auditor L4)."""
