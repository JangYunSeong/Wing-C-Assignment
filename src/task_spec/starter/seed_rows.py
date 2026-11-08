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

from task_spec.schemas import CaseRows, HiddenPlan, SeedGroup, SeedRule, SeedScalar, TaskContract
from task_spec.schemas.template import TemplateManifest

PUBLIC_PER_GROUP = 3  # 공개 테스트가 시드 그룹(처음 값)마다 쓸 행 수 — 숨은 범위·작은 id 밖
RESERVED_ROWS = 10  # 작은 id 쪽 — 숨은 케이스에 나눠 주지 않는다 (지원자 테스트·공개 테스트·시작 코드 테스트 몫)
_ID_FIELDS = {"id"}
_QUOTED = re.compile(r"""["'`]([^"'`]+)["'`]""")
_ENUM_TYPE = re.compile(r"^enum\(([^)]*)\)$", re.I)


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
    m = _ENUM_TYPE.match(type_.strip())
    if m:
        return {v.strip() for v in re.split(r"[|,]", m.group(1)) if v.strip()}
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
        if expected - 1 > s.rows:
            out.append(f"{label}: 범위가 rows={s.rows} 를 넘음 (id {expected - 1} 까지)")
        elif expected - 1 < s.rows:
            out.append(f"{label}: id {expected}~{s.rows} 이 어느 범위에도 없음")
    return out


def burst_rows_needed(plan: HiddenPlan, manifest: TemplateManifest) -> tuple[int | None, list[str]]:
    """버스트 케이스가 요청마다 다른 행을 쓰려면 필요한 행 수 (manifest burst_rows 조절값의 곱). 값이 없으면 None + 빠진 이름."""
    if not manifest.burst_rows:
        return None, []
    missing = [n for n in manifest.burst_rows if not isinstance(plan.harness.get(n), int)]
    if missing:
        return None, missing
    return math.prod(int(plan.harness[n]) for n in manifest.burst_rows), []  # type: ignore[arg-type]


def _matches(group_values: Mapping[str, SeedScalar], initial: Mapping[str, SeedScalar]) -> bool:
    return all(k in group_values and str(group_values[k]) == str(v) for k, v in initial.items())


def allocate_rows(
    contract: TaskContract, plan: HiddenPlan, reserved: int = RESERVED_ROWS
) -> RowAllocation:
    """케이스 순서대로, 처음 값이 맞는 그룹의 큰 id 부터 이어진 범위를 나눈다. id 1~reserved 는 나눠 주지 않는다."""
    seeds: dict[str, SeedRule] = {s.entity_id: s for s in contract.seed}
    names = {m.entity_id: m.name for m in contract.entities}
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
                ((i, g) for i, g in enumerate(seed.groups) if _matches(g.values, d.initial)),
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
    out: dict[tuple[str, tuple[tuple[str, SeedScalar], ...]], list[int]] = {}
    for s in contract.seed:
        if s.rows <= 0:
            continue
        for g in sorted(s.groups, key=lambda x: x.first):
            ids = out.setdefault((s.entity_id, tuple(g.values.items())), [])
            for i in range(max(g.first, reserved + 1), g.last + 1):
                if len(ids) >= PUBLIC_PER_GROUP:
                    break
                if i not in used:
                    ids.append(i)
    return [(eid, dict(vals), ids) for (eid, vals), ids in out.items()]


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
        if tail is not None and tail.last == seed.rows and dict(tail.values) == dict(d.initial):
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
            groups = [*seed.groups, SeedGroup(first=first, last=last, values=dict(d.initial))]
        grown = seed.model_copy(update={"rows": last, "groups": groups})
        notes.append(
            f"{names.get(d.entity_id, d.entity_id)} 시드 {seed.rows} → {last}행: id {first}~{last} ({_cond(d.initial)}) 덧붙임 "
            f"— 숨은 케이스 {cid} {d.rows}행을 작은 id {reserved}행 위에서 나누려고"
        )
        seeds[d.entity_id] = grown
        contract = contract.model_copy(update={"seed": [seeds[s.entity_id] for s in contract.seed]})
    if manifest.public_helper is not None:
        contract = _fit_public(contract, plan, seeds, names, reserved, notes)
    if notes:  # 고친 결과도 스키마(행 상한 등)를 지키는지
        contract = TaskContract.model_validate(contract.model_dump())
        plan = HiddenPlan.model_validate(plan.model_dump())
    return contract, plan, notes


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
        short = [
            (e, v, PUBLIC_PER_GROUP - len(ids))
            for e, v, ids in public_free(contract, plan, reserved)
        ]
        short = [x for x in short if x[2] > 0]
        if not short:
            break
        eid, values, deficit = short[0]
        seed = seeds[eid]
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
    """숨은 케이스의 data — 처음 값 필드, 버스트 행 수, 배정 가능 여부."""
    out: list[str] = []
    need, missing = burst_rows_needed(plan, manifest)
    for c in plan.cases:
        for d in c.data:
            out += _value_problems(
                f"숨은 케이스 {c.case_id} data", d.initial, contract, d.entity_id
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


def seed_markdown(s: SeedRule, name: str) -> str:
    """README·생성기 입력의 시드 한 줄 — 예: `Delivery` 200행 (id 1~200) — id 1~150: `status`=READY · …"""
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
    if s.rules:
        head += " — " + " · ".join(s.rules)
    if s.rows > RESERVED_ROWS:
        # 채점 테스트는 큰 id 범위를 나눠 쓴다 (allocate_rows) — 시작 코드·지원자 테스트가 그 행을 바꾸면 채점이 흔들린다
        head += f" — 테스트에서 바꿔도 되는 행은 id 1~{RESERVED_ROWS}"
    return head
