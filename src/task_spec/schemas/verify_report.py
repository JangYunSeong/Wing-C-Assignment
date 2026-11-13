"""검증 결과 — verifier(#55)가 대상(시작 코드·참조 구현)마다 내는 판정 (docs/design/spec-to-code.md §4.1·§7).

코드 본문은 담지 않는다 — 테스트 이름·사유 코드·실패 메시지 앞부분·컴파일 오류 위치만 (수리 때 원인 생성기에 붙인다).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema

# §4.1 실행기 결과 분류. SETUP_ERR(빌드·기동 실패)는 대상 단위라 TargetRun.setup 으로 따로 둔다.
ResultClass = Literal["PASS", "ASSERT", "HARNESS_ERR"]
TestGroup = Literal[
    "existing", "public", "hidden", "selftest", "conform"
]  # 시작 코드 기존 테스트 / 공개 테스트(gen_public·저장 확인) / 숨은 케이스 / 하네스 자체 점검 / 계약 적합성 스모크(starter/conformance.py)
SetupResult = Literal["ok", "compile_error", "boot_error", "timeout"]
# starter_hidden = 시작 코드 그대로 + 숨은 테스트 — 지원자가 아무것도 안 하고 내면? (숨은 케이스를 모두 통과하면 과제가 이미 풀린 것)
# ref_variant — 공개 테스트 변별 관문: 시작 코드 + 변형 + 공개 테스트 (숨은 테스트 없음, #164)
TargetKind = Literal["starter", "starter_hidden", "ref_naive", "ref_accepted", "ref_variant"]


class TestResult(Schema):
    group: TestGroup
    name: str = Field(min_length=1)  # 기존 테스트 클래스 이름 또는 숨은 케이스 id (hc1)
    result: ResultClass
    reason: str | None = None  # 실패 사유 코드 (`[reason:…]`) — 템플릿 failure_reasons
    message: str = Field(default="", max_length=400)  # 실패 메시지 앞부분
    # 실패한 줄 (테스트 파일:줄, 프론트 vitest 스택에서) — 과제 생성 검증의 수리 안내에만 쓴다. 채점 결과물에는 넣지 않는다
    at: str = Field(default="", max_length=300)


class CompileError(Schema):
    path: str = Field(min_length=1)  # 프로젝트 기준 경로
    line: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=300)
    detail: str = Field(default="", max_length=300)  # javac 의 symbol·location 줄 (없는 이름일 때)


class TargetRun(Schema):
    """대상 하나 — 시작 코드만(기존 테스트), 또는 시작 코드 + 참조 구현 + 숨은 테스트."""

    target: str = Field(min_length=1)  # "starter" · 참조 구현 라벨 (naive-all · naive-miss-t2 · d1)
    kind: TargetKind
    setup: SetupResult = "ok"
    compile_errors: list[CompileError] = Field(default_factory=list)
    # 기동 실패(boot_error)의 근본 원인 — 실패 스택의 가장 깊은 `Caused by` 문장 (예: H2 "Syntax error in SQL statement … [*]GENERATED").
    # 2026-11-08 알파 4케이스: 수리 요청에 'boot_error' 한 단어만 가 시작 코드가 같은 스키마 문법 오류를 되풀이했다.
    setup_detail: str = Field(default="", max_length=400)
    tests: list[TestResult] = Field(default_factory=list)
    seconds: float | None = None

    def results(self, group: TestGroup) -> list[TestResult]:
        return [t for t in self.tests if t.group == group]

    def case(self, case_id: str) -> TestResult | None:
        return next((t for t in self.tests if t.group == "hidden" and t.name == case_id), None)


class VerifyReport(Schema):
    runs: list[TargetRun] = Field(min_length=1)

    def run(self, target: str) -> TargetRun | None:
        return next((r for r in self.runs if r.target == target), None)
