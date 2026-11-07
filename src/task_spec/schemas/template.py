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

InterfaceKind = Literal[
    "http", "cli", "file", "ui"
]  # ui = 화면 (계약 screens), 서버 API 는 endpoints
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


TableStyle = Literal["snake", "snake_plural", "app_lower", "as_is", "none"]
IdentifierCase = Literal["lower", "upper"]


class TableNaming(Schema):
    """엔티티 이름 → DB 테이블 이름 (스택 관례, spec-to-code §3.3). `DeliveryAuditLog` 기준 (2026-11-07 문서·소스 확인):

    - snake — Spring Boot JPA 기본(`CamelCaseToUnderscoresNamingStrategy`, 소문자): `delivery_audit_log`
    - snake_plural — Rails ActiveRecord: `delivery_audit_logs`
    - app_lower — Django: `<앱>_deliveryauditlog` (클래스 이름 소문자, 단어 사이 `_` 없음)
    - as_is — Hibernate 기본(Spring 없이, 이름 그대로·따옴표 없음) · Prisma(`@@map` 없을 때, 따옴표로 감쌈): `DeliveryAuditLog`
    - none — 테이블 없음 (프론트엔드·파일형)
    따옴표 없이 쓰는 이름은 DB 가 접는다 (PostgreSQL 소문자, Oracle 등 표준대로 대문자) — `unquoted_case` 로 적는다.
    """

    style: TableStyle
    app_label: str | None = None  # app_lower 의 앱 이름 (Django)
    quoted: bool = False  # ORM 이 이름을 따옴표로 감싸 대소문자를 지킴 (Prisma)
    unquoted_case: IdentifierCase | None = None  # 따옴표 없는 이름을 DB 가 접는 방향 — 모르면 비움
    # 시작 코드에서 테이블 선언을 찾는 정규식 (그룹 1 = 이름, 대소문자 무시) — 예: CREATE TABLE, @Table(name=…)
    declare_patterns: list[str] = Field(default_factory=list)
    # 시작 코드에서 외래 키 선언을 찾는 정규식 (그룹 1 = 컬럼 이름, 또는 관계 애너테이션 이름) — 계약 foreign_keys 밖이면 재요청
    fk_patterns: list[str] = Field(default_factory=list)
    # 규칙으로 나온 이름이 이 SQL 예약어면 복수형으로 (order → orders — Spring 에서 흔한 @Table(name="orders") 관례). 대소문자 무시
    reserved: list[str] = Field(default_factory=list)

    @field_validator("declare_patterns", "fk_patterns")
    @classmethod
    def _rx(cls, v: list[str]) -> list[str]:
        return _compiles(v)

    @model_validator(mode="after")
    def _app(self) -> TableNaming:
        if self.style == "app_lower" and not self.app_label:
            raise ValueError("app_lower 는 app_label 이 필요함")
        return self


class NamingRules(Schema):
    """계약 이름 규칙 (템플릿 스택의 관례)."""

    path: str = r"^/[a-z0-9/{}\-]*$"  # http 경로
    header: str = r"^[A-Z][A-Za-z0-9]*(-[A-Z][A-Za-z0-9]*)*$"  # http 헤더 (예: X-Actor)
    entity: str = r"^[A-Z][A-Za-z0-9]*$"
    field: str = r"^[a-z][A-Za-z0-9]*$"
    test_id: str = r"^[a-z][a-z0-9-]*$"  # 화면 요소의 test id (ui)
    table: TableNaming | None = None  # 엔티티 → 테이블 이름 (없으면 계약에 테이블을 적지 않음)

    @field_validator("path", "header", "entity", "field", "test_id")
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


class InitScripts(Schema):
    """앱이 시작할 때 실행하는 SQL 스크립트 — 실행할 문장이 없으면(주석뿐) 프레임워크가 부팅에 실패하는 스택.

    2026-11-07 알파 테스트 (새 공고): 시드 0행이라 시작 코드가 data.sql 을 주석만 남겨 Spring 이 빈 스크립트로 부팅 실패,
    재생성 예산을 다 씀. 오버레이가 이 파일들을 실행할 문장 없이 쓰면 코드가 noop 문장을 붙인다 (starter/init_scripts.py).
    """

    paths: list[str] = Field(min_length=1)  # 프로젝트 기준 상대 경로
    noop: str = Field(min_length=1)  # 붙일 문장 (예: "SELECT 1;")


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
    # 단언의 사유 표시에서 (사유 코드, 문구)를 뽑는 정규식 (그룹 1·2). 한 케이스 파일에 같은 쌍이 둘 이상이면 사유가 잘못 붙었을 수 있다
    # (2026-11-07 루프: 'FAILURE 기록 있음' 단언에 그 아래 'SUCCESS 없음' 단언과 같은 사유·문구가 붙어 흔한 구현이 의도한 사유로 실패한 것처럼 보였다)
    reason_labels: list[str] = Field(default_factory=list)
    # 케이스의 문자열 상수 중 SQL 을 가리키는 것에서 테이블 이름을 뽑는 정규식 (그룹 1, 대소문자 무시) — 계약 테이블만 써야 함.
    # 2026-11-07 생성 스모크: 오류 주입 정규식이 테이블을 경로에서 짐작(`deliveries`, 실제 `delivery`)해 주입이 안 걸림
    table_refs: list[str] = Field(default_factory=list)
    string_literal: str = r'"((?:[^"\\\n]|\\.)*)"'  # 케이스 파일의 문자열 상수 (그룹 1 = 내용)
    # 케이스가 HTTP API 를 부르는 곳 (정규식, 매치 끝 = 경로 인자 시작, 그룹 method — 없으면 GET).
    # 경로 인자를 `{}` 자리로 정규화해 공개 계약의 API 와 대조한다 (2026-11-07 스모크: 계약에 없는 GET 을 불러 항상 404)
    http_calls: list[str] = Field(default_factory=list)
    # 응답 JSON 의 Map 값을 단언에 그대로 넣는 곳 (그룹 1 = 필드 이름) — 계약에서 정수 형식인 필드면 다시 쓰게 한다.
    # 2026-11-07 알파 테스트: assertThat(log.get("transactionId")).isEqualTo(txId) — Integer 105 ≠ 105L 로 모든 구현이 실패.
    # HARNESS.md 에 규칙이 있었는데도 어겨서 문서 대신 검사로.
    raw_number_asserts: list[str] = Field(default_factory=list)

    @field_validator(
        "forbidden_imports",
        "reason_patterns",
        "http_calls",
        "table_refs",
        "reason_labels",
        "raw_number_asserts",
    )
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
    # 버스트 케이스(HiddenCase.data[].burst)가 요청마다 다른 행을 쓰려면 필요한 행 수 = 이 조절값들의 곱 (예: concurrency × bursts)
    burst_rows: list[str] = Field(default_factory=list)
    # 숨은 테스트가 계약 시드의 DB 행을 바꾸는 과제인가 — 아니면(cli·file·ui) 시드·행 배정 대신 입력 환경(fixtures)과 케이스 setup
    seed_rows: bool = True
    init_scripts: InitScripts | None = None

    @model_validator(mode="after")
    def _unique(self) -> TemplateManifest:
        for label, names in (
            ("harness_params", [p.name for p in self.harness_params]),
            ("failure_reasons", [r.code for r in self.failure_reasons]),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"{label} 이름이 겹침")
        for name in self.burst_rows:
            param = self.param(name)
            if param is None or param.kind != "int":
                raise ValueError(f"burst_rows 의 {name} 이 int 조절값이 아님")
        return self

    @property
    def ref(self) -> str:
        return f"{self.template_id}@v{self.version}"

    def param(self, name: str) -> HarnessParam | None:
        return next((p for p in self.harness_params if p.name == name), None)

    @property
    def reason_codes(self) -> set[str]:
        return {r.code for r in self.failure_reasons}
