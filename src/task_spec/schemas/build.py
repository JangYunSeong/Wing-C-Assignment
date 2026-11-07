"""공통 과제 빌드 — 명세 → 시작 코드 생성 파이프라인의 경계 모델 (docs/design/spec-to-code.md §2·§3·§4) [제안].

- `TaskContract`: 공개 계약. 지원자에게 보인다 (README "계약" 절). 시작 코드·숨은 테스트·참조 구현 생성기가 함께 기대는 유일한 기준점.
  "무엇을"만 — 트랜잭션 경계·잠금·캐시 같은 "어떻게"와 숨은 요구 핵심어는 담지 않는다 (starter/contract.py 가 검사).
- `HiddenPlan`: 숨은 테스트 생성기만 보는 운영 조건과 케이스. 지원자·시작 코드 생성기에 가지 않는다.
- `GeneratedFiles`: 모든 생성기의 출력 모양. 경로 규칙(fill_slots·잠금·상한)은 starter/overlay.py(#55)가 본다.
- `BuildState`: 빌드 그래프 State. 오버레이 **본문을 담지 않는다** — 파일은 starter_file 테이블(#52)에, State 에는 (kind, path, sha256) 만.
"""

from __future__ import annotations

import re
from operator import add
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.provenance import NodeError, Provenance
from task_spec.schemas.template import InterfaceKind

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
# 오버레이 파일 종류 — public_test 는 공개 테스트를 지원자에게 주지 않을 때 (spec-to-code §4.3 [제안])
FileKind = Literal["starter", "hidden", "public_test", "ref_naive", "ref_accepted"]
BuildStatus = Literal[
    "pending", "generating", "verifying", "review", "failed", "approved", "published"
]
GeneratorName = Literal["plan_contract", "gen_starter", "gen_hidden", "gen_naive", "gen_accepted"]

_REF = r"^[rp]\d{1,2}$"  # 공개 요구(r*)·공개 수용 기준(p*) — 계약 요소가 나온 곳


class ContractField(Schema):
    """요청·응답 본문·엔티티의 필드 1개. refs = 이 필드가 나온 공개 요구·공개 수용 기준 (양방향 대응, §3.3)."""

    name: str = Field(min_length=1, max_length=60)
    type: str = Field(
        min_length=1, max_length=60
    )  # 예: string · integer · datetime · enum(READY|SHIPPED)
    required: bool = True
    constraints: str | None = Field(default=None, max_length=200)
    refs: list[str] = Field(min_length=1)

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)


class ContractResponse(Schema):
    # HTTP 엔드포인트(메서드 있음)는 상태 코드 100~599, cli·file(메서드 없음)은 종료 코드 0~255 — Endpoint 가 확인
    status: int = Field(ge=0, le=599)
    description: str = Field(min_length=1, max_length=200)
    body: list[ContractField] = Field(default_factory=list)
    returns: str | None = Field(
        default=None,
        pattern=r"^m\d{1,2}(\[\])?$",
        description="본문이 엔티티 하나면 그 id (예: m1), 엔티티 목록이면 m1[] — 필드는 엔티티 정의를 따른다",
    )


class Endpoint(Schema):
    """공개 인터페이스 1개 — http 면 메서드·경로, cli 면 명령, file 이면 입출력 파일."""

    endpoint_id: str = Field(pattern=r"^e\d{1,2}$")
    method: HttpMethod | None = None  # http 에서만 (필수)
    path: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=200)
    headers: list[ContractField] = Field(default_factory=list)  # 요청 헤더 (예: X-Actor)
    query: list[ContractField] = Field(default_factory=list)  # 쿼리 매개변수
    request: list[ContractField] = Field(default_factory=list)  # 요청 본문
    responses: list[ContractResponse] = Field(min_length=1)
    refs: list[str] = Field(min_length=1)
    # 이 과제 전에 이미 있는 API 인가 — true 면 시작 코드가 지금 동작을 만들어 두고(새 요구가 더하는 동작은 빼고),
    # false 면 시작 코드는 자리만 둔다. 2026-11-07 알파 테스트: 명세 글에 `METHOD /path` 가 없어 기존 API 목록이 비자
    # 시작 코드 생성기가 범위를 짐작 — 1회차는 기존 API 를 비우고 새 조회 API 를 구현, 2회차는 새 기능까지 다 구현.
    existing: bool = False

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)

    @model_validator(mode="after")
    def _statuses(self) -> Endpoint:
        """2026-11-07 종이 단계: CLI 결과를 종료 코드(0·1)로 적자 HTTP 전용 상태(≥100)에 걸려 응답 전체가 거부됐다."""
        lo, hi, what = (100, 599, "HTTP 상태 코드") if self.method else (0, 255, "종료 코드")
        bad = [r.status for r in self.responses if not lo <= r.status <= hi]
        if bad:
            raise ValueError(f"{self.endpoint_id} 응답 {bad}: {what}는 {lo}~{hi}")
        return self


class Fixture(Schema):
    """입력 환경 (공개) — 시작 코드가 깔아 주는 DB·파일·서비스·브라우저 상태. 지원자도 본다.
    숨은 테스트가 채점 때 바꾸는 단계는 HiddenCase.setup (숨김) 에 따로 적는다."""

    fixture_id: str = Field(pattern=r"^f\d{1,2}$")
    kind: Literal["db", "file", "service", "browser", "env"]
    summary: str = Field(min_length=1, max_length=200)
    content: str = Field(
        default="", max_length=2000
    )  # 모양·값 (예: 데이터베이스·테이블 목록, 파일 형식)
    refs: list[str] = Field(min_length=1)

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)


class UiElement(Schema):
    """화면 요소 — 숨은 테스트·공개 테스트가 test id 로 찾는다."""

    test_id: str = Field(min_length=1, max_length=60)
    role: Literal["button", "input", "list", "item", "text", "status", "link", "form"]
    description: str = Field(min_length=1, max_length=200)
    refs: list[str] = Field(min_length=1)

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)


class StorageKey(Schema):
    """화면이 쓰는 브라우저 저장소 (이름·모양) — 숨은 테스트가 읽어 확인한다."""

    name: str = Field(min_length=1, max_length=80)
    kind: Literal["indexeddb", "localStorage", "sessionStorage", "cookie"]
    shape: str = Field(min_length=1, max_length=300)
    refs: list[str] = Field(min_length=1)

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)


class Screen(Schema):
    """화면 1개 (interface_kind ui). 2026-11-07 종이 단계: 계약에 화면을 적을 수 없어 서버 API 만 계약됐고,
    숨은 케이스는 '탭 4개'라 적어도 HTTP 로는 함정(탭마다 로컬 DB 쓰기)에 닿지 않았다."""

    screen_id: str = Field(pattern=r"^s\d{1,2}$")
    route: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=200)
    elements: list[UiElement] = Field(min_length=1, max_length=30)
    storage: list[StorageKey] = Field(default_factory=list, max_length=8)
    refs: list[str] = Field(min_length=1)

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)


class Entity(Schema):
    entity_id: str = Field(pattern=r"^m\d{1,2}$")
    name: str = Field(min_length=1, max_length=60)
    fields: list[ContractField] = Field(min_length=1)
    relations: list[str] = Field(default_factory=list)
    # DB 외래 키로 둘 관계만 — "필드 → 엔티티" (예: "orderId → Order"). 비우면 외래 키 없음 — 시작 코드는 여기 없는 외래 키를 만들지 않는다.
    # 2026-11-07 루프: 계약에 관계가 없는데 시작 코드가 감사 기록 → 배송 건 외래 키를 걸어, 없는 배송 건 요청의 기록이 거부됐다.
    foreign_keys: list[str] = Field(
        default_factory=list,
        description='DB 외래 키로 둘 관계만 "필드 → 엔티티" (예: "orderId → Order"). 없는 대상의 요청도 기록하는 엔티티의 대상 id 는 넣지 않는다',
    )

    @field_validator("foreign_keys")
    @classmethod
    def _fk(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if not re.match(r"^\s*\w+\s*(?:→|->)\s*\w+\s*$", x)]
        if bad:
            raise ValueError(f"foreign_keys 는 '필드 → 엔티티' 꼴: {bad}")
        return v

    def fk_pairs(self) -> list[tuple[str, str]]:
        return [tuple(x.strip() for x in re.split(r"→|->", fk)) for fk in self.foreign_keys]  # type: ignore[misc]


SeedScalar = str | int | bool


class SeedGroup(Schema):
    """시드 id 범위 하나와 그 행들의 처음 값 (예: id 1~150 은 status=READY)."""

    first: int = Field(ge=1)
    last: int = Field(ge=1)
    values: dict[str, SeedScalar] = Field(
        default_factory=dict,
        description='이 범위 행들이 처음에 갖는 필드 값 (예: {"status": "READY"}). id 필드는 넣지 않는다',
    )

    @model_validator(mode="after")
    def _order(self) -> SeedGroup:
        if self.first > self.last:
            raise ValueError(f"시드 범위 {self.first}~{self.last}: first 가 last 보다 큼")
        return self


class SeedRule(Schema):
    """시드 데이터 (가상 데이터만). id 1~rows 가 반드시 있고, groups 가 그 범위를 빈틈·겹침 없이 덮어 처음 값을 정한다
    (spec-to-code §10-16 — 숨은 테스트가 시작 코드를 보지 않고도 쓸 행과 처음 상태를 안다)."""

    entity_id: str = Field(pattern=r"^m\d{1,2}$")
    rows: int = Field(
        ge=0, le=5000, description="정확한 행 수 — id 1~rows. 처음에 비어 있는 엔티티는 0"
    )
    groups: list[SeedGroup] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)  # 그 밖의 값 규칙 (예: 송장 번호 형식)


class PublicTest(Schema):
    test_id: str = Field(pattern=r"^pt\d{1,2}$")
    ac_id: str = Field(pattern=r"^p\d{1,2}$")
    checks: str = Field(min_length=1, max_length=300)  # 무엇을 확인하나


class TaskContract(Schema):
    """공개 계약 — 지원자에게 보인다. 크기 상한 엔드포인트 1–6 · 엔티티 1–8 (60분 과제 기준 [미정])."""

    interface_kind: InterfaceKind
    endpoints: list[Endpoint] = Field(min_length=1, max_length=6)
    entities: list[Entity] = Field(min_length=1, max_length=8)
    seed: list[SeedRule] = Field(default_factory=list)  # DB 행을 바꾸는 과제만 (템플릿 seed_rows)
    fixtures: list[Fixture] = Field(default_factory=list, max_length=8)  # 그 밖의 입력 환경
    screens: list[Screen] = Field(default_factory=list, max_length=8)  # interface_kind ui 일 때만
    public_tests: list[PublicTest] = Field(min_length=1)
    covers: dict[str, list[str]] = Field(
        min_length=1,
        description='공개 요구 id → 그 요구를 담는 엔드포인트·엔티티·화면 id 목록 (예: {"r1": ["e1", "m1"]})',
    )

    @model_validator(mode="after")
    def _screens(self) -> TaskContract:
        if self.interface_kind == "ui" and not self.screens:
            raise ValueError("interface_kind ui 는 screens 가 하나 이상")
        if self.interface_kind != "ui" and self.screens:
            raise ValueError(f"interface_kind {self.interface_kind} 는 screens 를 쓰지 않음")
        return self


HarnessValue = int | float | str | list[int]


class CaseRows(Schema):
    """숨은 케이스 하나가 바꾸는 시드 행 — 수와 처음 값만 선언하고 id 범위는 코드가 나눈다 (starter/seed_rows.py)."""

    entity_id: str = Field(pattern=r"^m\d{1,2}$")
    rows: int = Field(
        ge=0, le=2000
    )  # 0 은 코드가 뺀다 (seed_rows.fit_seed) — 2026-11-07: LLM 이 빈 기록 엔티티를 rows 0 으로 적어 응답 전체가 거부됨
    initial: dict[str, SeedScalar] = Field(
        default_factory=dict,
        description="이 행들이 처음에 가져야 하는 값 — 시드 groups 의 values 와 맞아야 함",
    )
    burst: bool = Field(
        default=False,
        description="동시 요청 버스트 — 요청마다 다른 행 (rows ≥ 템플릿 burst_rows 조절값의 곱)",
    )


class HiddenCase(Schema):
    case_id: str = Field(pattern=r"^hc\d{1,2}$")
    ac_id: str = Field(pattern=r"^h\d{1,2}$")
    trap_id: str = Field(min_length=1)
    scenario: str = Field(min_length=1, max_length=300)
    expect: str = Field(min_length=1, max_length=300)  # 통과 조건
    intended_reason: str = Field(
        min_length=1
    )  # 흔한 구현이 실패할 때의 사유 코드 — 템플릿 failure_reasons 안
    data: list[CaseRows] = Field(default_factory=list)  # 바꾸는 시드 행 (읽기만 하면 비움)
    # 이 케이스에서 하네스가 입력 환경을 바꾸는 단계 (숨김 — 계약·README 에 안 감). 예: "sales DB 에 같은 이름 테이블 추가"
    setup: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=6
    )


class HiddenPlan(Schema):
    """숨은 테스트 생성기만 보는 계획 — 지원자·시작 코드 생성기에 가지 않는다."""

    harness: dict[str, HarnessValue] = Field(
        default_factory=dict,
        description='템플릿 조절값 이름 → 값 (예: {"pool_size": 8}) — 지원자에게 보이는 값은 자료에 적힌 값 그대로',
    )
    cases: list[HiddenCase] = Field(min_length=1)
    # 공개 조절값마다 그 값이 적힌 자료 문장을 그대로 — 코드가 자료에 있는지·값이 그 안에 있는지 확인 (starter/contract.py evidence_ok).
    # 자료에 없으면 "없음" — 그 조절값이 필수면 명세 결함. 2026-11-07 종이 단계: 템플릿 표기 패턴이 한 예시 표현에만 맞아 값이 있는데도 못 찾았다.
    evidence: dict[str, str] = Field(
        default_factory=dict,
        description='공개 조절값 이름 → 그 값이 적힌 자료·답 문장을 그대로 (예: {"pool_size": "db.maxConnections=20"}). 자료에 없으면 "없음"',
    )


class PlanContractInput(Schema):
    """plan_contract 프롬프트 변수 — 승인된 기능형 명세·템플릿에서 나온 글. 지원서는 쓰지 않는다 (불변식 3)."""

    template: str = Field(min_length=1)  # 템플릿 ref · 인터페이스 · 이름 규칙
    harness_params: str = Field(min_length=1)  # 조절값 줄 묶음
    failure_reasons: str = Field(min_length=1)  # 실패 사유 코드 줄 묶음
    title: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    requirements: str = Field(min_length=1)  # "r1 …" 줄 묶음
    acceptance_public: str = Field(min_length=1)  # "p1 …"
    deliverables: str
    traps: str = Field(min_length=1)  # "t1 …" — 숨은 요구 (HiddenPlan 만)
    acceptance_hidden: str = Field(min_length=1)  # "h1 (t1) …"
    materials: str  # 1단계 자료 — "[a2] 제목" + 본문
    clarifications: str  # 추가 요구 구체 답 — "[c1] 주제: 답"
    revision_note: str  # 재요청 사유 (없으면 "(없음)")


class PlanContractInputV3(PlanContractInput):
    """plan_contract v3~ — 시드 행 수 규칙을 더한다 (starter/seed_rows.py 와 같은 값)."""

    seed_rules: str = Field(min_length=1)


class PlanContractOutput(Schema):
    """plan_contract 출력 — 공개 계약과 숨은 테스트 계획을 한 번에."""

    contract: TaskContract
    hidden_plan: HiddenPlan


class GenStarterInput(Schema):
    """gen_starter 프롬프트 변수 — 공개 정보만 (숨은 요구·HiddenPlan·허용 설계 없음, spec-to-code §4)."""

    stack: str = Field(min_length=1)
    template_files: str = Field(min_length=1)  # 템플릿 뼈대 파일 경로·내용
    slots: str = Field(min_length=1)  # 쓸 수 있는 경로·잠긴 파일
    existing_apis: str  # 상황·요구에 나온 기존 API (완전히 동작해야 함)
    title: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    requirements: str = Field(min_length=1)
    acceptance_public: str = Field(min_length=1)
    contract: str = Field(min_length=1)  # 공개 계약 (README 와 같은 글)
    revision_note: str


class GenHiddenInput(Schema):
    """gen_hidden 프롬프트 변수 — 계약·HiddenPlan·숨은 요구, 하네스 API (시작 코드·참조 구현 본문 없음)."""

    stack: str = Field(min_length=1)
    harness_api: str = Field(min_length=1)  # 템플릿 hidden/HARNESS.md
    slots: str = Field(min_length=1)
    contract: str = Field(min_length=1)
    hidden_plan: str = Field(min_length=1)  # 조절값 + 케이스 목록
    traps: str = Field(min_length=1)
    acceptance_hidden: str = Field(min_length=1)
    failure_reasons: str = Field(min_length=1)
    revision_note: str


class GenRefInput(Schema):
    """gen_naive · gen_accepted 프롬프트 변수 — 계약·시작 코드 + 구현 목표 (숨은 테스트 본문 없음)."""

    stack: str = Field(min_length=1)
    slots: str = Field(min_length=1)
    contract: str = Field(min_length=1)
    starter_files: str = Field(min_length=1)  # 시작 코드 오버레이 경로·내용
    requirements: str = Field(min_length=1)
    acceptance_public: str = Field(min_length=1)
    goal: str = Field(min_length=1)  # 흔한 구현 설명 또는 허용 설계 1건
    conditions: str  # 지킬 것 — 하나만 놓친 판의 지킬 숨은 요구 / 허용 설계의 스택 조건
    revision_note: str


class GenStarterInputV2(GenStarterInput):
    """gen_starter v3~ — 수리면 자기 이전 출력 (2026-11-07: 처음부터 다시 써 한 줄 오류가 다른 문제로 바뀜)."""

    previous_files: str = "(없음)"


class GenHiddenInputV2(GenHiddenInput):
    """gen_hidden v2~ — 수리면 자기 이전 출력."""

    previous_files: str = "(없음)"


class GenRefInputV2(GenRefInput):
    """gen_naive·gen_accepted v2~ — 수리면 자기 이전 출력."""

    previous_files: str = "(없음)"


class GeneratedFile(Schema):
    path: str = Field(min_length=1, max_length=300)
    content: str = Field(max_length=200_000)

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        # 기본 모양만 — fill_slots·잠금·상한은 starter/overlay.py (#55)
        if v.startswith("/") or "\\" in v or any(ch in v for ch in "\t\n\r"):
            raise ValueError("상대 경로만, 역슬래시·탭·줄바꿈 금지")
        if ".." in v.split("/"):
            raise ValueError("경로에 .. 금지")
        return v


class GeneratedFiles(Schema):
    """모든 생성기의 출력 — {files: [{path, content}]}."""

    files: list[GeneratedFile] = Field(min_length=1, max_length=80)

    @field_validator("files")
    @classmethod
    def _unique(cls, v: list[GeneratedFile]) -> list[GeneratedFile]:
        paths = [f.path for f in v]
        if len(set(paths)) != len(paths):
            raise ValueError("같은 경로가 두 번 나옴")
        return v


class FileRef(Schema):
    """State 에 남기는 오버레이 파일 참조 — 본문 없음."""

    kind: FileKind
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class RepairRecord(Schema):
    """수리 이력 1건 — 어떤 게이트 실패를 어느 생성기에 배정했고 결과가 어땠나 (코드 본문 없음)."""

    gate: str = Field(min_length=1)  # 예: "③" · "contract" · "static"
    target: GeneratorName
    reason: str = Field(min_length=1, max_length=300)  # 사유 코드·케이스 id
    result: Literal["regenerated", "failed", "skipped"]


class BuildState(BaseModel):
    """공통 과제 빌드 1건의 State. thread_id = build_id. 공통 과제 판(common_id, revision)마다 한 번."""

    model_config = ConfigDict(extra="forbid")

    build_id: str = Field(min_length=1)
    common_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    ontology_version: str = Field(min_length=1)
    status: BuildStatus = "pending"
    template_ref: str | None = None  # id@vN
    contract: TaskContract | None = None
    hidden_plan: HiddenPlan | None = None
    files: list[FileRef] = Field(default_factory=list)  # 교체 — 생성·수리 때마다 최신 목록
    regen_counts: dict[str, int] = Field(default_factory=dict)  # 생성기별 재생성 횟수 (예산, §7)
    repairs: list[RepairRecord] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)
    provenance: Annotated[list[Provenance], add] = Field(default_factory=list)
    errors: Annotated[list[NodeError], add] = Field(default_factory=list)


def _check_refs(v: list[str]) -> list[str]:
    bad = [r for r in v if not re.match(_REF, r)]
    if bad:
        raise ValueError(f"refs 는 공개 요구(r*)·공개 수용 기준(p*) id 만: {bad}")
    return v
