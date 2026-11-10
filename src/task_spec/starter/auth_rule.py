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
"""

from __future__ import annotations

import re
from collections import Counter

from task_spec.schemas import AuthRule, HiddenPlan, SeedGroup, SeedRule, SeedScalar, TaskContract
from task_spec.schemas.build import ContractField, Endpoint, Entity
from task_spec.starter.seed_sql import default_value
from task_spec.starter.state_rule import seed_values

DENIED = (401, 403)
_REASON = re.compile(r"\b(PERMISSION_DENIED|UNAUTHORIZED|FORBIDDEN|ACCESS_DENIED)\b")
_PATH_PARAM = re.compile(r"\{(\w+)\}")
_QUOTED = re.compile(r"""["']([A-Za-z0-9_-]+)["']""")
_ENUM_TYPE = re.compile(r"^enum\(([^)]*)\)$", re.I)
_FIXED_DENIED = ("guest", "denied-user", "anonymous")
FLAG_OTHER = "권한 규칙 자동 보장 안 됨 (kind=other — 사람 확인): "


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


def _field(m: Entity, name: str | None) -> ContractField | None:
    return next((f for f in m.fields if f.name == name), None)


def _choices(contract: TaskContract, m: Entity, attribute: str) -> list[str]:
    """그 필드가 가질 수 있는 값 — 상태 칸 값, 시드 그룹 값, enum(…) 형식·따옴표 값 순."""
    if m.state is not None and m.state.field == attribute:
        return list(m.state.values)
    out = seed_values(contract, m.entity_id, attribute)
    f = _field(m, attribute)
    if f is not None:
        if em := _ENUM_TYPE.match(f.type.strip()):
            out += [v.strip() for v in re.split(r"[|,]", em.group(1)) if v.strip()]
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
    )


def fit_auth(contract: TaskContract, seed_rows: bool = True) -> tuple[TaskContract, list[str]]:
    """칸이 있는 엔드포인트마다 요청자 행·헤더 값을 채우고 헤더 constraints 를 칸에서 쓴다. 두 번 불러도 같다.
    role 은 행 시드 템플릿에서만 행을 덧붙인다 (아니면 공개 테스트 예시 값으로 — header_values)."""
    notes: list[str] = []
    seeds = {s.entity_id: s for s in contract.seed}
    done: dict[tuple[object, ...], AuthRule] = {}
    endpoints: list[Endpoint] = []
    for e in contract.endpoints:
        rule = e.auth
        if rule is None:
            endpoints.append(e)
            continue
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


def _add_requesters(
    contract: TaskContract, rule: AuthRule, seeds: dict[str, SeedRule], notes: list[str]
) -> AuthRule:
    """시드 끝에 요청자 행 두 개 (통과용·거절용). 값을 정할 수 없으면 칸을 그대로 두고 auth_problems 가 말한다."""
    m = _entity(contract, rule.entity)
    seed = seeds.get(rule.entity or "")
    if m is None or seed is None or not rule.attribute or not rule.identifies:
        return rule
    if rule.above:
        # 등급 비교: 맨 위 등급은 어떤 대상·요청보다 높고(통과), 맨 아래는 어떤 것보다도 높지 않다(거절). allowed 가 있으면 그 안에서
        if not rule.order:
            return rule
        tops = [v for v in rule.order if not rule.allowed or v in rule.allowed]
        good = tops[-1] if tops else None
        bad = rule.order[0] if tops and rule.order[0] != good else None
    else:
        if not rule.allowed:
            return rule
        good = rule.allowed[0]
        bad = next(
            (v for v in _choices(contract, m, rule.attribute) if v not in rule.allowed), None
        )
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


def auth_problems(contract: TaskContract, seed_rows: bool = True) -> list[str]:
    """칸이 필요한데 없음 · 칸이 계약과 안 맞음 → 재요청 사유."""
    out: list[str] = []
    for e in contract.endpoints:
        rule = e.auth
        label = f"{e.method or ''} {e.path} auth".strip()
        if rule is None:
            if auth_required(e):
                out.append(
                    f"{label}: 권한 실패(401·403·권한 사유 코드)를 내는데 auth 칸이 없음 — 누가 통과하는지 auth 에 적는다 "
                    '(예: {"kind": "role", "header": "X-Actor", "denied_status": 403, "entity": "m1", "identifies": "id", '
                    '"attribute": "role", "allowed": ["ADMIN"]})'
                )
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
    """숨은 케이스의 대상 행이 등급 비교 권한에서 아무 요청자도 바꿀 수 없는 값 (above=target 인데 order 의 맨 위, or_equal 아님).
    2026-11-10 v19-be2: hc1 버스트 대상 240행을 MASTER 로 두어 어떤 요청자도 통과 못 했다 — 허용 구현이 모두 403 으로 hc1 을
    떨어지자 숨은 테스트를 '403 기대'로 다시 만들었고, 요청이 권한에서 끝나 기능 없는 시작 코드도 hc1 을 통과했다."""
    out: list[str] = []
    for e in contract.endpoints:
        r = e.auth
        if (
            r is None
            or r.kind != "role"
            or r.above != "target"
            or r.or_equal
            or not r.order
            or not r.attribute
        ):
            continue
        top = r.order[-1]
        for c in plan.cases:
            for d in c.data:
                if d.entity_id == r.entity and str(d.initial.get(r.attribute)) == top:
                    out.append(
                        f"숨은 케이스 {c.case_id} 의 {d.entity_id} 대상 행 {r.attribute}={top} 은 {e.method} {e.path} 의 권한 규칙"
                        f"(요청자 {r.attribute} 가 대상보다 높아야)에서 어떤 요청자도 바꿀 수 없다 — 요청이 권한에서 끝나 숨은 요구를 재지 못한다. "
                        f"대상 행은 {top} 보다 낮은 값({', '.join(r.order[:-1])} 중)으로 둔다"
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
