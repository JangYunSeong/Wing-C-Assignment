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


class FacilitatorNotAvailable(DomainError):
    """진행자 보기를 줄 수 없음 — 검사·검증을 마치지 않은 빌드이거나 공통 과제 명세가 없다."""

    code = "facilitator_not_available"
    status_code = 404

    def __init__(self, build_id: str, reason: str) -> None:
        super().__init__(f"진행자 자료를 볼 수 없습니다 ({reason}): {build_id}")


class CasesNotAvailable(DomainError):
    """테스트 정보를 볼 수 없음 — 검사·검증을 마치지 않은 빌드."""

    code = "cases_not_available"
    status_code = 404

    def __init__(self, build_id: str, reason: str) -> None:
        super().__init__(f"테스트 정보를 볼 수 없습니다 ({reason}): {build_id}")


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


class CommonTaskNotFound(DomainError):
    code = "common_task_not_found"
    status_code = 404

    def __init__(self, common_id: str) -> None:
        super().__init__(f"공통 과제를 찾을 수 없습니다: {common_id}")


class CommonTaskGone(DomainError):
    """승인하려는 명세가 가리키는 공통 과제 판이 저장소에 없다 — 확정하면 실패하므로 승인 전에 막는다."""

    code = "common_task_gone"
    status_code = 409

    def __init__(self, common_ids: list[str], *, run_id: str) -> None:
        super().__init__(
            f"이 명세가 가리키는 공통 과제가 저장소에 없습니다 ({', '.join(common_ids)}) — "
            "같은 공고로 명세를 다시 만드세요",
            run_id=run_id,
        )


class BuildRuleViolation(DomainError):
    """빌드 저장소 규칙 위반 (상태·역할 충돌) — 저장소 메시지는 id·상태·역할만 담는다."""

    code = "build_rule_violation"
    status_code = 409


class DeliveryTargetNotConfigured(DomainError):
    """DB 에 등록된 대상이지만 API 설정(SFTP_TARGETS__<이름>__*)에 접속 정보가 없다 — 이름만 담는다."""

    code = "delivery_target_not_configured"
    status_code = 409

    def __init__(self, name: str) -> None:
        super().__init__(f"전달 대상 {name} 의 접속 설정이 없습니다 (docs/deploy.md 2-11)")


class BuildApprovalInvalid(DomainError):
    """빌드 승인 그래프가 resume 값을 받지 않고 다시 기다린다 — 사유는 검토 표시 판단·저장소 규칙 (원문 없음)."""

    code = "build_approval_invalid"
    status_code = 422


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


class PullUnauthorized(DomainError):
    """받는 쪽 토큰이 없거나 맞지 않음 (/pull) — 어느 토큰이 틀렸는지 말하지 않는다."""

    code = "pull_unauthorized"
    status_code = 401

    def __init__(self) -> None:
        super().__init__("받는 쪽 토큰이 필요합니다 (Authorization: Bearer <토큰>)")


class PullNotAvailable(DomainError):
    """이 토큰의 역할로 가져갈 수 있는 묶음이 아님 — 빌드가 없거나, 승인·확정 전이거나, 그 역할의 묶음·파일이 없다.
    어느 쪽인지 구분하지 않는다 (받는 쪽에 다른 빌드의 상태를 알리지 않게)."""

    code = "pull_not_available"
    status_code = 404

    def __init__(self, build_id: str) -> None:
        super().__init__(f"{build_id}: 가져갈 수 있는 묶음이 없습니다")
