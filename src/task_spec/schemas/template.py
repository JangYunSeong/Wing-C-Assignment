"""시작 코드 템플릿 manifest 중 생성 파이프라인이 쓰는 부분 (docs/design/spec-to-code.md §4.1) [제안].

- 계약 검사(§3.3)용: 하네스 조절값 `harness_params`(명세 자료에서 값을 뽑는 표기 패턴·시작 코드 설정 키),
  실패 사유 코드 `failure_reasons`, 이름 규칙 `naming`, 허용 인터페이스 `interfaces`, 허용 설계의 스택 조건 `stack_conditions`.
- 템플릿 레지스트리(starter/templates.py)용: 스택·명령·kind 별 쓸 수 있는 경로 `fill_slots`·잠금 `locked`·채점 전용 `reserved`·
  채점 조립 위치 `hidden_layout`·상한 `limits`·이미지 층 `image` (starter-templates.md §3·§6.1).
- manifest.yaml 은 P1 내부용 — 템플릿·응시용 이미지에 넣지 않는다 (§6.1 [확정: 2026-11-07 사용자]).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from task_spec.schemas.base import Schema

InterfaceKind = Literal["http", "cli", "file"]
HarnessValueKind = Literal["int", "float", "str", "int_list"]

_NAME = r"^[a-z][a-z0-9_]*$"


def _compiles(patterns: list[str]) -> list[str]:
    for p in patterns:
        try:
            re.compile(p)
        except re.error as e:
            raise ValueError(f"정규식이 아님: {p!r} ({e})") from e
    return patterns


class HarnessParam(Schema):
    """하네스 조절값 1개 — 숨은 테스트가 채점 때 넣는 운영 조건 (예: 풀 크기·동시 요청 수·지연)."""

    name: str = Field(pattern=_NAME)
    kind: HarnessValueKind
    description: str = Field(min_length=1, max_length=300)
    minimum: float | None = None
    maximum: float | None = None
    required: bool = True  # HiddenPlan 이 반드시 값을 줘야 하나
    # 지원자가 1단계 자료·구체 답에서 같은 값을 발견할 수 있어야 하나 (운영 조건 수치 — spec-to-code §3.3)
    visible: bool = False
    # 명세 자료에서 값을 뽑는 표기 패턴 — 정규식, 그룹 1 = 값 (예: r"maximumPoolSize\s*[=:]\s*(\d+)", r"풀\s*(\d+)\s*개")
    spec_patterns: list[str] = Field(default_factory=list)
    # 표(공백·| 구분)의 열 이름 정규식 — 그 열의 숫자를 값으로 뽑는다 (예: 요청 지표 표의 inflight_requests 열)
    spec_columns: list[str] = Field(default_factory=list)
    # 시작 코드 설정 파일에서 이 값을 담는 키 (§6 정적 검사 — 운영 값이 시작 코드에 박혔는지)
    config_keys: list[str] = Field(default_factory=list)

    @field_validator("spec_patterns")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for p in _compiles(v):
            if re.compile(p).groups < 1:
                raise ValueError(f"표기 패턴에 값 그룹이 없음: {p!r}")
        return v

    @field_validator("spec_columns")
    @classmethod
    def _columns(cls, v: list[str]) -> list[str]:
        return _compiles(v)

    @model_validator(mode="after")
    def _visible_needs_pattern(self) -> HarnessParam:
        if self.visible and not (self.spec_patterns or self.spec_columns):
            raise ValueError(
                f"{self.name}: visible 조절값은 표기 패턴(spec_patterns·spec_columns)이 1개 이상 필요"
            )
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"{self.name}: minimum 이 maximum 보다 큼")
        return self


class FailureReason(Schema):
    """의도한 실패 사유 코드 1개 — verifier 가 실행 로그·예외를 이 패턴으로 대조한다 (게이트 ③)."""

    code: str = Field(pattern=_NAME)
    description: str = Field(min_length=1, max_length=300)
    log_patterns: list[str] = Field(min_length=1)

    @field_validator("log_patterns")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        return _compiles(v)


class NamingRules(Schema):
    """계약 이름 규칙 (템플릿 스택의 관례)."""

    path: str = r"^/[a-z0-9/{}\-]*$"  # http 경로
    header: str = r"^[A-Z][A-Za-z0-9]*(-[A-Z][A-Za-z0-9]*)*$"  # http 헤더 (예: X-Actor)
    entity: str = r"^[A-Z][A-Za-z0-9]*$"
    field: str = r"^[a-z][A-Za-z0-9]*$"

    @field_validator("path", "header", "entity", "field")
    @classmethod
    def _rx(cls, v: str) -> str:
        return _compiles([v])[0]


class StackInfo(Schema):
    language: str = Field(min_length=1)
    language_version: str = Field(min_length=1)
    framework: str = Field(min_length=1)
    framework_version: str = Field(min_length=1)
    build_tool: str = Field(min_length=1)
    build_tool_version: str = Field(min_length=1)


class TemplateCommands(Schema):
    """작업 경로 안에서 돌리는 명령 (검증·채점 컨테이너는 네트워크 없음 — 오프라인 명령)."""

    build: str = Field(min_length=1)
    test: str = Field(min_length=1)
    hidden_test: str = Field(min_length=1)
    run: str | None = None


class HiddenLayout(Schema):
    """채점용 이미지 조립 때 템플릿 hidden/ 과 과제 hidden 오버레이를 둘 곳 (프로젝트 기준 상대 경로)."""

    harness: str = Field(min_length=1)  # hidden/harness/* 가 들어갈 폴더 (잠김)
    selftest: str = Field(min_length=1)  # hidden/selftest/* (잠김)
    cases: str = Field(min_length=1)  # 과제 케이스 (kind=hidden)
    params: str = Field(min_length=1)  # HiddenPlan.harness → 조절값 파일


class TemplateLimits(Schema):
    max_files: int = Field(ge=1)
    max_file_bytes: int = Field(ge=1)
    max_total_bytes: int = Field(ge=1)
    allow_dependency_changes: bool = False


class ImageLayers(Schema):
    """이미지 층 — 템플릿·응시용 이미지에는 template_layer 만, grader_only 는 채점용 레이어에서만 (§6.1)."""

    template_layer: list[str] = Field(default_factory=lambda: ["project/"])
    grader_only: list[str] = Field(default_factory=lambda: ["hidden/", "manifest.yaml"])
    grader_only_dependencies: list[str] = Field(default_factory=list)  # 하네스 전용 라이브러리


class HiddenRules(Schema):
    """숨은 테스트 케이스(kind=hidden) 정적 검사 규칙 — 스택마다 다른 부분 (spec-to-code §6)."""

    # 케이스 파일에 있으면 안 되는 줄 (정규식, 줄 단위) — 지원자 구현 내부(클래스·메서드)를 직접 쓰는 것 (게이트 ⑥ 사전 확인)
    forbidden_imports: list[str] = Field(default_factory=list)
    # 케이스가 붙인 실패 사유 표시에서 코드를 뽑는 정규식 (그룹 1 = 코드) — manifest failure_reasons 안이어야 함
    reason_patterns: list[str] = Field(default_factory=list)
    # 케이스 id(HiddenPlan.cases[].case_id)가 케이스 파일에 나오는 모양 — 기본은 낱말 hc1·hc2…
    case_id_pattern: str = r"\bhc\d{1,2}\b"

    @field_validator("forbidden_imports", "reason_patterns")
    @classmethod
    def _rx(cls, v: list[str]) -> list[str]:
        return _compiles(v)


SlotKind = Literal["starter", "public_test", "hidden", "ref"]


class TemplateManifest(Schema):
    """템플릿 `templates/<직군>/<스택>/vN/manifest.yaml`."""

    template_id: str = Field(min_length=1)  # 예: backend/java-spring-boot3-jpa
    version: int = Field(ge=1)
    stack: StackInfo | None = None
    interfaces: list[InterfaceKind] = Field(min_length=1)
    workspace: str = "/workspace"
    commands: TemplateCommands | None = None
    # 오버레이 kind 별로 쓸 수 있는 경로 (glob, 프로젝트 기준). ref = ref_naive·ref_accepted
    fill_slots: dict[SlotKind, list[str]] = Field(default_factory=dict)
    locked: list[str] = Field(default_factory=list)  # 어느 오버레이도 덮어쓸 수 없음
    reserved: list[str] = Field(
        default_factory=list
    )  # hidden 외의 오버레이는 쓸 수 없음 (채점 자리)
    hidden_layout: HiddenLayout | None = None
    limits: TemplateLimits | None = None
    image: ImageLayers = Field(default_factory=ImageLayers)
    hidden_rules: HiddenRules = Field(default_factory=HiddenRules)
    naming: NamingRules = Field(default_factory=NamingRules)
    harness_params: list[HarnessParam] = Field(default_factory=list)
    failure_reasons: list[FailureReason] = Field(default_factory=list)
    stack_conditions: list[str] = Field(
        default_factory=list
    )  # 허용 설계가 이 스택에서 성립하는 조건 (§4.2)

    @model_validator(mode="after")
    def _unique(self) -> TemplateManifest:
        for label, names in (
            ("harness_params", [p.name for p in self.harness_params]),
            ("failure_reasons", [r.code for r in self.failure_reasons]),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"{label} 이름이 겹침")
        return self

    @property
    def ref(self) -> str:
        return f"{self.template_id}@v{self.version}"

    def param(self, name: str) -> HarnessParam | None:
        return next((p for p in self.harness_params if p.name == name), None)

    @property
    def reason_codes(self) -> set[str]:
        return {r.code for r in self.failure_reasons}
