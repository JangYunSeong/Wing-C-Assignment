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

from task_spec.interview.errors import InterviewError

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


class ApprovalRevisionMismatch(DomainError):
    """승인 결정이 가리키는 판(?revision=)이 지금 승인 대기 판이 아니다 — 그 사이 다른 결정으로 다시 만들어졌다 (#247)."""

    code = "approval_revision_mismatch"
    status_code = 409

    def __init__(self, asked: int, current: int | None, *, run_id: str) -> None:
        now = f"{current}판" if current is not None else "판 없음"
        super().__init__(
            f"보고 있는 판({asked}판)이 지금 승인 대기 판({now})이 아닙니다 — 새로 고친 뒤 다시 결정하세요",
            run_id=run_id,
        )


class CareerLevelRequired(DomainError):
    """실행 만들기 전에 공고의 신입·경력이 정해져 있어야 한다 [확정: 2026-11-19 사용자] — 정한 값이 없으면 career_level 을 함께."""

    code = "career_level_required"
    status_code = 422

    def __init__(self, posting_id: str) -> None:
        super().__init__(
            f"공고 {posting_id} 의 신입·경력이 정해지지 않았습니다 — career_level(신입·경력)을 함께 보내세요"
        )


class CareerLevelConflict(DomainError):
    """공고에 정한 값과 다른 career_level — 같은 공고 지원자는 공통 과제를 같이 쓰므로 실행마다 바꾸지 않는다."""

    code = "career_level_conflict"
    status_code = 409

    def __init__(self, posting_id: str, current: str, asked: str) -> None:
        super().__init__(
            f"공고 {posting_id} 는 이미 '{current}' 로 정해져 있습니다 ('{asked}' 를 보냄) — "
            "바꾸려면 PUT /postings/{id}/career-level"
        )


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


class CommonTaskReplaced(DomainError):
    """승인하려는 명세의 공통 과제를 이 공고가 이제 쓰지 않는다 — 채용담당자 인터뷰 과제가 승인·교체됐다 [확정: 2026-11-19 사용자].
    확정하면 공고 지원자끼리 다른 과제가 되므로 승인 전에 막는다."""

    code = "common_task_replaced"
    status_code = 409

    def __init__(self, common_ids: list[str], *, run_id: str) -> None:
        super().__init__(
            f"이 공고의 공통 과제가 인터뷰 과제로 정해졌거나 바뀌었습니다 (이 명세의 과제: {', '.join(common_ids)}) — "
            "경험 검증 다시 만들기(revise_target=verification)로 지금 과제를 반영한 명세를 만드세요",
            run_id=run_id,
        )


class InterviewTaskLocked(DomainError):
    """지원자 명세의 수정 요청으로 인터뷰 과제를 고치려 한다 — 인터뷰 과제는 채용담당자가 인터뷰 화면에서 고친다."""

    code = "interview_task_locked"
    status_code = 409

    def __init__(self, common_id: str, *, run_id: str) -> None:
        super().__init__(
            f"이 공고의 공통 과제는 채용담당자 인터뷰로 승인된 과제({common_id})라 여기서 고칠 수 없습니다 — "
            "과제는 인터뷰 화면에서 고치고, 이 명세는 경험 검증만 다시 만들 수 있습니다 (revise_target=verification)",
            run_id=run_id,
        )


class InterviewTaskVersionMismatch(DomainError):
    """공고의 인터뷰 과제가 다른 온톨로지 판으로만 승인돼 있다 — 과제 안의 역량·수행준거 id 가 그 판 것이라 이 판 실행에 쓸 수
    없고(불변식 4), 공개 사례 과제로 바꿔 만들지도 않는다 [확정: 2026-11-19 사용자]."""

    code = "interview_task_version_mismatch"
    status_code = 409

    def __init__(self, posting_id: str, versions: list[str], current: str) -> None:
        super().__init__(
            f"공고 {posting_id} 의 인터뷰 과제는 온톨로지 {', '.join(versions)} 로 승인돼 있어 지금 판({current})에서 "
            "쓸 수 없습니다 — 인터뷰에서 지금 판으로 다시 승인한 뒤 실행하세요"
        )


class PostingCompanyInvalid(DomainError):
    """회사 표기에서 회사를 알 수 없다 (법인 표기·공백만 있음)."""

    code = "posting_company_invalid"
    status_code = 422

    def __init__(self) -> None:
        super().__init__("회사 표기에서 회사를 알 수 없습니다 (법인 표기·공백만 있음)")


class PostingCompanyConflict(DomainError):
    """실행 만들기에서 받은 회사가 공고에 정해진 회사와 다르다 — 공고의 회사를 바꾸려면 PUT /postings/{id}/company."""

    code = "posting_company_conflict"
    status_code = 409

    def __init__(self, posting_id: str, current: str, asked: str) -> None:
        super().__init__(
            f"공고 {posting_id} 의 회사는 '{current}' 입니다 (받은 값 '{asked}') — 바꾸려면 PUT /postings/{posting_id}/company"
        )


class PostingDuplicate(DomainError):
    """같은 회사·같은 원문의 임시 공고가 이미 있다 (POST /postings). 실수로 두 번 올리는 것을 막는다 — 같은 공고로 다른 문제를
    만들려고 일부러 따로 등록할 때는 allow_duplicate [확정: 2026-11-20 사용자 — 알리고, 확인하면 따로 등록]."""

    code = "posting_duplicate"
    status_code = 409

    def __init__(self, posting_ids: list[str]) -> None:
        super().__init__(
            f"같은 회사·같은 원문의 공고가 이미 있습니다: {', '.join(posting_ids)} — 그 공고를 쓰거나, 다른 문제를 만들려고 "
            "따로 등록하려면 allow_duplicate 를 true 로 보내 주세요"
        )


class PostingLinkConflict(DomainError):
    """공고를 이을 수 없다 — 사슬(기준 공고가 이어져 있음·이 공고에 이어진 공고가 있음)·이미 다른 공고에 이어짐·회사가 다름
    (docs/design/posting-link.md)."""

    code = "posting_link_conflict"
    status_code = 409


class ChosenTaskUnavailable(DomainError):
    """실행 만들 때 고른 문제가 그 사이 교체(superseded)되거나 사라졌다 — 승인 전에 막는다 [확정: 2026-11-20 사용자 — 인터뷰 과제의
    '교체 전 과제를 가리키는 명세는 승인하지 않는다'와 같게]. 실행을 새로 만들어 다시 고른다."""

    code = "chosen_task_unavailable"
    status_code = 409

    def __init__(self, common_id: str, *, run_id: str) -> None:
        super().__init__(
            f"실행 만들 때 고른 문제({common_id})가 교체됐거나 사라졌습니다 — 실행을 새로 만들어 문제를 다시 고르세요 "
            "(고를 수 있는 문제: GET /postings/{id}/tasks)",
            run_id=run_id,
        )


class ChosenTaskLocked(DomainError):
    """고른 문제를 지원자 명세의 수정 요청으로 고치려 한다 — 고른 과제는 그대로 쓴다."""

    code = "chosen_task_locked"
    status_code = 409

    def __init__(self, common_id: str, *, run_id: str) -> None:
        super().__init__(
            f"이 실행은 만들 때 고른 문제({common_id})를 그대로 씁니다 — 다른 문제로 바꾸려면 실행을 새로 만들고, 이 명세는 "
            "경험 검증만 다시 만들 수 있습니다 (revise_target=verification)",
            run_id=run_id,
        )


class CommonTaskChoiceRequired(DomainError):
    """공고 묶음(이어진 공고 포함)에 승인된 문제가 둘 이상인데 실행 만들 때 고르지 않았다 [확정: 2026-11-20 사용자]."""

    code = "common_task_choice_required"
    status_code = 422

    def __init__(self, posting_id: str, common_ids: list[str]) -> None:
        super().__init__(
            f"공고 {posting_id} 묶음에 승인된 문제가 여럿입니다({', '.join(common_ids)}) — common_id 로 하나를 고르세요 "
            f"(GET /postings/{posting_id}/tasks)"
        )


class CommonTaskNotChoosable(DomainError):
    """실행 만들 때 고른 문제(common_id)가 이 공고(이어진 공고 포함)에 실행 온톨로지 판으로 승인된 공통 과제가 아니다 —
    고를 수 있는 문제는 GET /postings/{id}/tasks."""

    code = "common_task_not_choosable"
    status_code = 422

    def __init__(self, posting_id: str, common_id: str) -> None:
        super().__init__(
            f"{common_id} 는 공고 {posting_id}(이어진 공고 포함)에 승인된 문제가 아닙니다 — GET /postings/{posting_id}/tasks 에서 고르세요"
        )


class IncidentNotFound(DomainError):
    code = "incident_not_found"
    status_code = 404

    def __init__(self, incident_id: str) -> None:
        super().__init__(f"회사 사례를 찾을 수 없습니다: {incident_id}")


class IncidentInvalid(DomainError):
    """회사 사례 등록·조회 값이 맞지 않다 (회사 표기를 id 로 못 바꿈 등)."""

    code = "incident_invalid"
    status_code = 422


class IncidentContentBlocked(DomainError):
    """회사 사례에 연락처·링크로 보이는 표현이 있다 — 공유되면 여러 공고·LLM 입력으로 퍼지므로 등록하지 않는다.
    값은 돌려주지 않고 칸·범주만."""

    code = "incident_content_blocked"
    status_code = 422

    def __init__(self, findings: list[str]) -> None:
        super().__init__(
            "연락처·링크로 보이는 표현이 있어 등록하지 않았습니다 — 지우거나 일반 표현으로 바꿔 다시 등록하세요: "
            + "; ".join(findings)
        )


class IncidentNotPending(DomainError):
    """공유 승인·보관할 수 있는 상태가 아니다 (이미 결정·보관됨)."""

    code = "incident_not_pending"
    status_code = 409


class IncidentShareInvalid(DomainError):
    """공유 승인 그래프가 결정을 받지 않고 다시 기다린다 — 사유를 그대로 돌려준다."""

    code = "incident_share_invalid"
    status_code = 422


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

    @app.exception_handler(InterviewError)
    async def _interview(_: Request, exc: InterviewError) -> JSONResponse:
        # 인터뷰 패키지는 API 를 import 하지 않으려고 같은 모양(code·status_code·message)의 오류를 따로 둔다 (#262)
        return _response(exc.status_code, exc.code, exc.message, None)

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
