"""도메인 예외와 에러 응답 형식 — 예외 → 코드 매핑은 `install_error_handlers` 한 곳.

메시지에 업로드 원문·파일 바이트·요청 값을 넣지 않는다 (id·크기·확장자·코드만).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    run_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class DomainError(Exception):
    code = "bad_request"
    status_code = 400

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.run_id = run_id


class RunNotFound(DomainError):
    code = "run_not_found"
    status_code = 404

    def __init__(self, run_id: str) -> None:
        super().__init__("실행을 찾을 수 없습니다", run_id=run_id)


class SpecNotFound(DomainError):
    code = "spec_not_found"
    status_code = 404

    def __init__(self, run_id: str) -> None:
        super().__init__("승인 확정된 명세가 없습니다", run_id=run_id)


class PostingNotFound(DomainError):
    code = "posting_not_found"
    status_code = 404

    def __init__(self, posting_id: str) -> None:
        # 새 공고는 POST /postings/suggest → POST /postings 로 임시 공고(prov-…)를 먼저 만든다
        super().__init__(f"공고를 찾을 수 없습니다: {posting_id}")


class ReadmeNotAvailable(DomainError):
    """README 가 없거나, 검사·검증을 마치지 않은 빌드 (review 전·failed)."""

    code = "readme_not_available"
    status_code = 404

    def __init__(self, build_id: str, reason: str) -> None:
        super().__init__(f"지원자 README 를 볼 수 없습니다 ({reason}): {build_id}")


class UiDisabled(DomainError):
    code = "not_found"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("화면이 꺼져 있거나 없는 파일입니다 (UI_ENABLED)")


class ApprovalNotPending(DomainError):
    code = "approval_not_pending"
    status_code = 409


class UnsupportedFileType(DomainError):
    code = "unsupported_file_type"
    status_code = 415


class FileTooLarge(DomainError):
    code = "file_too_large"
    status_code = 413


class ServiceUnavailable(DomainError):
    code = "service_unavailable"
    status_code = 503


class BuildNotFound(DomainError):
    code = "build_not_found"
    status_code = 404

    def __init__(self, build_id: str) -> None:
        super().__init__(f"빌드를 찾을 수 없습니다: {build_id}")


class BuildRuleViolation(DomainError):
    """빌드 저장소 규칙 위반 (상태·역할 충돌) — 저장소 메시지는 id·상태·역할만 담는다."""

    code = "build_rule_violation"
    status_code = 409


class InvalidUpload(DomainError):
    code = "invalid_upload"
    status_code = 422


def _response(status: int, code: str, message: str, run_id: str | None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, run_id=run_id))
    return JSONResponse(status_code=status, content=body.model_dump())


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        return _response(exc.status_code, exc.code, exc.message, exc.run_id)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 기본 핸들러는 입력 값(input)을 되돌려 준다 — 위치와 사유만 남긴다.
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        run_id = request.path_params.get("run_id")
        return _response(422, "validation_error", detail, run_id)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return _response(exc.status_code, code, str(exc.detail), request.path_params.get("run_id"))

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error type=%s path=%s", type(exc).__name__, request.url.path)
        return _response(500, "internal_error", "내부 오류", request.path_params.get("run_id"))
