"""상태 규칙 칸(Entity.state) — 허용 값·전이·거절 코드를 코드가 검사하고 문장·테스트 도우미 상수로 만든다 [2026-11-08].

2026-11-08 상류 평가: 계약 첫 답 9개 중 5개가 상태 전이를 빠뜨렸고("허용 상태 값 중 하나"), 자유 글(constraints)의 'A→B' 를
정규식으로 찾던 재요청은 2회에도 같은 답이 남은 실행이 있었다. 그래서 정해진 칸으로 받고, 나머지는 기계적으로:
- 상태 필드 constraints 문장은 칸에서 코드가 쓴다 → README·시작 코드·참조 구현이 같은 규칙을 본다.
- 테스트 도우미에 처음 값별 다음 상태 상수(NEXT_OK_·NEXT_BAD_) → 숨은·공개 테스트가 전이를 짐작하지 않는다.
- 화살표 글만 적은 답(plan_contract v9 이하)은 코드가 칸으로 옮긴다.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic import ValidationError

from task_spec.schemas import HiddenPlan, StateRule, TaskContract
from task_spec.schemas.build import Entity

ARROW = re.compile(r"([\w-]+)\s*(?:→|->)\s*([\w-]+)")
_CODE_4XX = re.compile(r"\b(4\d\d)\b")
_STATE_NAME = re.compile(r"status|state", re.I)
_REJECT_WORDS = re.compile(r"전이|transition|상태", re.I)


def sentence(rule: StateRule) -> str:
    """상태 필드 constraints 에 들어갈 문장 — 따옴표 값 목록은 시드 값 검사(seed_rows._enum_values)도 읽는다."""
    values = ", ".join(f'"{v}"' for v in rule.values)
    moves = ", ".join(f"{a}→{b}" for a, b in rule.pairs())
    return f"상태 값: {values}. 허용 전이: {moves} — 그 밖은 {rule.rejected_status}"


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def seed_values(contract: TaskContract, entity_id: str, field: str) -> list[str]:
    """시드 그룹이 그 필드에 주는 처음 값 (id 순)."""
    out: list[str] = []
    for s in contract.seed:
        if s.entity_id == entity_id:
            for g in sorted(s.groups, key=lambda g: g.first):
                if field in g.values:
                    out.append(str(g.values[field]))
    return _unique(out)


def _rejected(text: str, contract: TaskContract) -> int | None:
    """거절 코드 — 전이 글에 적힌 4xx, 없으면 상태·전이를 말하는 4xx 응답, 그것도 없으면 400·409 응답."""
    if m := _CODE_4XX.search(text):
        return int(m.group(1))
    codes = [
        r.status
        for e in contract.endpoints
        for r in e.responses
        if 400 <= r.status < 500 and _REJECT_WORDS.search(r.description)
    ]
    if codes:
        return codes[0]
    have = {r.status for e in contract.endpoints for r in e.responses}
    return next((c for c in (400, 409) if c in have), None)


def legacy_rule(contract: TaskContract, m: Entity) -> StateRule | None:
    """화살표 글로만 적은 전이를 칸으로 — 그 엔티티 필드의 constraints, 없으면 (상태 필드가 하나이고 시드 행이 있는 엔티티만 —
    요청으로 쌓이는 기록 엔티티는 아님) 상태를 바꾸는 요청 필드·응답 설명의 화살표."""
    field, text = None, ""
    for f in m.fields:
        if f.constraints and ARROW.search(f.constraints):
            field, text = f.name, f.constraints
            break
    if field is None:
        named = [f.name for f in m.fields if _STATE_NAME.search(f.name)]
        seeded = any(x.entity_id == m.entity_id and x.rows > 0 for x in contract.seed)
        if not seeded or len(named) != 1:
            return None
        field = named[0]
        texts = [f.constraints or "" for e in contract.endpoints for f in e.request]
        texts += [r.description for e in contract.endpoints for r in e.responses]
        text = " ".join(t for t in texts if ARROW.search(t))
        if not text:
            return None
    pairs = _unique([f"{a}→{b}" for a, b in ARROW.findall(text)])
    ends = [x for p in pairs for x in p.split("→")]
    code = _rejected(text, contract)
    if code is None:
        return None
    try:
        return StateRule(
            field=field,
            values=_unique([*seed_values(contract, m.entity_id, field), *ends]),
            transitions=pairs,
            rejected_status=code,
        )
    except ValidationError:
        return None


def _merged(orig: str | None, rule: StateRule) -> str:
    """칸에서 쓴 문장 — 원래 제약에 화살표가 없으면 뒤에 붙여 남긴다 (예: 1~32자). 두 번 불러도 같다."""
    s = sentence(rule)
    if orig and orig.startswith(s):
        return orig
    if not orig or ARROW.search(orig):
        return s
    return f"{s} ({orig})"


def fit_state(contract: TaskContract) -> tuple[TaskContract, list[str]]:
    """화살표 글만 있으면 칸으로 옮기고, 칸이 있는 엔티티의 상태 필드 constraints 를 칸에서 쓴다. 바꾼 것은 notes 로."""
    notes: list[str] = []
    entities: list[Entity] = []
    changed = False
    for m in contract.entities:
        rule = m.state
        if rule is None:
            rule = legacy_rule(contract, m)
            if rule is not None:
                notes.append(
                    f"{m.name}.{rule.field} 의 화살표 전이 글을 state 칸으로 옮김 "
                    f"(전이 {len(rule.transitions)}개, 거절 {rule.rejected_status})"
                )
        if rule is None or rule.field not in {f.name for f in m.fields}:
            entities.append(m)
            continue
        fields = [
            f.model_copy(update={"constraints": _merged(f.constraints, rule)})
            if f.name == rule.field
            else f
            for f in m.fields
        ]
        new = m.model_copy(update={"state": rule, "fields": fields})
        changed |= new != m
        entities.append(new)
    if not changed:
        return contract, notes
    return contract.model_copy(update={"entities": entities}), notes


def state_problems(contract: TaskContract, plan: HiddenPlan) -> list[str]:
    """칸이 계약·시드·숨은 케이스와 맞는지 — 필드 있음, 전이 양끝이 값 목록 안, 거절 코드가 응답에, 처음 값이 값 목록 안,
    숨은 케이스 처음 값에서 나갈 허용 전이가 있음 (롤백·버스트 케이스가 규칙을 통과하는 요청을 보낼 수 있게)."""
    responses = {r.status for e in contract.endpoints for r in e.responses}
    out: list[str] = []
    for m in contract.entities:
        rule = m.state
        if rule is None:
            continue
        label = f"{m.name}.state"
        names = [f.name for f in m.fields]
        if rule.field not in names:
            out.append(
                f"{label}: field {rule.field} 가 {m.name} 필드에 없음 (있는 필드: {', '.join(names)})"
            )
            continue
        values = set(rule.values)
        if len(values) != len(rule.values):
            out.append(f"{label}: values 에 같은 값이 둘 이상")
        outside = [f"{a}→{b}" for a, b in rule.pairs() if a not in values or b not in values]
        if outside:
            out.append(
                f"{label}: 전이 {', '.join(outside)} 의 값이 values 에 없음 — values 에 상태 값을 빠짐없이"
            )
        if rule.rejected_status not in responses:
            out.append(
                f"{label}: 거절 코드 {rule.rejected_status} 가 어느 API 응답에도 없음 — 상태를 바꾸는 API responses 에 그 코드를 둔다"
            )
        for v in seed_values(contract, m.entity_id, rule.field):
            if v not in values:
                out.append(f"{label}: 시드 처음 값 {rule.field}={v} 가 values 에 없음")
        starts = {a for a, _ in rule.pairs()}
        for c in plan.cases:
            for d in c.data:
                if d.entity_id != m.entity_id or rule.field not in d.initial:
                    continue
                v = str(d.initial[rule.field])
                if v not in values:
                    out.append(
                        f"숨은 케이스 {c.case_id}: 처음 값 {rule.field}={v} 가 {label} values 에 없음"
                    )
                elif v not in starts:
                    out.append(
                        f"숨은 케이스 {c.case_id}: 처음 값 {rule.field}={v} 에서 나갈 허용 전이가 없음 — 규칙을 통과하는 "
                        "요청을 보낼 수 없다 (전이를 더하거나 처음 값을 바꾼다)"
                    )
    return out


def next_states(contract: TaskContract) -> Iterator[tuple[str, str, str | None, str | None]]:
    """(엔티티 이름, 처음 값, 허용되는 다음 값, 거절되는 다음 값) — 시드에 있는 처음 값마다. 테스트 도우미 상수 재료."""
    for m in contract.entities:
        rule = m.state
        if rule is None:
            continue
        pairs = rule.pairs()
        for v in seed_values(contract, m.entity_id, rule.field):
            ok = next((b for a, b in pairs if a == v), None)
            bad = next((w for w in rule.values if w != v and (v, w) not in pairs), None)
            yield m.name, v, ok, bad
