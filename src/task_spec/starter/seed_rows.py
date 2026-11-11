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
from task_spec.schemas.build import Endpoint, Entity
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.auth_rule import requester_rows
from task_spec.starter.field_types import parse as parse_field
from task_spec.starter.seed_sql import _insert_fields, default_value

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


def _lists_entity(e: Endpoint, m: Entity) -> bool:
    """성공 응답이 그 엔티티의 목록인가 — returns 가 `m1[]`·`이름[]` 이거나, 본문에 그 엔티티의 목록 칸(`{items: m1[]}`)이 있다.
    다른 것의 목록(멤버 한 행을 찾아 그 감사 기록을 돌려주는 API)은 아니다 — 경로 변수로 그 엔티티 한 행을 찾는다."""
    ok = next((r for r in e.responses if 200 <= r.status < 300), None)
    if ok is None:
        return False
    keys = {m.entity_id, m.name}
    if ok.returns and ok.returns.endswith("[]") and ok.returns[:-2] in keys:
        return True
    for f in ok.body:
        ft = parse_field(f.type)
        if ft.kind == "list" and ft.items[0].kind == "ref" and ft.items[0].name in keys:
            return True
    return False


def _lookup_key_problems(contract: TaskContract) -> list[str]:
    """경로 변수로 한 행을 찾는데 여러 행 그룹이 그 필드를 모두 같은 값으로 고정 — 조회가 여러 건이 된다.

    2026-11-09 알파(cap1): 시드 규칙 글은 "memberId 는 id 와 같다"인데 groups 가 254행 모두 memberId=1 로 고정해
    `findByGuildIdAndMemberId` 가 여러 건 오류 — 시작 코드·흔한 구현·허용 구현이 모두 같은 500 으로 떨어졌다.
    """
    entities = {m.entity_id: m for m in contract.entities}
    out: list[str] = []
    for e in contract.endpoints:
        params = _PATH_PARAM.findall(e.path)
        if any(p in _ID_FIELDS for p in params):
            continue  # id 로 찾으면 행마다 다르다
        for s in contract.seed:
            m = entities.get(s.entity_id)
            if m is None:
                continue
            if _lists_entity(e, m):
                # 그 엔티티의 목록 API — 같은 경로 변수 값(프로젝트의 게시글들)이 여러 행에 있는 것이 정상. 2026-11-11 N+1 알파:
                # GET /projects/{projectId}/board/posts(m1[]) 를 '한 행을 찾는 API' 로 짚어 계약을 다시 요청했다.
                # 길드 감사(GET …/members/{memberId}/audit — 감사 기록 목록)는 멤버 한 행을 찾으므로 계속 본다
                continue
            keys = params if set(params) <= {f.name for f in m.fields} else []
            for (
                g
            ) in s.groups:  # 필드가 아닌 경로 변수({memberId} → id 등)가 있으면 그게 행마다 다르다
                if keys and g.last > g.first and all(k in g.values for k in keys):
                    # 갈래 둘 — 2026-11-11 N+1 알파: 목록 API 응답을 한 건의 칸으로 적은 계약에 '값을 뺀다' 만 줘서 다시 요청해도
                    # 응답 모양을 고치지 않았다 (진짜 원인은 목록 표시가 없는 응답)
                    msg = (
                        f"시드 {s.entity_id} 범위 {g.first}~{g.last}: {e.method or ''} {e.path} 를 {', '.join(keys)} 로 한 행을 찾는 "
                        f"API 로 적었는데 {', '.join(keys)} 가 모든 행에서 같음 — 여러 건 조회 오류. "
                        + (
                            f'목록을 돌려주는 API 면 성공 응답을 returns "{s.entity_id}[]" ({m.name} 목록)으로 적는다. '
                            f"한 행을 찾는 API 면 {keys[-1]} 는 values 에서 뺀다"
                            if e.method == "GET"  # 변경 요청(PATCH 등)은 목록 API 가 아니다
                            else f"{keys[-1]} 는 values 에서 뺀다"
                        )
                        + " (코드가 행마다 다른 값을 넣는다)"
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
    out: dict[tuple[str, tuple[tuple[str, SeedScalar], ...]], list[int]] = {}
    for s in contract.seed:
        if s.rows <= 0:
            continue
        for g in sorted(s.groups, key=lambda x: x.first):
            if set(range(g.first, g.last + 1)) <= asked.get(s.entity_id, set()):
                continue  # 요청자 행만 있는 그룹
            ids = out.setdefault((s.entity_id, tuple(g.values.items())), [])
            for i in range(max(g.first, reserved + 1), g.last + 1):
                if len(ids) >= PUBLIC_PER_GROUP:
                    break
                if i not in used and i not in asked.get(s.entity_id, set()):
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
