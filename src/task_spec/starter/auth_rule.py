"""권한 규칙 칸(Endpoint.auth) — 요청자 행·헤더 값·규칙 문장을 코드가 만든다 [2026-11-09]. state_rule.py 와 같은 방식.

2026-11-09 알파 smilegate_2026#002: 같은 명세의 계약 14개가 권한을 4가지로 정했고(8개는 '요청자 식별자'뿐), 공개 테스트 예시의
통과 값이 시드와 맞지 않았다 — 통과하는 등급의 행이 시드에 없어 예시를 `<MASTER 등급 멤버의 memberId>` 로 쓰거나, 지어낸 값으로
모든 요청이 403 에서 끝났다. 그래서 규칙은 칸으로 받고 나머지는 기계적으로:
- role: 시드 끝에 요청자 행 두 개(통과용 — allowed 첫 값, 거절용 — allowed 밖 값)를 덧붙이고, 숨은 케이스·공개 몫 배정에서 뺀다
  (권한 필드가 바꾸려는 상태와 같은 필드일 수 있다 — 다른 테스트가 요청자 행을 바꾸면 요청 도중 권한을 잃는다).
- 그 행의 식별 값이 헤더 통과·거절 값 → 테스트 도우미 상수 (header_values).
- 헤더 constraints 문장은 칸에서 코드가 쓴다 → README·시작 코드·참조 구현이 같은 규칙을 본다.
- 칸이 필요한지는 응답 코드로 판정한다 (401·403 또는 권한 사유 코드) — 헤더가 있다는 것만으로는 아니다 (요청자를 기록용으로만 받는 과제가 많다).
- other: 칸 밖의 규칙은 글로 받고 '자동 보장 안 됨'으로 사람 확인에 올린다.
- issued [2026-11-24]: 헤더 값이 다른 API(로그인)의 응답으로 발급된다 — 시드 끝에 로그인 행 하나(자격 증명은 발급 API 요청 칸과
  같은 이름의 엔티티 필드 값)를 덧붙이고, 테스트 도우미가 그 행으로 로그인해 값을 받는다. 거절 값은 발급하지 않은 값.
  발급 API 자체의 401(자격 증명 불일치)은 칸을 요구하지 않는다 — 가리킬 요청 헤더가 없다 (운영 빌드 ct-f6087a7ba679-b1).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from task_spec.schemas import AuthRule, HiddenPlan, SeedGroup, SeedRule, SeedScalar, TaskContract
from task_spec.schemas.build import ContractField, Endpoint, Entity
from task_spec.starter.field_types import enum_values, non_null
from task_spec.starter.field_types import parse as parse_field
from task_spec.starter.seed_sql import default_value, time_kind
from task_spec.starter.state_rule import seed_values

DENIED = (401, 403)
_REASON = re.compile(r"\b(PERMISSION_DENIED|UNAUTHORIZED|FORBIDDEN|ACCESS_DENIED)\b")
_PATH_PARAM = re.compile(r"\{(\w+)\}")
_QUOTED = re.compile(r"""["']([A-Za-z0-9_-]+)["']""")
_FIXED_DENIED = ("guest", "denied-user", "anonymous")
# issued 의 거절 값 — 발급하지 않은 값은 어떤 설계든 거절해야 한다
ISSUED_DENIED = "not-issued-session"
_BODY_METHODS = ("POST", "PUT", "PATCH")
FLAG_OTHER = "권한 규칙 자동 보장 없음 (kind=other — 사람 확인): "


def auth_required(e: Endpoint) -> bool:
    """권한 실패를 내는 HTTP 엔드포인트 — 401·403 응답, 또는 응답·필드 글에 권한 사유 코드.
    명령·파일 인터페이스(method 없음)는 요청 헤더가 없어 칸을 채울 수 없다 — 권한 사유 코드가 있어도 보지 않는다
    (예: 데이터 직군 권한 산출물 과제의 종료 코드 설명에 PERMISSION_DENIED)."""
    if e.method is None:
        return False
    if any(r.status in DENIED for r in e.responses):
        return True
    texts = [r.description for r in e.responses]
    texts += [f.constraints or "" for f in (*e.headers, *e.request)]
    return any(_REASON.search(t) for t in texts)


def _entity(contract: TaskContract, entity_id: str | None) -> Entity | None:
    return next((m for m in contract.entities if m.entity_id == entity_id), None)


def _endpoint(contract: TaskContract, endpoint_id: str | None) -> Endpoint | None:
    return next((e for e in contract.endpoints if e.endpoint_id == endpoint_id), None)


def issuers(contract: TaskContract) -> set[str]:
    """issued 칸이 가리키는 발급 엔드포인트 id — 그 401 은 본문 자격 증명 불일치라 칸을 요구하지 않는다."""
    return {
        e.auth.issued_by
        for e in contract.endpoints
        if e.auth is not None and e.auth.kind == "issued" and e.auth.issued_by
    }


@dataclass(frozen=True)
class LockedApi:
    """시작 코드가 부를 수 없는 기존 API — 새로 만드는 발급 API(로그인)가 주는 값이 필요하다."""

    endpoint: Endpoint
    rule: AuthRule
    issuer: Endpoint


def locked_existing(contract: TaskContract, existing: Sequence[Endpoint]) -> list[LockedApi]:
    """기존 API 중 새 발급 API 의 값이 필요한 것 — 시작 코드에는 발급 API 가 없어 통과하는 값을 만들 수 없고, 새 기능을 구현하면
    그 요청은 거절된다. 시작 코드 생성기 입력(generate.existing_apis)과 시작 코드 검사(static_check._starter_issued_calls)가 함께 쓴다
    — 2026-11-24 운영 빌드 ct-f6087a7ba679-b2: 검사만 알고 생성기는 몰라, '기존 API 로 기존 기능 테스트를 하나 이상'(gen_starter)과
    '이 API 는 부르지 않는다'(검사)가 부딪쳐 시작 코드 생성이 재시도 한도에서 끝났다 (기존 API 가 모두 잠긴 계약)."""
    by_id = {e.endpoint_id: e for e in contract.endpoints}
    return [
        LockedApi(e, e.auth, by_id[e.auth.issued_by])
        for e in existing
        if e.auth is not None
        and e.auth.kind == "issued"
        and e.auth.issued_by in by_id
        and by_id[e.auth.issued_by] not in existing
    ]


def issued_rule(contract: TaskContract) -> AuthRule | None:
    """도우미가 세션을 받을 issued 칸 — 로그인 행까지 정해진 첫 칸 (헤더가 하나뿐인지는 auth_problems 가 본다)."""
    return next(
        (
            e.auth
            for e in contract.endpoints
            if e.auth is not None and e.auth.kind == "issued" and e.auth.rows
        ),
        None,
    )


def login_candidates(contract: TaskContract, rule: AuthRule) -> list[Entity]:
    """발급 API 요청 칸(자격 증명)을 모두 가진 엔티티 — 로그인 사용자. 칸 이름은 계약이 선언한 것 (낱말 목록으로 짐작하지 않는다)."""
    src = _endpoint(contract, rule.issued_by)
    if src is None or not src.request:
        return []
    names = {f.name for f in src.request}
    if "id" in names:
        return []
    return [m for m in contract.entities if names <= {f.name for f in m.fields}]


def _fit_login_entity(contract: TaskContract, rule: AuthRule, notes: list[str]) -> AuthRule:
    """entity 는 계약에서 도출한다 — 발급 API 요청 칸을 모두 가진 엔티티가 하나면 그것. 적힌 entity 가 후보면 그대로, 후보가 없거나
    여럿인데 적힌 것이 후보가 아니면 그대로 두고 auth_problems 가 말한다.
    2026-11-24 v32 회귀(세션 유지 과제): 재료가 세션 엔티티를 요구하자 entity 를 Session 으로 적어(로그인 칸은 User 에) 계약이 실패했다
    — 계약만 보고 정할 수 있는 값을 묻고 틀리면 고치는 대신, 묻지 않는다 (모호할 때만 entity 로 고른다)."""
    found = login_candidates(contract, rule)
    if rule.entity in {m.entity_id for m in found} or len(found) != 1:
        return rule
    m = found[0]
    old = _entity(contract, rule.entity)
    note = (
        f"issued 칸의 로그인 엔티티를 {m.name} 로"
        + (f" ({old.name} 대신)" if old is not None else "")
        + (" — 발급 API 요청 칸을 모두 가진 엔티티")
    )
    if note not in notes:
        notes.append(note)
    return rule.model_copy(update={"entity": m.entity_id})


@dataclass(frozen=True)
class Credential:
    """발급 API 요청 칸 하나의 행별 값 — 시드 SQL 과 같은 규칙(seed_sql._value): 그 행의 그룹 값, 없으면 기본값
    (글자 '<칸>-<행>' 20자까지 · 정수는 행 번호 · enum 첫 값 · boolean false)."""

    name: str
    groups: tuple[tuple[int, int, SeedScalar], ...]
    default: Literal["text", "row", "fixed"]
    fixed: SeedScalar | None = None


def credentials(contract: TaskContract, rule: AuthRule) -> list[Credential] | None:
    """issued 로그인 엔티티의 자격 증명 칸마다 행별 값 — 테스트 도우미가 행 번호로 로그인 본문을 만든다 (테스트가 짐작하지 않게).
    2026-11-24 세션 알파: 도우미가 로그인 행 하나의 본문만 줘, 케이스·공개 몫 행마다 로그인하려던 테스트가 "user-"+id·"pass-"+id 로
    지어 모든 구현에서 401. 날짜·시각·외래 키 칸처럼 값을 행으로 정할 수 없으면 None."""
    m = _entity(contract, rule.entity)
    src = _endpoint(contract, rule.issued_by)
    seed = next((x for x in contract.seed if x.entity_id == rule.entity), None)
    if m is None or src is None or seed is None or not src.request:
        return None
    fields = {f.name: f for f in m.fields}
    fks = {
        fk for fk, _ in m.fk_pairs()
    }  # 외래 키는 시드 SQL 이 대상 행 안에서 돌린다 — 자격 증명으로 쓰지 않는다
    out: list[Credential] = []
    for req in src.request:
        f = fields.get(req.name)
        if f is None or f.name in fks:
            return None
        ft = non_null(parse_field(f.type))
        groups = tuple(
            (g.first, g.last, g.values[f.name]) for g in seed.groups if f.name in g.values
        )
        if time_kind(f) is not None:
            return None
        if ft.kind == "enum" and ft.values:
            out.append(Credential(f.name, groups, "fixed", ft.values[0]))
        elif ft.kind in ("integer", "number"):
            out.append(Credential(f.name, groups, "row"))
        elif ft.kind == "boolean":
            out.append(Credential(f.name, groups, "fixed", False))
        else:
            out.append(Credential(f.name, groups, "text"))
    return out


def _response_fields(contract: TaskContract, e: Endpoint) -> set[str] | None:
    """성공 응답 본문의 칸 이름 — body 칸과 returns 가 가리키는 엔티티·모양의 칸. 성공 응답이 없으면 None."""
    ok = e.success()
    if ok is None:
        return None
    names = {f.name for f in ok.body}
    if ok.returns:
        shape = next(
            (m for m in contract.data_shapes() if m.entity_id == ok.returns.removesuffix("[]")),
            None,
        )
        names |= {f.name for f in shape.fields} if shape is not None else set()
    return names


def _field(m: Entity, name: str | None) -> ContractField | None:
    return next((f for f in m.fields if f.name == name), None)


def _choices(contract: TaskContract, m: Entity, attribute: str) -> list[str]:
    """그 필드가 가질 수 있는 값 — 상태 칸 값, 시드 그룹 값, enum(…) 형식·따옴표 값 순."""
    if m.state is not None and m.state.field == attribute:
        return list(m.state.values)
    out = seed_values(contract, m.entity_id, attribute)
    f = _field(m, attribute)
    if f is not None:
        out += enum_values(f.type) or ()  # 형식이 정한 값 목록 — 계약 형식 해석 한 곳 (#172)
        out += _QUOTED.findall(f.constraints or "")
    return list(dict.fromkeys(out))


def _scope_value(contract: TaskContract, entity_id: str, name: str) -> SeedScalar | None:
    """같은 경로 변수 범위 값 — 그 엔티티 시드 그룹에서 가장 많은 값 (숨은·공개 테스트가 쓰는 행과 같은 범위)."""
    seen: Counter[SeedScalar] = Counter()
    for s in contract.seed:
        if s.entity_id == entity_id:
            for g in s.groups:
                if name in g.values:
                    seen[g.values[name]] += g.last - g.first + 1
    return seen.most_common(1)[0][0] if seen else None


def _identity(m: Entity, identifies: str, row: int) -> str:
    if identifies == "id":
        return str(row)
    f = _field(m, identifies)
    return str(default_value(f, row)) if f is not None else str(row)


def sentence(rule: AuthRule, contract: TaskContract) -> str:
    """헤더 constraints 에 들어갈 문장 (≤200자)."""
    tail = f" — 아니면 {rule.denied_status}"
    if rule.ok_value is not None:
        tail += f" (통과 예 {rule.ok_value}, 거절 예 {rule.denied_value})"
    if rule.kind == "fixed":
        return (f"값이 {'·'.join(rule.allowed)} 이면 통과" + tail)[:200]
    if rule.kind == "issued":
        src = _endpoint(contract, rule.issued_by)
        where = f"{src.method} {src.path}" if src is not None else rule.issued_by
        put = f"앞에 '{rule.prefix}' 를 붙여" if rule.prefix else "그대로"
        text = f"{where} 성공 응답의 {rule.issued_field} 값을 {put} 넣는다 — 없거나 발급하지 않은 값이면 {rule.denied_status}"
        m = _entity(contract, rule.entity)
        if src is not None and src.request and m is not None:
            # 시드 값은 평문 — 해시로 비교하는 구현은 시드 행으로 로그인하지 못한다 (지원자에게도 같은 규칙)
            creds = "·".join(f.name for f in src.request)
            text += f". 자격 증명({creds})은 시드 {m.name} 행의 같은 이름 칸 값과 그대로 비교한다 (시드 값은 평문)"
        return text[:200]
    if rule.kind == "role":
        m = _entity(contract, rule.entity)
        name = m.name if m is not None else rule.entity
        scope = "".join(f", {f} 가 경로의 {f} 와 같고" for f in rule.same_as_path)
        if rule.above:
            what = "요청한 새 값" if rule.above == "requested" else "대상의 지금 값"
            cmp = "보다 높거나 같으면" if rule.or_equal else "보다 높으면"
            only = f"{'·'.join(rule.allowed)} 이고 " if rule.allowed else ""
            return (
                f"요청자 {name}.{rule.identifies}{scope} {rule.attribute} 가 {only}{what}{cmp} 통과"
                f" (순서 {'<'.join(rule.order)})" + tail
            )[:200]
        return (
            f"요청자 {name}.{rule.identifies}{scope} {rule.attribute} 가 {'·'.join(rule.allowed)} 이면 통과"
            + tail
        )[:200]
    return (rule.description or "권한 규칙")[:200]


def _key(rule: AuthRule) -> tuple[object, ...]:
    return (
        rule.kind,
        rule.entity,
        rule.identifies,
        rule.attribute,
        tuple(rule.allowed),
        tuple(rule.order),
        rule.above,
        rule.or_equal,
        tuple(rule.same_as_path),
        rule.issued_by,
        rule.issued_field,
        rule.prefix,
    )


def fit_auth(contract: TaskContract, seed_rows: bool = True) -> tuple[TaskContract, list[str]]:
    """칸이 있는 엔드포인트마다 요청자 행·헤더 값을 채우고 헤더 constraints 를 칸에서 쓴다. 두 번 불러도 같다.
    role 은 행 시드 템플릿에서만 행을 덧붙인다 (아니면 공개 테스트 예시 값으로 — header_values).
    화면 과제의 issued 는 그대로 둔다 — 권한 검사·도우미가 쓰지 않고(테스트가 서버 응답을 직접 정한다), 칸에서 쓴 문장이 계약의 헤더
    설명을 덮으면 README 가 바뀐다 (2026-11-24 v32 회귀: 토큰 갱신 화면 과제에서 갱신 API 도 발급하는데 '로그인 응답의 값' 으로 덮임)."""
    notes: list[str] = []
    seeds = {s.entity_id: s for s in contract.seed}
    done: dict[tuple[object, ...], AuthRule] = {}
    endpoints: list[Endpoint] = []
    for e in contract.endpoints:
        rule = e.auth
        if rule is None or (rule.kind == "issued" and contract.interface_kind == "ui"):
            endpoints.append(e)
            continue
        if rule.kind == "issued" and seed_rows:
            rule = _fit_login_entity(contract, rule, notes)
        if rule.ok_value is None and _key(rule) in done:
            rule = rule.model_copy(
                update={
                    k: getattr(done[_key(rule)], k) for k in ("rows", "ok_value", "denied_value")
                }
            )
        elif rule.ok_value is None and rule.kind == "fixed" and rule.allowed:
            denied = next(
                v for v in (*_FIXED_DENIED, "x-" + rule.allowed[0]) if v not in rule.allowed
            )
            rule = rule.model_copy(update={"ok_value": rule.allowed[0], "denied_value": denied})
        elif rule.ok_value is None and rule.kind == "role" and seed_rows:
            rule = _add_requesters(contract, rule, seeds, notes)
        elif rule.kind == "issued" and rule.denied_value is None:
            rule = rule.model_copy(update={"denied_value": f"{rule.prefix or ''}{ISSUED_DENIED}"})
            if seed_rows:
                rule = _add_login_row(contract, rule, seeds, notes)
        done.setdefault(_key(rule), rule)
        headers = [
            h.model_copy(update={"constraints": sentence(rule, contract)})
            if h.name == rule.header
            else h
            for h in e.headers
        ]
        endpoints.append(e.model_copy(update={"auth": rule, "headers": headers}))
    out = contract.model_copy(update={"endpoints": endpoints, "seed": list(seeds.values())})
    return (contract, notes) if out == contract else (out, notes)


def requester_levels(contract: TaskContract, rule: AuthRule) -> tuple[str | None, str | None]:
    """role 규칙의 (통과용 요청자 attribute 값, 거절용 값) — 요청자 행 두 개가 갖는 값. 정할 수 없으면 None.
    등급 비교(above)면 맨 위 등급(allowed 가 있으면 그 안에서)이 어떤 대상·요청보다 높아 통과하고, 맨 아래가 거절.
    그 밖은 allowed 첫 값이 통과, allowed 밖 값이 거절."""
    if rule.above:
        if not rule.order:
            return None, None
        tops = [v for v in rule.order if not rule.allowed or v in rule.allowed]
        good = tops[-1] if tops else None
        return good, (rule.order[0] if tops and rule.order[0] != good else None)
    m = _entity(contract, rule.entity)
    if not rule.allowed or m is None or not rule.attribute:
        return None, None
    bad = next((v for v in _choices(contract, m, rule.attribute) if v not in rule.allowed), None)
    return rule.allowed[0], bad


def _add_requesters(
    contract: TaskContract, rule: AuthRule, seeds: dict[str, SeedRule], notes: list[str]
) -> AuthRule:
    """시드 끝에 요청자 행 두 개 (통과용·거절용). 값을 정할 수 없으면 칸을 그대로 두고 auth_problems 가 말한다."""
    m = _entity(contract, rule.entity)
    seed = seeds.get(rule.entity or "")
    if m is None or seed is None or not rule.attribute or not rule.identifies:
        return rule
    good, bad = requester_levels(contract, rule)
    if good is None:
        return rule
    scope: dict[str, SeedScalar] = {}
    for f in rule.same_as_path:
        v = _scope_value(contract, m.entity_id, f)
        if v is None:
            return rule
        scope[f] = v
    if bad is None:
        return rule
    rows = seed.rows
    ok_row, bad_row = rows + 1, rows + 2
    groups = [
        *seed.groups,
        SeedGroup(first=ok_row, last=ok_row, values={**scope, rule.attribute: good}),
        SeedGroup(first=bad_row, last=bad_row, values={**scope, rule.attribute: bad}),
    ]
    seeds[m.entity_id] = seed.model_copy(update={"rows": bad_row, "groups": groups})
    ok_v, bad_v = _identity(m, rule.identifies, ok_row), _identity(m, rule.identifies, bad_row)
    notes.append(
        f"{m.name} 시드에 요청자 행 덧붙임: id {ok_row} ({rule.attribute}={good} — 통과, {rule.header}={ok_v}), "
        f"id {bad_row} ({rule.attribute}={bad} — 거절, {rule.header}={bad_v}) — 숨은 케이스·공개 몫 배정에서 뺌"
    )
    return rule.model_copy(
        update={"rows": [ok_row, bad_row], "ok_value": ok_v, "denied_value": bad_v}
    )


def _add_login_row(
    contract: TaskContract, rule: AuthRule, seeds: dict[str, SeedRule], notes: list[str]
) -> AuthRule:
    """시드 끝에 로그인 행 하나 — 발급 API 요청 칸마다 같은 이름 필드에 행 번호로 만든 값 (형식이 email 인 칸은 이메일 모양).
    값을 그룹에 적어 둔다 — 필수가 아닌 칸은 그룹 값이 없으면 NULL 로 들어가 로그인할 수 없다 (seed_sql._insert_fields).
    정할 수 없으면 칸을 그대로 두고 auth_problems 가 말한다."""
    m = _entity(contract, rule.entity)
    seed = seeds.get(rule.entity or "")
    src = _endpoint(contract, rule.issued_by)
    if m is None or seed is None or src is None or not src.request:
        return rule
    fields = {f.name: f for f in m.fields}
    if any(f.name not in fields or f.name == "id" for f in src.request):
        return rule
    row = seed.rows + 1
    values: dict[str, SeedScalar] = {}
    for f in src.request:
        mf = fields[f.name]
        if mf.type.strip().lower() == "email":
            values[f.name] = f"user{row}@example.com"
        else:
            v = default_value(mf, row)
            values[f.name] = v if isinstance(v, (str, int, bool)) else str(v)
    groups = [*seed.groups, SeedGroup(first=row, last=row, values=values)]
    seeds[m.entity_id] = seed.model_copy(update={"rows": row, "groups": groups})
    shown = ", ".join(f"{k}={v}" for k, v in values.items())
    notes.append(
        f"{m.name} 시드에 로그인 행 덧붙임: id {row} ({shown}) — {src.method} {src.path} 로 {rule.header} 값을 받는다, "
        "숨은 케이스·공개 몫 배정에서 뺌"
    )
    return rule.model_copy(update={"rows": [row]})


def auth_problems(contract: TaskContract, seed_rows: bool = True) -> list[str]:
    """칸이 필요한데 없음 · 칸이 계약과 안 맞음 → 재요청 사유."""
    out: list[str] = _issued_headers(contract)
    issuing = issuers(contract)
    for e in contract.endpoints:
        rule = e.auth
        label = f"{e.method or ''} {e.path} auth".strip()
        if rule is None:
            if auth_required(e) and e.endpoint_id not in issuing:
                msg = (
                    f"{label}: 권한 실패(401·403·권한 사유 코드)를 내는데 auth 칸이 없음 — 누가 통과하는지 auth 에 적는다 "
                    '(예: {"kind": "role", "header": "X-Actor", "denied_status": 403, "entity": "m1", "identifies": "id", '
                    '"attribute": "role", "allowed": ["ADMIN"]})'
                )
                if not e.headers:
                    msg += (
                        f". 요청 헤더 없이 본문 자격 증명으로 401 을 내는 로그인·발급 API 면 이 엔드포인트에는 칸을 두지 않고, 발급한 값을 "
                        f'헤더로 받는 엔드포인트의 auth 에 {{"kind": "issued", "issued_by": "{e.endpoint_id}", …}} 를 적는다'
                    )
                out.append(msg)
            continue
        if rule.header not in {h.name for h in e.headers}:
            out.append(f"{label}: header {rule.header} 가 이 엔드포인트 headers 에 없음")
        if rule.denied_status not in {r.status for r in e.responses}:
            out.append(
                f"{label}: denied_status {rule.denied_status} 가 이 엔드포인트 responses 에 없음"
            )
        if rule.kind == "fixed" and not rule.allowed:
            out.append(f"{label}: fixed 인데 allowed(통과하는 헤더 값)가 비었음")
        if rule.kind == "other" and not (rule.description or "").strip():
            out.append(f"{label}: other 인데 description(규칙 한 줄)이 비었음")
        if rule.kind == "role":
            out += _role_problems(contract, e, rule, label, seed_rows)
        if rule.kind == "issued":
            out += _issued_problems(contract, e, rule, label, seed_rows)
    return out


def _issued_headers(contract: TaskContract) -> list[str]:
    """issued 칸은 헤더 하나·발급 방법 하나 — 도우미가 세션 하나를 받아 모든 요청에 쓴다."""
    ways = {
        (e.auth.header, e.auth.issued_by, e.auth.issued_field, e.auth.prefix)
        for e in contract.endpoints
        if e.auth is not None and e.auth.kind == "issued"
    }
    if len(ways) < 2:
        return []
    shown = "; ".join(f"{h} ← {by}.{fld}" for h, by, fld, _ in sorted(ways, key=str))
    return [
        f"issued 칸이 서로 다름 ({shown}) — 발급한 값을 받는 헤더·발급 엔드포인트·응답 칸·prefix 는 모든 엔드포인트에서 같게 적는다"
    ]


def _issued_problems(
    contract: TaskContract, e: Endpoint, rule: AuthRule, label: str, seed_rows: bool
) -> list[str]:
    src = _endpoint(contract, rule.issued_by)
    if not rule.issued_by:
        return [f"{label}: issued 인데 issued_by(값을 발급하는 엔드포인트 id, 예: 로그인)가 비었음"]
    if src is None:
        return [f"{label}: issued_by {rule.issued_by} 가 계약 엔드포인트에 없음"]
    if src.endpoint_id == e.endpoint_id:
        return [
            f"{label}: issued_by 가 자기 자신 — 값을 발급하는 다른 엔드포인트(로그인 등)를 적는다"
        ]
    out: list[str] = []
    where = f"issued_by {src.endpoint_id}({src.method or ''} {src.path})"
    if (src.method or "").upper() not in _BODY_METHODS or _PATH_PARAM.search(src.path):
        out.append(
            f"{label}: {where} 는 경로 변수 없는 POST·PUT·PATCH 여야 한다 — 자격 증명을 요청 본문으로 보낸다"
        )
    if not src.request:
        out.append(f"{label}: {where} 에 요청 본문 칸(자격 증명)이 없음")
    if not rule.issued_field:
        out.append(f"{label}: issued 인데 issued_field(발급 응답 본문에서 값이 담긴 칸)가 비었음")
    else:
        names = _response_fields(contract, src)
        if names is None:
            out.append(f"{label}: {where} 에 성공(2xx) 응답이 없음")
        elif rule.issued_field not in names:
            out.append(
                f"{label}: issued_field {rule.issued_field} 가 {where} 성공 응답 본문에 없음 — 응답 body 나 returns 에 그 칸을 적는다"
            )
    if not seed_rows:
        return out
    names = ", ".join(f.name for f in src.request)
    found = login_candidates(contract, rule)
    m = _entity(contract, rule.entity)
    if src.request and not found:
        shown = "; ".join(
            f"{x.name}({', '.join(f.name for f in x.fields)})" for x in contract.entities
        )
        out.append(
            f"{label}: {where} 요청 칸 {names} 를 모두 가진 엔티티가 없음 — 자격 증명 칸 이름은 로그인 사용자 엔티티의 필드 이름과 같게 "
            f"(코드가 그 필드 값으로 로그인 행을 만든다. 엔티티: {shown})"
        )
    elif len(found) > 1 and (m is None or m not in found):
        out.append(
            f"{label}: {where} 요청 칸 {names} 를 가진 엔티티가 여럿 ({', '.join(f'{x.entity_id} {x.name}' for x in found)}) — "
            "issued 의 entity 에 로그인 사용자 엔티티 id 를 적는다"
        )
    elif m is not None and m in found:
        if not any(x.entity_id == m.entity_id for x in contract.seed):
            out.append(
                f"{label}: 로그인 엔티티 {m.name} 에 시드가 없음 — 로그인 행을 덧붙일 시드를 둔다"
            )
        elif credentials(contract, rule) is None:
            out.append(
                f"{label}: {where} 요청 칸 {names} 중 날짜·시각·외래 키 칸이 있음 — 자격 증명은 글자·정수 칸으로 (행마다 값을 코드가 정한다)"
            )
    if not out and not rule.rows:
        out.append(
            f"{label}: 로그인 행을 만들 수 없음 — issued_by 요청 칸과 로그인 사용자 엔티티를 확인한다"
        )
    return out


def _role_problems(
    contract: TaskContract, e: Endpoint, rule: AuthRule, label: str, seed_rows: bool
) -> list[str]:
    m = _entity(contract, rule.entity)
    if m is None:
        return [f"{label}: role 의 entity {rule.entity} 가 계약 엔티티에 없음"]
    names = [f.name for f in m.fields]
    out: list[str] = []
    for what in ("identifies", "attribute"):
        v = getattr(rule, what)
        if not v:
            out.append(f"{label}: role 인데 {what} 가 비었음")
        elif v != "id" and v not in names:
            out.append(
                f"{label}: {what} {v} 가 {m.name} 필드에 없음 (있는 필드: {', '.join(names)})"
            )
    if rule.above or rule.order:
        out += _order_problems(contract, m, rule, label)
    elif not rule.allowed:
        out.append(
            f"{label}: role 인데 allowed(통과하는 {rule.attribute} 값)가 비었음 — 등급 비교 규칙이면 order·above 를 쓴다"
        )
    if rule.allowed and rule.attribute and (choices := _choices(contract, m, rule.attribute)):
        unknown = [v for v in rule.allowed if v not in choices]
        if unknown and m.state is not None and m.state.field == rule.attribute:
            out.append(f"{label}: allowed {', '.join(unknown)} 가 {m.name}.state values 에 없음")
    params = set(_PATH_PARAM.findall(e.path))
    for f in rule.same_as_path:
        if f not in names:
            out.append(f"{label}: same_as_path {f} 가 {m.name} 필드에 없음")
        elif f not in params:
            out.append(f"{label}: same_as_path {f} 가 경로 변수에 없음 ({e.path})")
        elif seed_rows and _scope_value(contract, m.entity_id, f) is None:
            out.append(
                f"{label}: same_as_path {f} 의 값이 시드 groups values 에 없음 — 범위 값은 values 에 정한다"
            )
    if rule.identifies and rule.identifies != "id":
        for s in contract.seed:
            if s.entity_id == m.entity_id and any(
                rule.identifies in g.values and g.last > g.first for g in s.groups
            ):
                out.append(
                    f"{label}: identifies {rule.identifies} 를 시드 values 에 고정함 — 행마다 다른 값이어야 요청자를 가리킨다 "
                    "(values 에서 빼면 코드가 행마다 다른 값을 넣는다)"
                )
                break
    if seed_rows and not out and rule.ok_value is None:
        out.append(
            f"{label}: 거절용 요청자 행을 만들 수 없음 — {rule.attribute} 의 allowed 밖 값이 없음 "
            "(state values·시드 값·enum 중 하나는 allowed 밖이어야 한다)"
        )
    return out


def _order_problems(contract: TaskContract, m: Entity, rule: AuthRule, label: str) -> list[str]:
    """등급 비교 role — order 는 attribute 의 값들(낮은 것 → 높은 것), above 는 requested·target 중 하나.
    2026-11-09 fixmt-be2: '요청자 등급이 바꾸려는 등급보다 높아야' 를 칸으로 못 적어 other 로 빠졌다."""
    out: list[str] = []
    if not rule.above:
        out.append(f"{label}: order 를 썼으면 above(requested·target)도 적는다")
    if len(rule.order) < 2 or len(set(rule.order)) != len(rule.order):
        out.append(f"{label}: order 는 서로 다른 값 2개 이상 (낮은 것 → 높은 것)")
    choices = _choices(contract, m, rule.attribute) if rule.attribute else []
    unknown = [v for v in rule.order if choices and v not in choices]
    if unknown:
        out.append(f"{label}: order 의 {', '.join(unknown)} 가 {m.name}.{rule.attribute} 값에 없음")
    if m.state is not None and m.state.field == rule.attribute:
        missing = [v for v in m.state.values if v not in rule.order]
        if missing:
            out.append(
                f"{label}: order 에 {m.name}.state 값 {', '.join(missing)} 이 빠짐 — 모든 값을 순서대로"
            )
    return out


def unreachable_targets(contract: TaskContract, plan: HiddenPlan) -> list[str]:
    """숨은 케이스의 대상 행을 통과용 요청자도 바꿀 수 없음 — 등급 비교 권한(above=target)에서 요청자 행의 실제 등급(requester_levels)과
    대상 행의 처음 값(data initial)을 규칙과 같은 비교(order·or_equal)로 견준다.
    2026-11-10 v19-be2: hc1 버스트 대상 240행을 MASTER 로 두어 어떤 요청자도 통과 못 했다 — 허용 구현이 모두 403 으로 hc1 을
    떨어지자 숨은 테스트를 '403 기대'로 다시 만들었고, 요청이 권한에서 끝나 기능 없는 시작 코드도 hc1 을 통과했다.
    #181: 예전엔 '대상이 order 의 맨 위'만 봐 allowed 로 요청자 상한이 내려가면 그 위 대상도 닿지 않는 것을 놓쳤다.
    above=requested 는 요청한 새 값이 계획에 칸으로 없어(scenario·expect 는 글) 보지 않는다."""
    out: list[str] = []
    for e in contract.endpoints:
        r = e.auth
        if r is None or r.kind != "role" or r.above != "target" or not r.attribute:
            continue
        good, _ = requester_levels(contract, r)
        if good is None or good not in r.order:
            continue
        level = r.order.index(good)
        reach = [v for i, v in enumerate(r.order) if i < level or (r.or_equal and i == level)]
        for c in plan.cases:
            for d in c.data:
                target = str(d.initial.get(r.attribute))
                if d.entity_id != r.entity or target not in r.order or target in reach:
                    continue
                out.append(
                    f"숨은 케이스 {c.case_id} 의 {d.entity_id} 대상 행 {r.attribute}={target} 은 {e.method} {e.path} 의 권한 규칙"
                    f"(요청자 {r.attribute} 가 대상보다 높아야{' — 같아도 됨' if r.or_equal else ''})에서 통과용 요청자({r.attribute}={good})도 "
                    "바꿀 수 없다 — 요청이 권한에서 끝나 숨은 요구를 재지 못한다. 대상 행은 "
                    + (
                        f"{', '.join(reach)} 중 하나로 둔다"
                        if reach
                        else "다른 값으로 둔다 (닿는 등급이 없음 — 권한 규칙을 다시 본다)"
                    )
                )
    return out


def auth_flags(contract: TaskContract) -> list[str]:
    return [
        FLAG_OTHER + f"{e.method or ''} {e.path}: {e.auth.description or ''}".strip()
        for e in contract.endpoints
        if e.auth is not None and e.auth.kind == "other"
    ]


def requester_rows(contract: TaskContract) -> dict[str, set[int]]:
    """엔티티 id → 요청자 행 id (숨은 케이스·공개 몫 배정에서 뺀다)."""
    out: dict[str, set[int]] = {}
    for e in contract.endpoints:
        if e.auth is not None and e.auth.entity and e.auth.rows:
            out.setdefault(e.auth.entity, set()).update(e.auth.rows)
    return out
