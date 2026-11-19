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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from task_spec.schemas.base import Schema
from task_spec.schemas.provenance import NodeError, Provenance
from task_spec.schemas.template import FixtureKind, InterfaceKind

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
# 오버레이 파일 종류 — public_test 는 공개 테스트를 지원자에게 주지 않을 때 (spec-to-code §4.3 [제안])
# ref_variant — 공개 테스트 변별 관문의 변형(정답 구현에서 공개 수용 기준 하나를 어김, #164). 검증에만 쓰고 저장하지 않는다
FileKind = Literal["starter", "hidden", "public_test", "ref_naive", "ref_accepted", "ref_variant"]
BuildStatus = Literal[
    "pending", "generating", "verifying", "review", "failed", "approved", "published"
]
GeneratorName = Literal[
    "plan_contract",
    "gen_public",
    "gen_starter",
    "gen_hidden",
    "gen_naive",
    "gen_accepted",
    "gen_variant",
]

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

    @model_validator(mode="before")
    @classmethod
    def _description(cls, data: Any) -> Any:
        """필드에 붙인 description — 응답(responses)에 있는 칸이라 모델이 필드에도 붙인다 (2026-11-09 측정: plan_contract v16
        6번 중 1번이 이것 하나로 스키마 위반, 재요청마다 계약 전체를 다시 써 1분 반~2분). 제약이 비면 그 자리로, 있으면 버린다."""
        if isinstance(data, dict) and "description" in data:
            rest = {k: v for k, v in data.items() if k != "description"}
            text = data["description"]
            if not rest.get("constraints") and isinstance(text, str) and text.strip():
                rest["constraints"] = text[:200]
            return rest
        return data

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)


class HeaderField(ContractField):
    """요청 헤더 1개 — 필드에 '요청마다 새 값인가'를 더한다 (#181).

    2026-11-10 retrydup-c: 요청 번호 X-Request-Id 를 도우미가 상수 하나로 줘 모든 테스트가 같은 번호를 공유 — 명세대로 번호로 중복을 막은
    허용 구현이 떨어졌다. 예전엔 이름·제약 글의 낱말(request-id·멱등·uuid …)로 짐작해 X-Nonce·X-Dedup-Key·'고유·중복 불가'는 놓쳤다 —
    계약이 칸으로 정한다. 비어 있으면 계약 검사가 재요청한다 (권한 칸 auth 의 헤더는 권한 규칙이 값을 정하므로 묻지 않는다).
    이름·제약이 요청 하나를 가리키면(request-id·멱등·nonce …) 칸이 false 여도 코드가 true 로 맞춘다 (header_values.fit_per_request —
    2026-11-15 알파: X-Request-Id 를 false 로 적어 허용 구현이 공개 테스트에서 떨어졌다)."""

    per_request: bool | None = Field(
        default=None,
        description="요청마다 새 값이어야 하면 true (요청 번호·멱등 키·nonce 처럼 요청 하나를 가리키는 값 — 테스트가 요청마다 새로 만든다), "
        "여러 요청이 같은 값을 써도 되면 false (요청자·클라이언트 이름 등)",
    )


class ContractResponse(Schema):
    # HTTP 엔드포인트(메서드 있음)는 상태 코드 100~599, cli·file(메서드 없음)은 종료 코드 0~255 — Endpoint 가 확인
    status: int = Field(ge=0, le=599)
    description: str = Field(min_length=1, max_length=200)
    body: list[ContractField] = Field(default_factory=list)
    returns: str | None = Field(
        default=None,
        pattern=r"^[mv]\d{1,2}(\[\])?$",
        description="본문이 엔티티·응답 모양 하나면 그 id (예: m1·v1), 목록이면 m1[]·v1[] — 칸은 그 정의를 따른다",
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_refs(cls, data: Any) -> Any:
        # plan_contract v7 은 "모든 요소에 refs" 라 응답 묶음에도 붙이는 답이 있다 — 출처는 엔드포인트·필드 refs 가 맡으므로 버린다
        if isinstance(data, dict) and "refs" in data:
            return {k: v for k, v in data.items() if k != "refs"}
        return data


class AuthRule(Schema):
    """권한 규칙 칸 — 권한 실패(401·403)가 있는 엔드포인트만. 코드가 요청자 행을 시드에 덧붙이고(통과용·거절용) 그 행에서
    헤더 통과·거절 값과 규칙 문장을 만든다 (starter/auth_rule.py). StateRule 과 같은 방식.

    2026-11-09 알파 smilegate_2026#002: 같은 명세의 계약 14개가 권한을 4가지로 정했고(8개는 '요청자 식별자'뿐), 공개 테스트 예시의
    통과 값이 시드와 맞지 않아(통과하는 등급의 행이 없음) 계약이 거절되거나 모든 요청이 403 에서 끝났다."""

    kind: Literal["role", "fixed", "other"] = Field(
        description="role: 요청자가 엔티티 한 행이고 그 행의 필드 값으로 가름 · fixed: 정해진 헤더 값만 통과 · other: 그 밖 (글로)"
    )
    header: str = Field(
        min_length=1,
        max_length=60,
        description="요청자를 나타내는 요청 헤더 이름 — 이 엔드포인트 headers 에 있어야 함",
    )
    denied_status: int = Field(
        ge=401,
        le=403,
        description="권한이 없을 때 응답 코드 (401 또는 403) — 이 엔드포인트 responses 에 있어야 함",
    )
    entity: str | None = Field(
        default=None, pattern=r"^m\d{1,2}$", description="role: 요청자 엔티티 id (예: m1)"
    )
    identifies: str | None = Field(
        default=None,
        max_length=60,
        description="role: 헤더 값이 가리키는 그 엔티티 필드 (예: id, memberId) — 행마다 다른 값이어야 함",
    )
    attribute: str | None = Field(
        default=None, max_length=60, description="role: 권한을 가르는 그 엔티티 필드 (예: rank)"
    )
    allowed: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list,
        max_length=20,
        description="role: 통과하는 attribute 값 · fixed: 통과하는 헤더 값",
    )
    order: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list,
        max_length=20,
        description="role 등급 비교: attribute 값의 순서 (낮은 것 → 높은 것). above 를 쓸 때만",
    )
    above: Literal["requested", "target"] | None = Field(
        default=None,
        description="role 등급 비교: 요청자 attribute 가 order 에서 이것보다 높아야 통과 — requested(요청한 새 값)·target(대상 행의 지금 값)",
    )
    or_equal: bool = Field(default=False, description="role 등급 비교: 같은 등급도 통과면 true")
    same_as_path: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list,
        max_length=4,
        description="role: 경로 변수와 같아야 통과하는 요청자 필드 (예: 같은 길드 — guildId). 없으면 비움",
    )
    description: str | None = Field(
        default=None,
        max_length=200,
        description="other: 규칙 한 줄 (자동 보장 안 됨 — 사람이 확인)",
    )
    # 코드가 채운다 (fit_auth) — 덧붙인 요청자 행 id 와 그 행의 헤더 값. LLM 출력 스키마에는 보이지 않는다
    rows: SkipJsonSchema[list[int]] = Field(default_factory=list)
    ok_value: SkipJsonSchema[str | None] = None
    denied_value: SkipJsonSchema[str | None] = None


class Endpoint(Schema):
    """공개 인터페이스 1개 — http 면 메서드·경로, cli 면 명령, file 이면 입출력 파일."""

    endpoint_id: str = Field(pattern=r"^e\d{1,2}$")
    method: HttpMethod | None = None  # http 에서만 (필수)
    path: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=200)
    headers: list[HeaderField] = Field(default_factory=list)  # 요청 헤더 (예: X-Actor)
    query: list[ContractField] = Field(default_factory=list)  # 쿼리 매개변수
    request: list[ContractField] = Field(default_factory=list)  # 요청 본문
    responses: list[ContractResponse] = Field(min_length=1)
    refs: list[str] = Field(min_length=1)
    # 이 과제 전에 이미 있는 API 인가 — true 면 시작 코드가 지금 동작을 만들어 두고(새 요구가 더하는 동작은 빼고),
    # false 면 시작 코드는 자리만 둔다. 2026-11-07 알파 테스트: 명세 글에 `METHOD /path` 가 없어 기존 API 목록이 비자
    # 시작 코드 생성기가 범위를 짐작 — 1회차는 기존 API 를 비우고 새 조회 API 를 구현, 2회차는 새 기능까지 다 구현.
    existing: bool = False
    auth: AuthRule | None = Field(
        default=None,
        description="권한 실패(401·403)를 내는 엔드포인트만: 누가 통과하는지 (AuthRule). 그 밖은 null",
    )

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)

    def success(self) -> ContractResponse | None:
        """성공 응답 — 처음 나오는 2xx (201 만 선언한 생성 API 도). 도우미·예시·시드 배정이 모두 이것으로 고른다 (#172:
        Java 도우미만 200 을 봐서 201 만 선언한 엔드포인트에 본문 함수가 생기지 않았다)."""
        return next((r for r in self.responses if 200 <= r.status < 300), None)

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
    kind: FixtureKind
    summary: str = Field(min_length=1, max_length=200)
    content: str = Field(
        default="", max_length=2000
    )  # 모양·값 (예: 데이터베이스·테이블 목록, 파일 형식)
    # 2026-11-15 #187 데이터 알파(woori001): 입력 위치가 자유 글뿐이라 검사가 날짜 폴더를 표기(`YYYY-MM-DD/` 꼴)로 짐작했고,
    # `{YYYY-MM-DD}/` 로 바르게 적은 계약을 재요청했다 — 위치를 칸으로, 마디는 명령 옵션으로 잇는다
    path: str | None = Field(
        default=None,
        max_length=200,
        description="kind file 만: 위치 패턴 — 명령 옵션을 {옵션 이름} 으로 (예: {raw_dir}/{run_date}/*.jsonl). "
        "받은 날짜별 폴더면 그 마디가 실행 날짜 옵션(type date)",
    )
    refs: list[str] = Field(min_length=1)

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)


# 화면 요소 역할의 흔한 다른 이름 → UiElement.role 목록의 값 (Testing Library 로 찾는 뜻이 같은 것만)
ROLE_ALIASES: dict[str, str] = {
    "spinbutton": "input",
    "slider": "input",
    "number": "input",
    "textarea": "textbox",
    "searchbar": "searchbox",
    "select": "combobox",
    "dropdown": "combobox",
    "listitem": "item",
    "grid": "table",
    "gridcell": "cell",
    "columnheader": "cell",
    "rowheader": "cell",
    "img": "text",
    "image": "text",
    "label": "text",
    "paragraph": "text",
    "spinner": "progressbar",
    "loading": "progressbar",
    "toast": "alert",
    "alertdialog": "dialog",
    "modal": "dialog",
    "nav": "navigation",
    "section": "region",
    "group": "region",
    "menubar": "menu",
}


class UiElement(Schema):
    """화면 요소 — 숨은 테스트·공개 테스트가 test id 로 찾는다."""

    test_id: str = Field(min_length=1, max_length=60)
    # 처음 8개는 종이 단계의 추상 역할, 뒤는 테스트가 요소를 찾는 ARIA 역할 (Testing Library getByRole) — 2026-11-09 React 템플릿:
    # 손 예시 숨은 테스트가 combobox·option·alert 로 판정하는데 계약에 적을 수 없었다.
    role: Literal[
        "button",
        "input",
        "list",
        "item",
        "text",
        "status",
        "link",
        "form",
        "textbox",
        "searchbox",
        "combobox",
        "listbox",
        "option",
        "checkbox",
        "radio",
        "switch",
        "tab",
        "tabpanel",
        "dialog",
        "alert",
        "table",
        "row",
        "cell",
        "heading",
        "progressbar",
        "menu",
        "menuitem",
        "navigation",
        "region",
    ]
    description: str = Field(min_length=1, max_length=200)
    # 조작 방법 — 지원자가 이 요소를 어떻게 다루면 무엇이 일어나는지, 서버 요청이 나가는 시점까지 (2026-11-09 React 알파: 수량 입력의
    # 저장 시점(입력마다·포커스 이동·Enter)이 계약에 없어 테스트와 구현이 서로 다르게 짐작 — 모든 구현에서 '요청이 오지 않음')
    action: str | None = Field(
        default=None,
        max_length=200,
        description="조작하는 요소(버튼·입력 등)는 필수 — 무엇을 하면 무엇이 일어나고 서버 요청이 언제 나가는지 한 줄 "
        "(예: '클릭하면 수량 1 증가 — 바로 저장 요청', '값을 바꾸고 포커스를 옮기면 저장 요청')",
    )
    # 고를 수 있는 값이 정해진 요소(선택 목록·라디오·탭)의 값 — 테스트는 이 값(도우미 OPTIONS)으로 고르고 시작 코드는 이 값을 그린다.
    # 2026-11-10 프론트 알파(fefix 008d6314): 값이 설명 글에만 있어('캘리브레이션·센서 등') 숨은 테스트가 첫 항목(안내 문구, 빈 값)을 골라,
    # 필수 입력을 검사하는 허용 구현만 저장하지 않아 떨어졌다.
    options: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list,
        max_length=20,
        description="고를 수 있는 값이 정해진 요소(선택 목록·라디오·탭)만 — 화면에 보이는 값 그대로 (안내 문구 '선택하세요' 는 넣지 않는다)",
    )
    refs: list[str] = Field(min_length=1)

    @field_validator("role", mode="before")
    @classmethod
    def _role_alias(cls, v: object) -> object:
        """흔한 ARIA·HTML 이름은 목록의 같은 뜻 역할로 받는다 — 2026-11-10 프론트 알파 fe7-dup: 목록 밖 역할을 계약 3번이 모두
        반복해 거절됐다 (재요청이 무엇이 틀렸는지 몰랐다). 목록에 없는 다른 값은 그대로 형식 오류."""
        if isinstance(v, str):
            key = v.strip().lower()
            return ROLE_ALIASES.get(key, key)
        return v

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
    # 2026-11-09 React 알파(naver_2026#003): 목록 화면이 서버를 부르는지 계약에 없어 공개 테스트는 '들어오면 GET',
    # 오프라인 우선 구현 4개는 모두 로컬 저장소만 읽어 '요청이 오지 않음'(HARNESS_ERR)으로 함께 떨어졌다
    on_load: str | None = Field(
        default=None,
        max_length=300,
        description="화면에 들어올 때 무엇을 읽고(로컬 저장소·서버) 어떤 서버 요청이 나가는지 한 줄. 서버 요청이 없으면 '서버 요청 없음'",
    )
    refs: list[str] = Field(min_length=1)

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)


class StateRule(Schema):
    """상태를 가진 엔티티의 상태 규칙 — 허용 값·허용 전이·거절 코드. 코드가 상태 필드 constraints 문장과 테스트 도우미 상수
    (NEXT_OK_·NEXT_BAD_)를 만든다 (starter/state_rule.py). 2026-11-08 상류 평가: 계약 첫 답 9개 중 5개가 전이를 빠뜨렸고
    ("허용 상태 값 중 하나"), 자유 글의 'A→B' 를 정규식으로 찾던 재요청은 2회에도 같은 답이 남았다 — 정해진 칸으로 받는다."""

    field: str = Field(
        min_length=1, max_length=60, description="상태 필드 이름 — 이 엔티티 fields 에 있어야 함"
    )
    values: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        min_length=2, max_length=20, description="상태 값 전부 (시드·숨은 케이스의 처음 값 포함)"
    )
    transitions: list[str] = Field(
        min_length=1,
        max_length=40,
        description='허용 전이만 "A→B" 꼴로 빠짐없이 (예: ["READY→SHIPPED", "SHIPPED→DELIVERED"]) — 나머지 전이는 거절',
    )
    rejected_status: int = Field(
        ge=400,
        le=499,
        description="허용되지 않은 전이 요청의 응답 코드 — 계약 responses 에 있어야 함",
    )

    @field_validator("transitions")
    @classmethod
    def _arrows(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if not re.match(r"^\s*[\w-]+\s*(?:→|->)\s*[\w-]+\s*$", x)]
        if bad:
            raise ValueError(f"transitions 는 'A→B' 꼴: {bad}")
        return v

    def pairs(self) -> list[tuple[str, str]]:
        return [tuple(x.strip() for x in re.split(r"→|->", t)) for t in self.transitions]  # type: ignore[misc]


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
    # 이 과제 전에 이미 있는 엔티티(기존 API 가 다루는 테이블)인가 — false 면 이 과제에서 새로 만드는 것(기록·이력). 숨은 케이스 data 는
    # 이미 있는 엔티티에만 (2026-11-07 알파 3×3: 계약이 대상 엔티티 대신 감사 기록에 시드 516행을 넣고 숨은 케이스가 그 행을 '바꿈').
    existing: bool = False
    # 상태를 바꾸는 요청이 있는 엔티티만 — 없으면 null (StateRule)
    state: StateRule | None = Field(
        default=None,
        description="상태를 바꾸는 요청이 있는 엔티티만: 상태 값 전부·허용 전이·거절 코드. 그 밖의 엔티티는 null",
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


class AppSetting(Schema):
    """애플리케이션 설정 1개 — 구현이 이 키에서 읽어야 하는 값 (예: 목록 한 번에 돌려줄 최대 개수). 공개 계약 — 키·뜻·기본값이 README 에 보인다.

    2026-11-13 #161: 표시 건수를 '설정에 따름'이라 해 놓고 계약에 설정 칸이 없어, 시작 코드 설정 파일에 LLM 이 키를 그때그때 넣고
    공개 테스트는 시드 행 수(제한 이하)에 기대 '≤ 제한'·'= 제한'을 단언했다 — 제한 없는 구현·값을 코드에 박은 구현도 통과.
    계약에 키를 두면 테스트가 그 키를 다른 값(도우미 SETTING_…_PROBE)으로 덮어 '정확히 그 값만큼'을 단언할 수 있다."""

    key: str = Field(
        pattern=r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+$",
        max_length=80,
        description="스프링 속성 키, 점 표기 (예: marketplace.listing.page-size)",
    )
    type: Literal["int"] = "int"
    default: int = Field(
        ge=1, le=100_000, description="기본값 — 시작 코드 설정 파일에 이 값으로 둔다"
    )
    meaning: str = Field(
        min_length=1,
        max_length=120,
        description="무엇을 정하는 값인가 (예: 목록 한 번에 돌려줄 최대 개수)",
    )
    refs: list[str] = Field(
        min_length=1, description="이 설정을 따르라는 공개 요구(r*)·공개 수용 기준(p*)"
    )

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return _check_refs(v)


class ResponseShape(Schema):
    """응답에만 있는 모양 — 테이블이 아니다 (저장·시드·외래 키 없음). 다른 엔티티에서 읽는 값(조인)이나 집계를 담는 목록 항목·상세 응답.

    2026-11-12 행마다 조회 알파: 계약에 '응답에만 있는 모양'을 적을 자리가 없어, 고객 이름·상품 수를 주문 테이블 칸·시드 값으로 복사하거나
    (칸만 읽으면 숨은 요구가 사라짐) 응답 모양을 빈 테이블로 만들었다(order_list_item). 엔티티(entities)는 테이블만 — 시드·테이블 이름·외래 키를
    읽는 곳은 그대로 두고, 타입·예시·README 처럼 모양을 읽는 곳만 이것도 읽는다 (TaskContract.data_shapes)."""

    shape_id: str = Field(pattern=r"^v\d{1,2}$")  # v = view (s 는 화면 id)
    name: str = Field(min_length=1, max_length=60)
    fields: list[ContractField] = Field(
        min_length=1,
        description="응답 칸 — 다른 엔티티에서 읽거나 계산하는 칸은 constraints 에 출처를 적는다 (예: 'Customer.name — 주문의 customerId 로')",
    )
    # 2026-11-14 #185 알파: 모양에 '어느 엔티티 행인가' 칸이 없어 공개 테스트 rows 에 조인 원천(고객·서류) 행이 섞여도 코드가 가릴 수 없었다
    row_of: str | None = Field(
        default=None,
        pattern=r"^m\d{1,2}$",
        description="이 모양 한 건(목록이면 항목 하나)이 어느 테이블 엔티티의 한 행인가 — 그 엔티티 id (예: 주문 목록 항목이면 주문 m1). "
        "엔티티 행에 대응하지 않는 집계(날짜별 합계 등)면 null",
    )

    @property
    def entity_id(self) -> str:
        """모양 id — 엔티티와 같은 자리 이름 (타입·예시를 렌더하는 곳이 엔티티와 함께 읽는다)."""
        return self.shape_id


class RowUse(Schema):
    """공개 테스트가 쓰는 시드 행 — 그 처음 값. 코드가 그 값의 시드 그룹과 공개 몫 상수(PUBLIC_<엔티티>_<처음 값>)를 보장한다
    (2026-11-13 #164: 공개 몫 행이 테스트가 무엇을 가르는지 보지 않고 그룹마다 기계적으로 나와 경계 행·걸러질 행이 없었다)."""

    entity_id: str = Field(pattern=r"^m\d{1,2}$")
    values: dict[str, SeedScalar] = Field(
        min_length=1,
        description='그 행의 처음 값 (예: {"remainingSeats": 1}) — 시드 groups 의 values 와 같은 칸·값',
    )


class PublicTest(Schema):
    test_id: str = Field(pattern=r"^pt\d{1,2}$")
    ac_id: str = Field(pattern=r"^p\d{1,2}$")
    checks: str = Field(min_length=1, max_length=300)  # 무엇을 확인하나
    rows: list[RowUse] = Field(
        default_factory=list,
        max_length=4,
        description="이 테스트가 쓰는 시드 행의 처음 값 — 규칙을 지킨 구현과 어긴 구현이 다른 결과를 내는 행 "
        "(경계 값과 바로 바깥 값, 걸러질 행과 남을 행 등). 요청으로 만드는 데이터만 쓰면 비움",
    )


class TaskContract(Schema):
    """공개 계약 — 지원자에게 보인다. 크기 상한 엔드포인트 1–6 · 엔티티 1–8 (60분 과제 기준 [미정])."""

    interface_kind: InterfaceKind
    endpoints: list[Endpoint] = Field(min_length=1, max_length=6)
    entities: list[Entity] = Field(min_length=1, max_length=8)
    # 응답에만 있는 모양 (테이블 아님) — 2026-11-13. returns·필드 형식이 v1·v1[] 로 가리킨다
    response_shapes: list[ResponseShape] = Field(
        default_factory=list,
        max_length=4,
        description="응답에만 있는 모양 (테이블 아님 — 저장·시드 없음): 다른 엔티티에서 읽는 값·집계를 담는 응답. returns 가 v1·v1[] 로 가리킨다",
    )
    # 구현이 읽어야 하는 애플리케이션 설정 (키·기본값) — 2026-11-13 #161. 스프링 템플릿만
    settings: list[AppSetting] = Field(
        default_factory=list,
        max_length=4,
        description="요구가 '설정에 따름'인 값만 (예: 목록 표시 건수): 스프링 속성 키·기본값·뜻·근거 요구. 없으면 빈 목록",
    )
    seed: list[SeedRule] = Field(default_factory=list)  # DB 행을 바꾸는 과제만 (템플릿 seed_rows)
    fixtures: list[Fixture] = Field(default_factory=list, max_length=8)  # 그 밖의 입력 환경
    screens: list[Screen] = Field(default_factory=list, max_length=8)  # interface_kind ui 일 때만
    # 같은 알파: 동기화가 언제 나가는지(online 이벤트·주기·다음 저장) 없어 숨은 케이스가 POST 를 기다리다 끝났다
    background: str | None = Field(
        default=None,
        max_length=300,
        description="ui 만: 화면 조작 없이 나가는 서버 요청과 그 시점 (예: '브라우저 online 이벤트가 오면 대기열 요청을 순서대로 보냄'). 없으면 '없음'",
    )
    public_tests: list[PublicTest] = Field(min_length=1)
    covers: dict[str, list[str]] = Field(
        min_length=1,
        description='공개 요구 id → 그 요구를 담는 엔드포인트·엔티티·화면 id 목록 (예: {"r1": ["e1", "m1"]})',
    )

    @model_validator(mode="after")
    def _setting_keys(self) -> TaskContract:
        keys = [s.key for s in self.settings]
        if len(keys) != len(set(keys)):
            raise ValueError(f"settings 키가 겹침: {keys}")
        return self

    @model_validator(mode="after")
    def _screens(self) -> TaskContract:
        if self.interface_kind == "ui" and not self.screens:
            raise ValueError("interface_kind ui 는 screens 가 하나 이상")
        if self.interface_kind != "ui" and self.screens:
            raise ValueError(f"interface_kind {self.interface_kind} 는 screens 를 쓰지 않음")
        return self

    def data_shapes(self) -> list[Entity | ResponseShape]:
        """모양이 있는 것 전부 — 테이블 엔티티(m…) + 응답 모양(v…). 타입·예시·README 응답 모양·필드 이름처럼 '모양'을 읽는 곳이 쓴다.
        테이블(시드·테이블 이름·외래 키·숨은 케이스 data)은 entities 만 읽는다."""
        return [*self.entities, *self.response_shapes]


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


class Fault(Schema):
    """숨은 케이스의 실패 지정 — 어느 요청을 실패시키는지 **순번이 아니라 요청 본문 내용으로** 고른다.
    2026-11-11 재측정 오프라인(008d6314): 시나리오 '재연결 직후 처음 2건 503' 을 테스트가 순번으로 채워, 하나씩 보내는 허용 설계(d1)는
    두 번째 503 을 다음 연결에서 또 받아 메모를 잃은 것으로 판정됐고, 다시 만든 테스트는 오지 않을 두 번째 요청을 기다렸다 —
    보내는 방식(하나씩/한꺼번에)에 따라 판정이 갈렸다. 내용으로 고르면(메모 B 의 첫 전송만 실패) 어느 방식이든 같은 요청이 실패한다."""

    endpoint: str = Field(pattern=r"^e\d{1,2}$", description="실패시킬 계약 API 의 endpoint_id")
    when: dict[str, SeedScalar] = Field(
        default_factory=dict,
        description='이 값을 본문에 가진 요청만 실패 (요청 본문 칸 이름 → 값, 예: {"note": "메모B"}). 비우면 그 API 의 모든 요청',
    )
    times: int = Field(
        default=1, ge=1, le=3, description="그 요청(같은 내용)의 처음 몇 번을 실패시킬지"
    )
    status: int | None = Field(
        default=None, ge=400, le=599, description="오류 응답 상태 — 비우면 연결 실패(응답 없음)"
    )

    @field_validator("status", mode="before")
    @classmethod
    def _no_status_is_network_failure(cls, v: object) -> object:
        """400 미만·빈 값(0·"")은 '연결 실패'(None)로 받는다 — LLM 이 연결 실패를 0 으로 적어 스키마 위반으로 재시도를 날렸다
        (2026-11-11 오프라인 명세 v8)."""
        if v in (None, "", 0) or (isinstance(v, int) and not isinstance(v, bool) and v < 400):
            return None
        return v


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
    # 이 케이스에서 하네스가 실패시킬 요청 (숨김). 비면 시나리오 글 그대로 — 재료가 fault_by="item" 이면 실패를 주는 케이스는 반드시 채운다.
    faults: list[Fault] = Field(default_factory=list, max_length=4)


class HiddenPlan(Schema):
    """숨은 테스트 생성기만 보는 계획 — 지원자·시작 코드 생성기에 가지 않는다."""

    harness: dict[str, HarnessValue] = Field(
        default_factory=dict,
        description='템플릿 조절값 이름 → 값 (예: {"pool_size": 8}) — 지원자에게 보이는 값은 자료에 적힌 값 그대로',
    )
    cases: list[HiddenCase] = Field(min_length=1)
    # 공개 조절값마다 그 값이 적힌 자료 문장을 그대로 — 코드가 자료에 있는지 확인 (starter/contract.py value_check).
    # 자료에 없으면 "없음" — 그 조절값이 필수면 명세 결함. 2026-11-07 종이 단계: 템플릿 표기 패턴이 한 예시 표현에만 맞아 값이 있는데도 못 찾았다.
    evidence: dict[str, str] = Field(
        default_factory=dict,
        description='공개 조절값 이름 → 그 값이 적힌 자료·답 문장을 그대로 (예: {"pool_size": "db.maxConnections=20"}). 자료에 없으면 "없음"',
    )
    # 근거 인용 안에서 값이 적힌 글자 그대로 — 코드는 인용 문장에서 어느 수가 값인지 추측하지 않고 이 글자가 가리키는 값만 재료 규칙대로
    # 환산한다. 2026-11-15 운영 계약 실패: 인용 줄머리 절 번호 '3.1' 을 값으로 읽어 기대값을 잘못 만들었다. 예전 계획에는 없다(빈 dict).
    evidence_value: dict[str, str] = Field(
        default_factory=dict,
        description='공개 조절값 이름 → 근거 인용 안에서 값이 적힌 글자 그대로 — 수와 단위, 범위면 범위째 (예: {"pool_size": "20", "request_timeout_s": "500ms", "query_delay_ms": "14~16ms"}). 근거가 "없음"이면 넣지 않음',
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


class PlanContractInputV4(PlanContractInput):
    """plan_contract v17~ — 시드 행 수 계산식 대신 '코드가 맞춘다' 는 짧은 행 규칙(row_rules), 템플릿 인터페이스에 해당하는
    안내만(interface_rules). 2026-11-09 점검: v16 은 백엔드 계약에도 ui·cli·입력 환경 안내가 다 들어갔고(약 2KB), 행 수는
    fit_seed 가 맞추는데도 계산식을 줘 추론 토큰을 썼다."""

    row_rules: str = Field(min_length=1)
    interface_rules: str = Field(min_length=1)


class PlanContractInputAll(PlanContractInputV3):
    """plan_input 이 만드는 전체 — 버전마다 선언한 입력 모델로 걸러 넣는다 (plan_contract.for_prompt)."""

    row_rules: str = Field(min_length=1)
    interface_rules: str = Field(min_length=1)


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


class GenStarterInputV3(GenStarterInputV2):
    """gen_starter v7~ — 공개 테스트를 보고 쓴다 (TDD: 저장 확인은 통과, 새 기능 공개 테스트는 실패 = 지원자 몫)."""

    public_tests: str = "(없음)"


class GenPublicInput(Schema):
    """gen_public 프롬프트 변수 — 공개 정보만 (숨은 요구·HiddenPlan·운영 조건·구현 없음). 공개 수용 기준을 확인하는 기능 테스트.
    2026-11-08 사용자 결정: 테스트 먼저(TDD) — 시작 코드·참조 구현이 이 테스트를 보고 맞춘다. 숨은 테스트는 여전히 독립."""

    stack: str = Field(min_length=1)
    harness_api: str = Field(min_length=1)  # 공개 테스트 하네스 문서 + 이 과제의 PublicCase 소스
    slots: str = Field(min_length=1)
    contract: str = Field(min_length=1)
    public_tests: str = Field(min_length=1)  # 계약 public_tests (pt* — 공개 수용 기준별 확인 방법)
    requirements: str = Field(min_length=1)
    acceptance_public: str = Field(min_length=1)
    revision_note: str
    previous_files: str = "(없음)"


class GenHiddenInputV2(GenHiddenInput):
    """gen_hidden v2~ — 수리면 자기 이전 출력."""

    previous_files: str = "(없음)"


class GenHiddenInputV3(GenHiddenInputV2):
    """gen_hidden_react v8~ — 이 과제의 허용 설계(요약·조건). 숨은 테스트는 이미 숨은 요구를 보므로 설계를 줘도 새는 것이 없다.
    공개 테스트 입력에는 넣지 않는다 (허용 설계는 숨은 요구의 해법이다)."""

    accepted_designs: str = "(없음)"


class GenRefInputV2(GenRefInput):
    """gen_naive·gen_accepted v2~ — 수리면 자기 이전 출력."""

    previous_files: str = "(없음)"


class GenRefInputV3(GenRefInputV2):
    """gen_naive v4~·gen_accepted v4~ — 공개 테스트를 모두 통과하게 (숨은 테스트는 보지 않음)."""

    public_tests: str = "(없음)"


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


VariantOp = Literal["omit", "invert", "boundary"]  # 빼먹기 · 뒤집기 · 경계 한 칸 어긋남


class GenVariantInput(Schema):
    """gen_variant 프롬프트 변수 — 공개 테스트 변별 관문 (#164). 공개 테스트·계약의 확인 방법(checks)은 주지 않는다:
    테스트를 쓴 쪽의 맹점을 모르는 독립 검사여야 한다 (2026-11-13 사용자 — 새 에이전트가 검사)."""

    stack: str = Field(min_length=1)
    contract: str = Field(min_length=1)  # 확인 방법(public_tests)을 뺀 계약
    requirements: str = Field(min_length=1)
    acceptance_public: str = Field(min_length=1)
    target: str = Field(min_length=1)  # 어길 공개 수용 기준 1건
    operators: str = Field(min_length=1)  # 만들 갈래와 뜻
    impl_files: str = Field(
        min_length=1
    )  # 정답 구현 (시작 코드 + 허용 구현을 합친 앱 파일 — 테스트 파일 없음)
    revision_note: str


class ViolationVariant(Schema):
    op: VariantOp
    note: str = Field(min_length=1, max_length=200)  # 무엇을 어떻게 어겼나 한 줄
    files: list[GeneratedFile] = Field(min_length=1, max_length=3)  # 바꾼 파일 — 내용 전체


class GenVariantOutput(Schema):
    # 갈래마다 곳마다 하나 — 같은 규칙이 여러 곳(두 쿼리·여러 칸)에 있으면 곳마다 따로 (1차 변이, 2026-11-13)
    variants: list[ViolationVariant] = Field(default_factory=list, max_length=6)


class GenTrapInput(Schema):
    """gen_trap 프롬프트 변수 — 숨은 테스트 검증 (#171). 허용 설계 구현을 그 설계에 적힌 함정에 빠진 구현으로 바꾼다.
    숨은 테스트는 주지 않는다 (테스트를 쓴 쪽의 맹점을 모르는 독립 검사). 숨은 요구·설계는 준다 — 빌드 안쪽이라 새는 곳이 없다."""

    stack: str = Field(min_length=1)
    contract: str = Field(min_length=1)
    hidden_requirements: str = Field(min_length=1)  # 그 설계가 푸는 숨은 요구
    design: str = Field(min_length=1)  # 허용 설계 요약·조건
    trap: str = Field(min_length=1)  # 그 설계의 함정 문장
    impl_files: str = Field(min_length=1)  # 그 허용 설계 구현 (시작 코드 + 구현을 합친 앱 파일)
    revision_note: str


class TrapVariant(Schema):
    note: str = Field(min_length=1, max_length=200)  # 어떻게 함정에 빠졌나 한 줄
    files: list[GeneratedFile] = Field(min_length=1, max_length=3)  # 바꾼 파일 — 내용 전체


class GenTrapOutput(Schema):
    variants: list[TrapVariant] = Field(default_factory=list, max_length=2)


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
    # kept-previous: 숨은 테스트 수리에서 일부 케이스가 재요청 뒤에도 문제가 남아 이전 파일을 그대로 둠 (예산은 쓴 것으로 센다)
    result: Literal["regenerated", "failed", "skipped", "kept-previous"]


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
