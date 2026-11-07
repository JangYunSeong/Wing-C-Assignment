"""시작 코드 템플릿 manifest 중 생성 파이프라인이 쓰는 부분 (docs/design/spec-to-code.md §4.1) [제안].

이 모듈은 공개 계약·HiddenPlan 결정적 검사(§3.3)에 필요한 필드만 정의한다:
- 하네스 조절값 `harness_params` (명세 자료에서 값을 뽑는 표기 패턴·시작 코드 설정 키 포함)
- 의도한 실패 사유 코드 `failure_reasons`
- 계약 이름 규칙 `naming`, 허용 인터페이스 `interfaces`, 허용 설계의 스택 조건 `stack_conditions`

빌드 명령·`fill_slots`·잠금 파일·상한·이미지 같은 나머지 필드는 템플릿 레지스트리(starter/templates.py, #55)에서 더한다.
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
    # 시작 코드 설정 파일에서 이 값을 담는 키 (§6 정적 검사 — 운영 값이 시작 코드에 박혔는지)
    config_keys: list[str] = Field(default_factory=list)

    @field_validator("spec_patterns")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for p in _compiles(v):
            if re.compile(p).groups < 1:
                raise ValueError(f"표기 패턴에 값 그룹이 없음: {p!r}")
        return v

    @model_validator(mode="after")
    def _visible_needs_pattern(self) -> HarnessParam:
        if self.visible and not self.spec_patterns:
            raise ValueError(
                f"{self.name}: visible 조절값은 표기 패턴(spec_patterns)이 1개 이상 필요"
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
    entity: str = r"^[A-Z][A-Za-z0-9]*$"
    field: str = r"^[a-z][A-Za-z0-9]*$"

    @field_validator("path", "entity", "field")
    @classmethod
    def _rx(cls, v: str) -> str:
        return _compiles([v])[0]


class TemplateManifest(Schema):
    """템플릿 `templates/<직군>/<스택>/vN/manifest.yaml` 의 생성 파이프라인용 부분."""

    template_id: str = Field(min_length=1)  # 예: backend/java-spring-boot3-jpa
    version: int = Field(ge=1)
    interfaces: list[InterfaceKind] = Field(min_length=1)
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
