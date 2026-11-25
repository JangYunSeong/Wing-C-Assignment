"""계약 시드 검사와 숨은 케이스 행 배정 (docs/design/spec-to-code.md §3.3 · §10-16) — 결정적, LLM 없음.

- 시드: id 1~rows 가 반드시 있고, groups 가 그 범위를 빈틈·겹침 없이 덮어 처음 값을 정한다.
- 숨은 케이스는 `HiddenCase.data` 로 바꿀 행(엔티티·수·처음 값·버스트)만 선언하고, **id 범위는 여기서 코드가 나눈다** —
  케이스 순서대로, 처음 값이 맞는 그룹의 큰 id 부터 이어진 범위로. 작은 id `RESERVED_ROWS` 행은 지원자·공개 테스트 몫.
  숨은 테스트 생성기는 시작 코드를 보지 않으므로(§2) 이 범위를 받아 쓴다.

2026-11-07 생성 스모크: 시드가 "10~100행, 상태는 넷 중 하나"뿐이라 숨은 테스트가 없는 id 를 쓰거나 동시 요청 96건에 쓸 행이 모자랐다.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from task_spec.schemas import CaseRows, HiddenPlan, SeedGroup, SeedRule, SeedScalar, TaskContract
from task_spec.schemas.build import Endpoint, Entity
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.auth_rule import login_fields, requester_rows
from task_spec.starter.field_types import enum_values
from task_spec.starter.field_types import parse as parse_field
from task_spec.starter.seed_sql import _insert_fields, default_value, time_kind

PUBLIC_PER_GROUP = 3  # 공개 테스트가 시드 그룹(처음 값)마다 쓸 행 수 — 숨은 범위·작은 id 밖
RESERVED_ROWS = 10  # 작은 id 쪽 — 숨은 케이스에 나눠 주지 않는다 (지원자 테스트·공개 테스트·시작 코드 테스트 몫)
_ID_FIELDS = {"id"}
_QUOTED = re.compile(r"""["'`]([^"'`]+)["'`]""")
# 고유 칸 — 계약 필드에 고유 표시 칸이 없어 제약 글로 본다 (예: '고유·중복 불가', '회원 고유 식별자 (UUID)')
_UNIQUE = re.compile(
    r"고유|유일|중복\s*(?:불가|금지|없|안\s*됨|될\s*수\s*없|허용\s*(?:안|않))|\bunique\b",
    re.IGNORECASE,
)
# 로그인 자격 증명 중 비밀 칸 — 여러 행이 같은 값이어도 된다 (로그인 칸은 식별 칸만 고유로 본다)
_SECRET = re.compile(r"pass|pwd|secret|pin\b|비밀", re.IGNORECASE)


def unique_fields(contract: TaskContract, entity_id: str) -> frozenset[str]:
    """같은 값을 두 행에 둘 수 없는 칸 — 제약 글이 고유를 말하는 칸과, 발급(로그인) 엔티티의 식별 칸(자격 증명 중 비밀 칸이 아닌 것).
    로그인 칸은 고유라고 적지 않아도 같은 값의 행이 둘이면 'username 으로 찾기' 구현이 깨진다."""
    m = next((e for e in contract.entities if e.entity_id == entity_id), None)
    if m is None:
        return frozenset()
    out = {f.name for f in m.fields if f.constraints and _UNIQUE.search(f.constraints)}
    # 로그인 칸은 로그인 행이 정해지기 전(fit_auth 앞, 계약 단계의 실제 순서)에도 찾는다 — auth_rule.login_fields
    out |= {n for n in login_fields(contract).get(entity_id, []) if not _SECRET.search(n)}
    return frozenset(out)


def public_per_group(
    contract: TaskContract, entity_id: str, values: Mapping[str, SeedScalar]
) -> int:
    """처음 값 그룹마다 공개 테스트 몫 행 수 — 고유 칸을 처음 값으로 정한 그룹은 1행 (같은 값의 행을 여럿 둘 수 없다).
    2026-11-25 #308 알파 1·3: 공개 테스트가 쓰는 로그인 행(username·password 같은 값)을 3행으로 덧붙여, 'username 으로 찾기'
    구현이 500 으로 떨어지고(알파 1) USERS(USERNAME) 고유 인덱스 위반으로 시작 코드가 기동하지 못했다(알파 3)."""
    return 1 if set(values) & unique_fields(contract, entity_id) else PUBLIC_PER_GROUP


def peak_reasons(manifest: TemplateManifest) -> dict[str, list[str]]:
    """동시 요청(운영 피크)으로만 재현되는 사유 → 그 성립 조건이 쓰는 버스트 조절값(burst_rows). 성립 조건이 동시 요청 수를 쓰면
    요청 하나로는 조건이 서지 않는다. 2026-11-25 #308 알파 1: pool_acquire_timeout 케이스 data 가 모두 burst=false 라 정적 검사가
    peak 를 막아 hc1 이 Burst.fire(1, …) 한 건이 됐고, 풀이 고갈될 수 없으니 흔한 구현이 매 회차 통과했다."""
    names = set(manifest.burst_rows)
    out: dict[str, list[str]] = {}
    for r in manifest.failure_reasons:
        used = sorted(
            {n for f in r.feasible for n in names if re.search(rf"\b{re.escape(n)}\b", f.when)}
        )
        if used:
            out[r.code] = used
    return out


@dataclass(frozen=True)
class RowRange:
    case_id: str
    entity_id: str
    first: int
    last: int
    initial: Mapping[str, SeedScalar]
    burst: bool = False

    @property
    def rows(self) -> int:
        return self.last - self.first + 1


@dataclass
class RowAllocation:
    ranges: list[RowRange] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    failed: list[tuple[str, CaseRows]] = field(
        default_factory=list
    )  # 시드는 있는데 범위를 못 나눈 (케이스, data)

    def for_case(self, case_id: str) -> list[RowRange]:
        return [r for r in self.ranges if r.case_id == case_id]


def _enum_values(type_: str, constraints: str | None) -> set[str] | None:
    """필드가 값 목록을 정하면 그 목록 — `enum(A|B)` 형식, 또는 제약 문장의 따옴표 낱말 2개 이상."""
    if (values := enum_values(type_)) is not None:  # 계약 형식 해석 한 곳 (#172)
        return set(values)
    quoted = _QUOTED.findall(constraints or "")
    return set(quoted) if len(quoted) >= 2 else None


def _value_problems(
    where: str, values: Mapping[str, SeedScalar], contract: TaskContract, entity_id: str
) -> list[str]:
    entity = next((m for m in contract.entities if m.entity_id == entity_id), None)
    if entity is None:
        return []
    fields = {f.name: f for f in entity.fields}
    out: list[str] = []
    for name, value in values.items():
        if name in _ID_FIELDS:
            out.append(f"{where}: id 는 값으로 정하지 않는다 (범위가 id)")
            continue
        f = fields.get(name)
        if f is None:
            out.append(f"{where}: {entity.name} 에 없는 필드 {name}")
            continue
        allowed = _enum_values(f.type, f.constraints)
        if allowed is not None and str(value) not in allowed:
            out.append(
                f"{where}: {entity.name}.{name}={value} 는 허용 값({', '.join(sorted(allowed))}) 밖"
            )
    return out


def _unique_value_problems(label: str, s: SeedRule, unique: frozenset[str]) -> list[str]:
    """고유 칸의 한 값이 몇 행에 들어가는지 범위를 넘어 합친다 — 범위 하나 안의 여러 행뿐 아니라 한 행 범위 둘(1~1·2~2)에 같은 값도
    중복이다 (2026-11-25 #308 4차: 범위 안만 보던 검사가 범위를 넘는 중복을 놓쳤다)."""
    groups: dict[tuple[str, str], list[SeedGroup]] = {}
    for g in s.groups:
        for name in sorted(set(g.values) & unique):
            groups.setdefault((name, str(g.values[name])), []).append(g)
    out: list[str] = []
    for (name, value), gs in sorted(groups.items()):
        rows = sum(g.last - g.first + 1 for g in gs)
        if rows > 1:
            spans = "·".join(f"{g.first}~{g.last}" for g in sorted(gs, key=lambda g: g.first))
            out.append(
                f"{label} 범위 {spans}: 고유 칸 {name} 을 같은 값({value})으로 {rows}행에 둠 — "
                "고유 칸은 values 에서 빼거나(행마다 다른 기본값) 값마다 한 행으로 둔다"
            )
    return out


def seed_problems(contract: TaskContract) -> list[str]:
    """시드 규칙 — 엔티티·중복·id 범위(1~rows 를 빈틈·겹침 없이)·처음 값 필드."""
    entity_ids = {m.entity_id for m in contract.entities}
    out: list[str] = []
    seen: set[str] = set()
    for s in contract.seed:
        label = f"시드 {s.entity_id}"
        if s.entity_id not in entity_ids:
            out.append(f"{label}: 엔티티가 계약에 없음")
            continue
        if s.entity_id in seen:
            out.append(f"{label}: 같은 엔티티의 시드 규칙이 둘 이상")
        seen.add(s.entity_id)
        if s.rows == 0:
            if s.groups:
                out.append(f"{label}: 0행인데 groups 가 있음")
            continue
        if not s.groups:
            out.append(
                f"{label}: groups 가 없음 — id 1~{s.rows} 를 덮는 범위와 처음 값을 적는다 (정할 값이 없으면 values 를 비운 범위 하나)"
            )
            continue
        expected = 1
        for g in sorted(s.groups, key=lambda g: g.first):
            if g.first > expected:
                out.append(f"{label}: id {expected}~{g.first - 1} 이 어느 범위에도 없음")
            elif g.first < expected:
                out.append(f"{label}: 범위 {g.first}~{g.last} 가 앞 범위와 겹침")
            expected = max(expected, g.last + 1)
            out += _value_problems(
                f"{label} 범위 {g.first}~{g.last}", g.values, contract, s.entity_id
            )
        out += _unique_value_problems(label, s, unique_fields(contract, s.entity_id))
        if expected - 1 > s.rows:
            out.append(f"{label}: 범위가 rows={s.rows} 를 넘음 (id {expected - 1} 까지)")
        elif expected - 1 < s.rows:
            out.append(f"{label}: id {expected}~{s.rows} 이 어느 범위에도 없음")
    return out + _lookup_key_problems(contract) + _scope_gap_problems(contract)


_PATH_PARAM = re.compile(r"\{(\w+)\}")


def _scope_gap_problems(contract: TaskContract) -> list[str]:
    """경로 변수인 필드를 어떤 그룹은 정하고 어떤 그룹은 비움 — 빈 그룹 행은 행마다 다른 기본값이 돼 경로로 보낸 값과 안 맞는다
    (v16-be1: guildId=1 그룹 사이에 guildId 없는 그룹 → 그 행의 guild_id 가 행 번호)."""
    params = {p for e in contract.endpoints for p in _PATH_PARAM.findall(e.path)}
    out: list[str] = []
    for s in contract.seed:
        fixed = {k for g in s.groups for k in g.values} & params
        for k in sorted(fixed):
            gaps = [f"{g.first}~{g.last}" for g in s.groups if k not in g.values]
            if gaps:
                out.append(
                    f"시드 {s.entity_id}: 경로 변수 {k} 를 어떤 범위는 정하고 {', '.join(gaps)} 는 비움 — 빈 범위 행은 행마다 다른 값이 돼 "
                    f"경로로 보낸 {k} 와 안 맞는다. 모든 범위의 values 에 {k} 를 적는다"
                )
    return out


ListTarget = tuple[Literal["entity", "shape", "other"], str]


def response_list_target(e: Endpoint, contract: TaskContract) -> ListTarget | None:
    """성공 응답이 무엇의 목록인가 — ("entity", 엔티티 id) · ("shape", 응답 모양 id) · ("other", 원소 형식), 목록이 아니면 None.
    returns `X[]` 를 먼저, 없으면 본문의 목록 칸(`{items: X[]}`). 엔티티·모양은 id 또는 이름으로 적어도 된다.
    한 질문을 세 함수(무엇이든 목록·그 엔티티의 목록·모양의 목록)가 따로 답하던 것을 하나로 (#179).
    2026-11-11 N+1: 엔티티 목록 API(`GET /projects/{projectId}/board/posts` → m1[])를 '한 행을 찾는 API' 로 짚었다.
    2026-11-13 송금 내역: 응답 모양의 목록(`… → v1[]`)도 목록 API 다 — 모양에는 어느 엔티티의 모양인지 칸이 없다."""
    ok = e.success()
    if ok is None:
        return None
    entities = {k: m.entity_id for m in contract.entities for k in (m.entity_id, m.name)}
    shapes = {k: sh.shape_id for sh in contract.response_shapes for k in (sh.shape_id, sh.name)}
    names: list[str] = []
    if ok.returns and ok.returns.endswith("[]"):
        names.append(ok.returns[:-2])
    for f in ok.body:
        ft = parse_field(f.type)
        if ft.kind == "list":
            elem = ft.items[0] if ft.items else None
            names.append(elem.name if elem is not None and elem.kind == "ref" else f.type)
    for name in names:
        if name in entities:
            return ("entity", entities[name])
        if name in shapes:
            return ("shape", shapes[name])
    return ("other", names[0]) if names else None


def returned_entities(contract: TaskContract) -> set[str]:
    """계약 API 의 성공 응답이 행을 돌려주는 엔티티 — returns 의 엔티티(한 건·목록), 응답 모양의 row_of, 본문 칸의 엔티티(목록)."""
    by_key = {k: m.entity_id for m in contract.entities for k in (m.entity_id, m.name)}
    row_of = {
        k: s.row_of
        for s in contract.response_shapes
        if s.row_of in {m.entity_id for m in contract.entities}
        for k in (s.shape_id, s.name)
    }
    out: set[str] = set()
    for e in contract.endpoints:
        ok = e.success()
        if ok is None:
            continue
        names = [ok.returns.removesuffix("[]")] if ok.returns else []
        for f in ok.body:
            ft = parse_field(f.type)
            ref = ft.items[0] if ft.kind == "list" and ft.items else ft
            if ref.kind == "ref":
                names.append(ref.name)
        out |= {by_key[n] for n in names if n in by_key}
        out |= {row_of[n] for n in names if n in row_of and row_of[n] is not None}  # type: ignore[misc]
    return out


def _fk_links(contract: TaskContract) -> dict[str, set[str]]:
    """엔티티 id → 외래 키로 이어진 엔티티 id (양쪽 방향)."""
    by_name = {m.name: m.entity_id for m in contract.entities}
    out: dict[str, set[str]] = {}
    for m in contract.entities:
        for _, target in m.fk_pairs():
            t = by_name.get(target, target)
            if t != m.entity_id:
                out.setdefault(m.entity_id, set()).add(t)
                out.setdefault(t, set()).add(m.entity_id)
    return out


def _joined_only(contract: TaskContract, entity_id: str, returned: set[str]) -> bool:
    """API 가 행을 돌려주지 않고, 돌려주는 엔티티와 외래 키로 이어진 엔티티 — 그 행은 목록 항목의 칸(조인)으로만 보인다."""
    return entity_id not in returned and bool(_fk_links(contract).get(entity_id, set()) & returned)


def fit_row_targets(contract: TaskContract) -> tuple[TaskContract, list[str]]:
    """공개 테스트 rows 는 API 가 돌려주는 엔티티의 행으로 — 그 행에 조인돼 보이는 딸린 정보의 원천 행은 뺀다.
    테스트는 그 행을 공개 몫 상수로 가리킬 수 없고, 공개 몫 행은 목록 행과 외래 키로 이어지지도 않는다 (외래 키 값은 대상 행 안에서 돈다).
    2026-11-14 #185 알파(g185-nplus1-hana006): pt1 rows 에 송금 대기 행과 함께 고객(BR001)·서류(IDENTITY) 행을 적어, 정적 검사가 그 상수를
    요구했고(재요청에도 고칠 수 없음) 빌드가 실패했다 — 그 고객 공개 몫 행(211~213)을 가리키는 송금은 BR002 라 목록에 나오지도 않았다.
    돌려주는 엔티티를 알 수 없으면(응답 모양 row_of 없음) 그대로 둔다 — row_target_problems 가 묻는다."""
    returned = returned_entities(contract)
    if not returned:
        return contract, []
    names = {m.entity_id: m.name for m in contract.entities}
    tests, notes = [], []
    for t in contract.public_tests:
        drop = [u for u in t.rows if _joined_only(contract, u.entity_id, returned)]
        if not drop:
            tests.append(t)
            continue
        tests.append(t.model_copy(update={"rows": [u for u in t.rows if u not in drop]}))
        shown = "; ".join(
            f"{names.get(u.entity_id, u.entity_id)}({', '.join(f'{k}={v}' for k, v in u.values.items())})"
            for u in drop
        )
        notes.append(
            f"공개 테스트 {t.test_id} rows 에서 {shown} 뺌 — API 가 그 행을 돌려주지 않고 목록 행에 조인돼 보이는 딸린 정보라 "
            "테스트가 공개 몫 상수로 가리킬 수 없다"
        )
    return contract.model_copy(update={"public_tests": tests}), notes


def row_target_problems(contract: TaskContract) -> list[str]:
    """공개 테스트 rows 가 외래 키로 이어진 여러 엔티티의 행인데 그중 API 가 돌려주는 것을 계약에서 알 수 없음 — 응답 모양에 row_of 가 없다."""
    returned = returned_entities(contract)
    links = _fk_links(contract)
    unknown = [
        s.shape_id
        for s in contract.response_shapes
        if s.row_of is None and _shape_returned(contract, s.shape_id)
    ]
    if not unknown:
        return []
    out: list[str] = []
    names = {m.entity_id: m.name for m in contract.entities}
    for t in contract.public_tests:
        ents = {u.entity_id for u in t.rows}
        if len(ents) < 2 or ents & returned or not any(links.get(e, set()) & ents for e in ents):
            continue
        out.append(
            f"공개 테스트 {t.test_id} rows 의 {', '.join(sorted(names.get(e, e) for e in ents))} 중 어느 것이 API 가 돌려주는 행인지 계약에 없음 — "
            f"응답 모양 {', '.join(unknown)} 의 row_of 에 한 건(목록 항목 하나)이 어느 엔티티 행인지 적는다. rows 에는 그 행만 적고, 조인돼 보이는 "
            "딸린 정보(고객 이름·서류 수의 원천)의 행은 적지 않는다"
        )
    return out


def _shape_returned(contract: TaskContract, shape_id: str) -> bool:
    shape = next((s for s in contract.response_shapes if s.shape_id == shape_id), None)
    keys = {shape_id, shape.name} if shape is not None else {shape_id}
    for e in contract.endpoints:
        ok = e.success()
        if ok is None:
            continue
        if ok.returns and ok.returns.removesuffix("[]") in keys:
            return True
        for f in ok.body:
            ft = parse_field(f.type)
            ref = ft.items[0] if ft.kind == "list" and ft.items else ft
            if ref.kind == "ref" and ref.name in keys:
                return True
    return False


def _lookup_key_problems(contract: TaskContract) -> list[str]:
    """경로 변수로 그 엔티티의 한 행을 가리키는 API 에서, 그 변수가 여러 행 그룹 전체에 같은 값이면 문제 — 조회가 여러 건이 된다.

    '한 행을 가리킨다' = 경로 변수가 모두 그 엔티티의 칸이고(id 처럼 행마다 다른 칸이 끼면 한 행이 정해진다), 성공 응답이 그 엔티티나
    응답 모양의 목록이 아니다 (목록이면 같은 값이 여러 행에 있는 것이 정상 — 판별은 response_list_target 한 곳).
    2026-11-09 alpha-cap1: 시드 규칙 글은 "memberId 는 id 와 같다"인데 groups 가 254행 모두 memberId=1 로 고정해 여러 건 오류로
    시작 코드·흔한 구현·허용 구현이 모두 같은 500 으로 떨어졌다. 목록 API 를 한 건 응답으로 잘못 적은 계약(2026-11-11 N+1)도 같은 꼴로
    걸리므로 안내는 두 고침(목록으로 적기·값 빼기)을 함께 준다 (#180: 사례마다 갈래를 두던 것을 하나로)."""
    entities = {m.entity_id: m for m in contract.entities}
    out: list[str] = []
    for e in contract.endpoints:
        params = _PATH_PARAM.findall(e.path)
        if not params or any(p in _ID_FIELDS for p in params):
            continue
        target = response_list_target(e, contract)
        for s in contract.seed:
            m = entities.get(s.entity_id)
            if (
                m is None
                or (target is not None and target[0] == "shape")
                or target == ("entity", m.entity_id)
            ):
                continue
            if not set(params) <= {f.name for f in m.fields}:
                continue  # 칸이 아닌 경로 변수({memberId} → id 등)가 끼면 그게 행마다 다르다
            for g in s.groups:
                if g.last > g.first and all(k in g.values for k in params):
                    msg = (
                        f"시드 {s.entity_id} 범위 {g.first}~{g.last}: {e.method or ''} {e.path} 가 {', '.join(params)} 로 {m.name} 한 행을 "
                        f"가리키는데 {', '.join(params)} 가 모든 행에서 같음 — 여러 건 조회 오류. 이 API 가 목록을 돌려주면 성공 응답을 "
                        f'returns "{s.entity_id}[]" ({m.name} 목록)이나 응답 모양 목록으로 적고, 한 행을 찾는 API 면 {params[-1]} 는 '
                        "values 에서 뺀다 (코드가 행마다 다른 값을 넣는다)"
                    )
                    if msg not in out:
                        out.append(msg)
    return out


def burst_rows_needed(plan: HiddenPlan, manifest: TemplateManifest) -> tuple[int | None, list[str]]:
    """버스트 케이스가 요청마다 다른 행을 쓰려면 필요한 행 수 (manifest burst_rows 조절값의 곱). 값이 없으면 None + 빠진 이름."""
    if not manifest.burst_rows:
        return None, []
    missing = [n for n in manifest.burst_rows if not isinstance(plan.harness.get(n), int)]
    if missing:
        return None, missing
    return math.prod(int(plan.harness[n]) for n in manifest.burst_rows), []  # type: ignore[arg-type]


def common_values(seed: SeedRule) -> dict[str, SeedScalar]:
    """모든 그룹이 같은 값으로 정한 필드 (예: 한 길드 guildId=1) — 코드가 덧붙이는 그룹도 이어받는다.

    2026-11-09 v16-be1: 숨은·공개 몫으로 덧붙인 그룹이 케이스 처음 값(rank)만 가져 guildId 가 빠졌고, values 에 없는 필드는
    행마다 다른 기본값이라 그 행들의 guild_id 가 행 번호(251~497)가 됐다 — 숨은 테스트가 guildId=1 로 보내 모두 대상 없음(404)이라
    롤백 케이스가 주입 단계에 닿지 않고 흔한 구현까지 통과했다."""
    if not seed.groups:
        return {}
    head, *rest = seed.groups
    return {
        k: v
        for k, v in head.values.items()
        if all(k in g.values and str(g.values[k]) == str(v) for g in rest)
    }


def _matches(group_values: Mapping[str, SeedScalar], initial: Mapping[str, SeedScalar]) -> bool:
    return all(k in group_values and str(group_values[k]) == str(v) for k, v in initial.items())


def allocate_rows(
    contract: TaskContract, plan: HiddenPlan, reserved: int = RESERVED_ROWS
) -> RowAllocation:
    """케이스 순서대로, 처음 값이 맞는 그룹의 큰 id 부터 이어진 범위를 나눈다. id 1~reserved 는 나눠 주지 않는다."""
    seeds: dict[str, SeedRule] = {s.entity_id: s for s in contract.seed}
    names = {m.entity_id: m.name for m in contract.entities}
    asked = requester_rows(contract)  # 권한 칸의 요청자 행 — 바꾸지 않는다 (auth_rule)
    # 그룹마다 아직 안 나눈 가장 큰 id
    tops: dict[tuple[str, int], int] = {
        (s.entity_id, i): g.last for s in contract.seed for i, g in enumerate(s.groups)
    }
    res = RowAllocation()
    short: set[str] = set()
    for c in plan.cases:
        for d in c.data:
            if d.rows == 0:  # 바꿀 행이 없음 — fit_seed 가 빼지만 직접 부를 때도 배정하지 않는다
                continue
            name = names.get(d.entity_id, d.entity_id)
            seed = seeds.get(d.entity_id)
            if seed is None or seed.rows == 0:
                res.problems.append(
                    f"숨은 케이스 {c.case_id}: {name} 의 시드 행이 없음 — 바꿀 행은 계약 시드에 있어야 한다"
                )
                continue
            groups = sorted(
                (
                    (i, g)
                    for i, g in enumerate(seed.groups)
                    if _matches(g.values, d.initial)
                    and not set(range(g.first, g.last + 1)) <= asked.get(d.entity_id, set())
                ),
                key=lambda ig: -ig[1].last,
            )
            for i, g in groups:
                top = tops[(d.entity_id, i)]
                first = top - d.rows + 1
                if first >= max(g.first, reserved + 1):
                    res.ranges.append(
                        RowRange(c.case_id, d.entity_id, first, top, dict(d.initial), d.burst)
                    )
                    tops[(d.entity_id, i)] = first - 1
                    break
            else:
                short.add(d.entity_id)
                res.failed.append((c.case_id, d))
                cond = _cond(d.initial)
                free = sum(
                    max(0, tops[(d.entity_id, i)] - max(g.first, reserved + 1) + 1)
                    for i, g in groups
                )
                res.problems.append(
                    f"숨은 케이스 {c.case_id}: {name} 행 {d.rows}개({cond})를 이어진 범위로 나눌 수 없음 "
                    f"(남은 맞는 행 {free}개, 작은 id {reserved}행은 지원자 몫) — 시드 rows·groups 를 늘린다"
                )
    for eid in sorted(short):
        res.problems.append(_need_text(eid, names.get(eid, eid), seeds.get(eid), plan, reserved))
    return res


def public_free(
    contract: TaskContract, plan: HiddenPlan, reserved: int = RESERVED_ROWS
) -> list[tuple[str, dict[str, SeedScalar], list[int]]]:
    """공개 테스트 몫 행 — (엔티티 id, 처음 값, id 들). 처음 값이 같은 그룹은 하나로, 작은 id(1~reserved)와 숨은 케이스 범위를
    피해 낮은 id 부터 PUBLIC_PER_GROUP 개까지. 공개·숨은·시작 코드 테스트가 같은 DB 를 함께 써도 서로의 행을 바꾸지 않게."""
    used = {
        i
        for r in allocate_rows(contract, plan, reserved).ranges
        for i in range(r.first, r.last + 1)
    }
    asked = requester_rows(contract)
    # 처음 값이 같으면 칸 순서가 달라도 같은 그룹 — 2026-11-13 #164 좌석 알파: LLM 그룹({remainingSeats, totalSeats, status})과
    # 공통 값을 앞에 두는 덧붙임 그룹({status, …})을 따로 세어 같은 그룹을 거듭 덧붙이고 다른 처음 값의 몫은 비었다.
    # 상수 이름(hidden_helpers.public_rows)은 처음 본 그룹의 칸 순서를 따른다.
    out: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[dict[str, SeedScalar], list[int]]] = {}
    for s in contract.seed:
        if s.rows <= 0:
            continue
        for g in sorted(s.groups, key=lambda x: x.first):
            if set(range(g.first, g.last + 1)) <= asked.get(s.entity_id, set()):
                continue  # 요청자 행만 있는 그룹
            key = (s.entity_id, tuple(sorted((k, str(v)) for k, v in g.values.items())))
            _, ids = out.setdefault(key, (dict(g.values), []))
            target = public_per_group(contract, s.entity_id, g.values)
            for i in range(max(g.first, reserved + 1), g.last + 1):
                if len(ids) >= target:
                    break
                if i not in used and i not in asked.get(s.entity_id, set()):
                    ids.append(i)
            if target < PUBLIC_PER_GROUP and not ids:
                # 고유 칸을 정한 그룹은 같은 값의 행을 덧붙일 수 없다 (_fit_public) — 작은 id 쪽의 그 행이라도 공개 몫으로
                ids += [
                    i
                    for i in range(g.first, min(g.last, reserved) + 1)
                    if i not in used and i not in asked.get(s.entity_id, set())
                ][:1]
    return [(eid, vals, ids) for (eid, _), (vals, ids) in out.items()]


def _cond(initial: Mapping[str, SeedScalar]) -> str:
    return ", ".join(f"{k}={v}" for k, v in initial.items()) or "조건 없음"


def _need_text(
    entity_id: str, name: str, seed: SeedRule | None, plan: HiddenPlan, reserved: int
) -> str:
    """모자란 엔티티의 정확한 필요 행 수 — 재요청이 "늘린다"만으로는 계산을 또 틀렸다 (2026-11-07 v3 스모크: 96+10 만 두고 hc2 를 빠뜨림)."""
    uses = [(c.case_id, d) for c in plan.cases for d in c.data if d.entity_id == entity_id]
    total = sum(d.rows for _, d in uses)
    by_cond: dict[str, int] = {}
    for _, d in uses:
        by_cond[_cond(d.initial)] = by_cond.get(_cond(d.initial), 0) + d.rows
    have = seed.rows if seed is not None else 0
    parts = " + ".join(f"{cid} {d.rows}" for cid, d in uses)
    conds = " · ".join(f"{k} {v}행" for k, v in by_cond.items())
    return (
        f"{name} 시드는 rows ≥ {total + reserved} 이어야 한다 (지금 {have}): 숨은 케이스 data {parts} = {total} "
        f"+ 지원자 몫 작은 id {reserved}행. 처음 값별로 id {reserved + 1} 위에 필요한 행: {conds}"
    )


def fit_seed(
    contract: TaskContract,
    plan: HiddenPlan,
    manifest: TemplateManifest,
    reserved: int = RESERVED_ROWS,
) -> tuple[TaskContract, HiddenPlan, list[str]]:
    """행 수 맞추기 — LLM 이 곱·합을 틀려도 코드가 정확히 맞춘다. 바꾼 것은 notes 로 돌려준다 (승인 화면에 보임).

    2026-11-07 v3 스모크: 버스트 행을 concurrency 만큼만(32·42·50) 쓰고, 재요청에 96 을 적어 줘도 또 32 를 썼다.
    시드 rows 도 케이스 행 합 + 지원자 몫을 빠뜨렸다. 그래서 재요청 대신:
    1. 버스트 data 의 rows = manifest burst_rows 조절값의 곱 (작으면 올림).
    2. 처음에 비어 있는 엔티티(rows 0)의 data 는 뺀다 — 요청으로 생기는 기록은 바꿀 시드 행이 아니다.
    3. 그래도 범위를 못 나누면 그 data 의 처음 값으로 시드 끝에 범위를 덧붙인다 (작은 id 지원자 몫 위로).
    시드 규칙이 아예 없는 엔티티는 고치지 않는다 (무슨 값으로 채울지 몰라 검사 문제로 남김).
    """
    notes: list[str] = []
    names = {m.entity_id: m.name for m in contract.entities}
    seeds = {s.entity_id: s for s in contract.seed}
    need, _ = burst_rows_needed(plan, manifest)
    formula = " × ".join(manifest.burst_rows)
    cases = []
    for c in plan.cases:
        kept: list[CaseRows] = []
        for d in c.data:
            name = names.get(d.entity_id, d.entity_id)
            seed = seeds.get(d.entity_id)
            if d.rows == 0:
                notes.append(
                    f"숨은 케이스 {c.case_id}: {name} data 의 rows 0 을 뺌 (바꿀 시드 행이 없음)"
                )
                continue
            if seed is not None and seed.rows == 0:
                notes.append(
                    f"숨은 케이스 {c.case_id}: 처음에 비어 있는 {name} 의 data 를 뺌 (요청으로 생기는 기록은 바꿀 시드 행이 아님)"
                )
                continue
            if d.burst and need is not None and d.rows < need:
                notes.append(f"숨은 케이스 {c.case_id}: 버스트 행 {d.rows} → {need} ({formula})")
                d = d.model_copy(update={"rows": need})
            kept.append(d)
        cases.append(c.model_copy(update={"data": kept}))
    plan = plan.model_copy(update={"cases": cases})

    for _ in range(sum(len(c.data) for c in plan.cases) + 1):
        alloc = allocate_rows(contract, plan, reserved)
        if not alloc.failed:
            break
        cid, d = alloc.failed[0]
        seed = seeds[d.entity_id]
        first = seed.rows + 1
        tail = max(seed.groups, key=lambda g: g.last) if seed.groups else None
        values = {**common_values(seed), **d.initial}
        if tail is not None and tail.last == seed.rows and dict(tail.values) == values:
            # 끝 범위와 처음 값이 같으면 모자란 만큼만 그 범위를 늘린다 (README 에 같은 값 범위가 이어 붙지 않게)
            taken = sum(
                r.rows
                for r in alloc.ranges
                if r.entity_id == d.entity_id and tail.first <= r.first and r.last <= tail.last
            )
            free = max(0, tail.last - max(tail.first, reserved + 1) + 1 - taken)
            last = max(seed.rows, reserved) + d.rows - free
            groups = [
                g if g is not tail else g.model_copy(update={"last": last}) for g in seed.groups
            ]
        else:
            last = max(seed.rows, reserved) + d.rows
            groups = [*seed.groups, SeedGroup(first=first, last=last, values=values)]
        grown = seed.model_copy(update={"rows": last, "groups": groups})
        notes.append(
            f"{names.get(d.entity_id, d.entity_id)} 시드 {seed.rows} → {last}행: id {first}~{last} ({_cond(values)}) 덧붙임 "
            f"— 숨은 케이스 {cid} {d.rows}행을 작은 id {reserved}행 위에서 나누려고"
        )
        seeds[d.entity_id] = grown
        contract = contract.model_copy(update={"seed": [seeds[s.entity_id] for s in contract.seed]})
    if manifest.public_helper is not None:
        contract = _fit_row_uses(contract, seeds, names, reserved, notes)
        contract = _fit_public(contract, plan, seeds, names, reserved, notes)
    if notes:  # 고친 결과도 스키마(행 상한 등)를 지키는지
        contract = TaskContract.model_validate(contract.model_dump())
        plan = HiddenPlan.model_validate(plan.model_dump())
    return contract, plan, notes


def matches(values: Mapping[str, SeedScalar], want: Mapping[str, SeedScalar]) -> bool:
    """처음 값 values 가 want 를 모두 담는가 (칸 순서·값 형식 무관 — 시드 값은 글자로 비교)."""
    return all(k in values and str(values[k]) == str(v) for k, v in want.items())


def _append_unique(seed: SeedRule, values: dict[str, SeedScalar]) -> tuple[SeedRule, int]:
    """고유 칸을 정한 처음 값의 행을 시드 끝에 하나만 덧붙인다 — (새 시드, 그 행 id). 작은 id 쪽이어도 둔다: 작은 id 는 공개 테스트
    몫이기도 하고(RESERVED_ROWS), public_free 가 그 행을 공개 몫으로 대신한다. 작은 id 밖으로 밀려고 사이 행을 채우면 그 채움 범위에
    다시 공개 몫 행이 붙는다."""
    row = seed.rows + 1
    group = SeedGroup(first=row, last=row, values=dict(values))
    return seed.model_copy(update={"rows": row, "groups": [*seed.groups, group]}), row


def _fit_row_uses(
    contract: TaskContract,
    seeds: dict[str, SeedRule],
    names: Mapping[str, str],
    reserved: int,
    notes: list[str],
) -> TaskContract:
    """공개 테스트가 쓰겠다고 한 행(PublicTest.rows)의 처음 값마다 시드 그룹이 있게 — 없으면 시드 끝에 덧붙인다.
    그 뒤 _fit_public 이 그룹마다 공개 몫 상수를 채운다 (#164: 경계 행·걸러질 행이 시드에 없어 공개 테스트가 규칙을 가르지 못했다).
    처음에 비어 있는 엔티티·시드 규칙이 없는 엔티티는 고치지 않는다 (row_use_problems 가 잡음)."""
    for t in contract.public_tests:
        for use in t.rows:
            seed = seeds.get(use.entity_id)
            if seed is None or seed.rows == 0:
                continue
            if any(matches(g.values, use.values) for g in seed.groups):
                continue
            values = {**common_values(seed), **use.values}
            unique = unique_fields(contract, use.entity_id)
            why = f"공개 테스트 {t.test_id} 가 쓰는 행"
            if set(values) & unique:
                grown, first = _append_unique(seed, values)
                last = first
                why += f" — 고유 칸({', '.join(sorted(set(values) & unique))})이라 1행"
            else:
                first = seed.rows + 1
                last = max(seed.rows, reserved) + PUBLIC_PER_GROUP
                grown = seed.model_copy(
                    update={
                        "rows": last,
                        "groups": [*seed.groups, SeedGroup(first=first, last=last, values=values)],
                    }
                )
            seeds[use.entity_id] = grown
            notes.append(
                f"{names.get(use.entity_id, use.entity_id)} 시드 {seed.rows} → {grown.rows}행: id {first}~{last} ({_cond(values)}) 덧붙임 "
                f"— {why}"
            )
            contract = contract.model_copy(
                update={"seed": [seeds[x.entity_id] for x in contract.seed]}
            )
    return contract


def row_use_problems(contract: TaskContract) -> list[str]:
    """공개 테스트 rows 가 가리키는 행을 시드로 만들 수 있는가 — 엔티티·칸이 있고, 처음부터 행이 있는 엔티티 (요청으로 만드는 데이터는 rows 에 적지 않음)."""
    out: list[str] = []
    entities = {m.entity_id: m for m in contract.entities}
    seeds = {x.entity_id: x for x in contract.seed}
    for t in contract.public_tests:
        for use in t.rows:
            m = entities.get(use.entity_id)
            if m is None:
                out.append(f"공개 테스트 {t.test_id} rows: 계약에 없는 엔티티 {use.entity_id}")
                continue
            seed = seeds.get(use.entity_id)
            if seed is None or seed.rows == 0:
                out.append(
                    f"공개 테스트 {t.test_id} rows: {m.name} 은 처음에 비어 있어 시드 행이 없음 — "
                    "요청으로 만드는 데이터는 rows 에 적지 않고 checks 에 만드는 요청을 쓴다"
                )
                continue
            fields = {f.name for f in m.fields}
            bad = [k for k in use.values if k not in fields or k == "id"]
            if bad:
                out.append(
                    f"공개 테스트 {t.test_id} rows: {m.name} 에 없는 칸(또는 id) {', '.join(bad)} — 시드 groups 의 values 와 같은 칸으로"
                )
    return out


def _fit_public(
    contract: TaskContract,
    plan: HiddenPlan,
    seeds: dict[str, SeedRule],
    names: Mapping[str, str],
    reserved: int,
    notes: list[str],
) -> TaskContract:
    """처음 값마다 공개 테스트 몫 PUBLIC_PER_GROUP 행이 비게 시드 끝에 덧붙인다.

    2026-11-08 상류 평가: 시드 172행을 숨은 케이스가 11~172 전부 가져가 공개 몫 상수가 하나도 없었고, 공개 테스트가 없는 이름
    (PUBLIC_ORDER_READY)을 짐작해 재요청으로도 못 고쳤다 (2/2). 행 수는 LLM 이 아니라 코드가 맞춘다."""
    for _ in range(sum(len(s.groups) for s in contract.seed) + 1):
        # 고유 칸을 정한 그룹은 덧붙이지 않는다 — 이미 시드에 있는 그룹이라 같은 값의 행이 둘이 된다 (public_free 가 작은 id 의 행으로 대신)
        short = [
            (e, v, PUBLIC_PER_GROUP - len(ids))
            for e, v, ids in public_free(contract, plan, reserved)
            if public_per_group(contract, e, v) == PUBLIC_PER_GROUP
        ]
        short = [x for x in short if x[2] > 0]
        if not short:
            break
        eid, found, deficit = short[0]
        seed = seeds[eid]
        values = {**common_values(seed), **found}
        first = seed.rows + 1
        last = max(seed.rows, reserved) + deficit
        tail = max(seed.groups, key=lambda g: g.last) if seed.groups else None
        if tail is not None and tail.last == seed.rows and dict(tail.values) == values:
            groups = [
                g if g is not tail else g.model_copy(update={"last": last}) for g in seed.groups
            ]
        else:
            groups = [*seed.groups, SeedGroup(first=first, last=last, values=dict(values))]
        seeds[eid] = seed.model_copy(update={"rows": last, "groups": groups})
        notes.append(
            f"{names.get(eid, eid)} 시드 {seed.rows} → {last}행: id {first}~{last} ({_cond(values)}) 덧붙임 "
            f"— 공개 테스트 몫 {PUBLIC_PER_GROUP}행 (숨은 케이스 범위·작은 id {reserved}행 밖)"
        )
        contract = contract.model_copy(update={"seed": [seeds[s.entity_id] for s in contract.seed]})
    return contract


def case_rows_problems(
    contract: TaskContract, plan: HiddenPlan, manifest: TemplateManifest
) -> list[str]:
    """숨은 케이스의 data — 처음 값 필드, 고유 칸, 동시 요청 사유의 버스트 행, 버스트 행 수, 배정 가능 여부."""
    out: list[str] = []
    need, missing = burst_rows_needed(plan, manifest)
    peak = peak_reasons(manifest)
    for c in plan.cases:
        if c.intended_reason in peak and not any(d.burst for d in c.data):
            out.append(
                f"숨은 케이스 {c.case_id}: 의도한 사유 {c.intended_reason} 는 동시 요청으로만 재현되는데 data 에 버스트 행이 없음 "
                f"(성립 조건이 {', '.join(peak[c.intended_reason])} 를 씀) — 버스트 행(burst: true, "
                f"{' × '.join(manifest.burst_rows)} 행 이상)을 둔다"
            )
        for d in c.data:
            out += _value_problems(
                f"숨은 케이스 {c.case_id} data", d.initial, contract, d.entity_id
            )
            fixed = sorted(set(d.initial) & unique_fields(contract, d.entity_id))
            if fixed and d.rows > 1:
                out.append(
                    f"숨은 케이스 {c.case_id} data: 고유 칸 {', '.join(fixed)} 을 같은 처음 값으로 {d.rows}행에 둠 — "
                    "고유 칸은 처음 값에서 뺀다 (행마다 다른 기본값)"
                )
            if not d.burst:
                continue
            if missing:
                out.append(
                    f"숨은 케이스 {c.case_id}: 버스트 행 수를 확인할 조절값이 harness 에 없음: {', '.join(missing)}"
                )
            elif need is not None and d.rows < need:
                formula = " × ".join(f"{n}({plan.harness[n]})" for n in manifest.burst_rows)
                out.append(
                    f"숨은 케이스 {c.case_id}: 버스트 행 {d.rows}개 < {formula} = {need} — 요청마다 다른 행을 쓴다"
                )
    return out + allocate_rows(contract, plan).problems


def _default_note(s: SeedRule, entity: Entity | None) -> str:
    """그룹이 정하지 않은 필드의 실제 값 — 코드(seed_sql.default_value)가 행마다 채운다.
    2026-11-10 gen-item: 시드 줄에 없던 playerId(실제 'playerId-1')를 시작 코드 테스트가 'player-1' 로 짐작해 기존 테스트 5개가 실패,
    재생성 예산을 다 썼다."""
    if entity is None or s.rows == 0:
        return ""
    grouped = {k for g in s.groups for k in g.values}
    shown: list[str] = []
    for f in _insert_fields(entity, s):
        if f.name in grouped:
            continue
        v = default_value(f, 1)
        if time_kind(f) is not None:  # 행마다 다른 날짜·시각, id 순서와 무관 (seed_sql.time_value)
            shown.append(f"`{f.name}`=행마다 다른 값, id 순서와 무관 (예: id 1 → '{v}')")
            continue
        if (
            isinstance(v, bool)
            or not isinstance(v, (int, str))
            or (isinstance(v, str) and v != f"{f.name}-1"[:20])
        ):
            shown.append(f"`{f.name}`={v}")  # 행마다 같은 값 (enum 첫 값·false·고정 시각)
        elif isinstance(v, int):
            shown.append(f"`{f.name}`=그 행의 id")
        else:
            shown.append(f"`{f.name}`='{f.name}-<id>' (예: id 1 → '{v}')")
    return " — 그 밖의 필드는 행마다: " + ", ".join(shown) if shown else ""


def seed_markdown(s: SeedRule, name: str, entity: Entity | None = None) -> str:
    """README·생성기 입력의 시드 한 줄 — 예: `Delivery` 200행 (id 1~200) — id 1~150: `status`=READY · …
    entity 를 주면 그룹이 정하지 않은 필드의 실제 값도 적는다 (_default_note)."""
    if s.rows == 0:
        head = f"- `{name}` 0행 (처음엔 비어 있음)"
    else:
        head = f"- `{name}` {s.rows}행 (id 1~{s.rows})"
    parts = [
        f"id {g.first}~{g.last}: " + ", ".join(f"`{k}`={v}" for k, v in g.values.items())
        for g in sorted(s.groups, key=lambda g: g.first)
        if g.values
    ]
    if parts:
        head += " — " + " · ".join(parts)
    head += _default_note(s, entity)
    if s.rules:
        head += " — " + " · ".join(s.rules)
    if s.rows > RESERVED_ROWS:
        # 채점 테스트는 큰 id 범위를 나눠 쓴다 (allocate_rows) — 시작 코드·지원자 테스트가 그 행을 바꾸면 채점이 흔들린다
        head += f" — 테스트에서 바꿔도 되는 행은 id 1~{RESERVED_ROWS}"
    return head
