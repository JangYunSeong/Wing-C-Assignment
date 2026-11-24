"""재료 인터페이스 → 공개 계약 (#256 1단계, 데이터 재료).

재료(incidents/public_v1/interfaces/*.yaml, schemas.MaterialInterface)가 정한 명령·입출력 파일·엔티티를 코드가 계약에 채우고
(fit_interface — plan_contract.fit_output 의 첫 단계), 계약 검사가 그대로인지 본다(interface_problems). LLM 에는 계약 계획 입력으로
보여 준다(interface_text) — 공개 테스트·숨은 케이스가 같은 이름을 쓰게.

- 고정 부분이 LLM 답과 다르면 코드가 재료 것으로 덮어쓴다 — 이름·위치를 고쳐 달라고 재요청하지 않는다.
  2026-11-18 알파: 계약이 칸 이름(payment_time·transaction_id)·출력 위치({out_dir}/{run_date}/)를 빌드마다 새로 정해, 흔한 구현의
  모양이 공개 계약이 되고 허용 구현 셋이 숨은 케이스에서 떨어졌다.
- 고정 밖 명령·파일·엔티티(같은 알파의 setup_schedule.py·kind env 입력 환경)는 지우지 않고 재요청한다 — 그 요소에 기대던 공개
  테스트를 LLM 이 다시 짜야 한다.
- refs(공개 요구·수용 기준 id)는 명세마다 달라 LLM 답에서 가져온다: 짝지은 요소의 refs, 같은 이름 칸은 그 칸의 refs. 짝이 없거나
  쓸 수 있는 refs 가 없으면 공개 요구 전체.
- 짝짓기는 짐작하지 않는다 — 규칙마다 후보가 하나일 때만: 명령은 경로 → id → 남은 명령이 하나씩이면 그 둘, 파일은 경로 → 확장자 → id,
  엔티티는 이름 → id. 짝의 id 가 재료와 다르면 covers 와 숨은 케이스의 id 참조(faults.endpoint·data.entity_id)를 재료 id 로 바꾼다.

결정적이라 저장한 LLM 답으로 다시 돌릴 수 있다. 계약 검사 모듈(starter/contract.py)이 이 모듈을 쓰므로 여기서는 그쪽을 import 하지 않는다.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Sequence
from pathlib import PurePosixPath
from typing import TypeVar

from pydantic import BaseModel

from task_spec.schemas.build import (
    ContractField,
    ContractResponse,
    Endpoint,
    Entity,
    Fixture,
    HiddenPlan,
    PlanContractOutput,
    TaskContract,
)
from task_spec.schemas.incident import (
    InterfaceCommand,
    InterfaceEntity,
    InterfaceField,
    InterfaceFile,
    MaterialInterface,
)
from task_spec.schemas.template import TemplateManifest

# 승인 화면 표시 — 파일을 다루는 템플릿(데이터)인데 재료에 인터페이스가 없을 때 (회사 사례·인터뷰 재료)
FLAG_NO_INTERFACE = "재료 인터페이스 없음 — 명령·입출력 파일·칸 이름을 LLM 이 결정함"

F = TypeVar("F")
L = TypeVar("L")
M = TypeVar("M", bound=BaseModel)


def _norm(path: str) -> str:
    return path.strip().removeprefix("./")


def _suffix(path: str) -> str:
    return PurePosixPath(path.strip()).suffix.lower()


def _pair(
    fixed: Sequence[F],
    llm: Sequence[L],
    rules: Sequence[Callable[[F, L], bool]],
    lone: bool = False,
) -> dict[int, int]:
    """재료 요소 순번 → 짝인 LLM 요소 순번. 규칙을 차례로 쓰고, 한 규칙에서 후보가 여럿이면 그 규칙으로는 짝짓지 않는다.
    lone: 마지막에 짝 없는 것이 양쪽 하나씩이면 그 둘을 짝짓는다."""
    pairs: dict[int, int] = {}
    for rule in rules:
        for i, f in enumerate(fixed):
            if i in pairs:
                continue
            taken = set(pairs.values())
            cands = [j for j, x in enumerate(llm) if j not in taken and rule(f, x)]
            if len(cands) == 1:
                pairs[i] = cands[0]
    if lone:
        rest_f = [i for i in range(len(fixed)) if i not in pairs]
        rest_l = [j for j in range(len(llm)) if j not in set(pairs.values())]
        if len(rest_f) == 1 and len(rest_l) == 1:
            pairs[rest_f[0]] = rest_l[0]
    return pairs


def _refs(found: Sequence[str], allowed: Collection[str]) -> list[str]:
    return [r for r in dict.fromkeys(found) if r in allowed]


def _fields(
    fixed: Sequence[InterfaceField],
    llm: Sequence[ContractField],
    base: list[str],
    allowed: Collection[str],
) -> list[ContractField]:
    by_name = {f.name: f for f in llm}
    out: list[ContractField] = []
    for f in fixed:
        mate = by_name.get(f.name)
        refs = (_refs(mate.refs, allowed) if mate is not None else []) or base
        out.append(
            ContractField(
                name=f.name,
                type=f.type,
                required=f.required,
                constraints=f.constraints,
                refs=refs,
            )
        )
    return out


def _endpoint(
    c: InterfaceCommand, mate: Endpoint | None, allowed: Collection[str], default: list[str]
) -> Endpoint:
    llm_fields = [*mate.request, *mate.query] if mate is not None else []
    own = _refs(mate.refs, allowed) if mate is not None else []
    base = own or _refs([r for f in llm_fields for r in f.refs], allowed) or default
    return Endpoint(
        endpoint_id=c.endpoint_id,
        method=None,
        path=c.path,
        summary=c.summary,
        request=_fields(c.options, llm_fields, base, allowed),
        responses=[
            ContractResponse(status=r.status, description=r.description) for r in c.responses
        ],
        refs=base,
        existing=c.existing,
    )


def _fixture(
    f: InterfaceFile, mate: Fixture | None, allowed: Collection[str], default: list[str]
) -> Fixture:
    refs = (_refs(mate.refs, allowed) if mate is not None else []) or default
    return Fixture(
        fixture_id=f.fixture_id,
        kind="file",
        summary=f.summary,
        content=f.content,
        path=f.path,
        refs=refs,
        role=f.role,
        format=f.format,
        entity=f.entity,
    )


def _entity(
    m: InterfaceEntity, mate: Entity | None, allowed: Collection[str], default: list[str]
) -> Entity:
    llm_fields = list(mate.fields) if mate is not None else []
    base = _refs([r for f in llm_fields for r in f.refs], allowed) or default
    return Entity(
        entity_id=m.entity_id,
        name=m.name,
        fields=_fields(m.fields, llm_fields, base, allowed),
        existing=m.existing,
    )


def _field_sig(
    fs: Sequence[ContractField] | Sequence[InterfaceField],
) -> tuple[tuple[object, ...], ...]:
    return tuple((f.name, f.type, f.required, f.constraints) for f in fs)


def _changed_command(c: InterfaceCommand, e: Endpoint) -> list[str]:
    what = []
    if _norm(e.path) != _norm(c.path):
        what.append("경로")
    if _field_sig([*e.request, *e.query]) != _field_sig(c.options):
        what.append("옵션")
    if [r.status for r in e.responses] != [r.status for r in c.responses]:
        what.append("종료 코드")
    return what


def _changed_file(f: InterfaceFile, fx: Fixture) -> list[str]:
    what = []
    if fx.kind != "file":
        what.append("종류")
    if _norm(fx.path or "") != _norm(f.path):
        what.append("위치")
    return what


def _changed_entity(m: InterfaceEntity, e: Entity) -> list[str]:
    what = []
    if e.name != m.name:
        what.append("이름")
    if _field_sig(e.fields) != _field_sig(m.fields):
        what.append("칸")
    return what


def _remap_plan(plan: HiddenPlan, ids: dict[str, str]) -> HiddenPlan:
    """숨은 케이스의 id 참조(faults.endpoint·data.entity_id)를 바꾼다. 칸 이름 키(faults.when·data.initial)는 다시 잇지 않는다 —
    데이터 템플릿은 행 시드(data)를 쓰지 않고, faults 는 명세가 승인한 실패 지정이 없으면 fit_faults 가 뺀다. 행 시드·실패 지정을
    쓰는 템플릿으로 넓히면(#102) 그 키도 재료 칸 이름으로 이어야 한다."""
    if not ids:
        return plan
    cases = [
        hc.model_copy(
            update={
                "faults": [
                    f.model_copy(update={"endpoint": ids.get(f.endpoint, f.endpoint)})
                    for f in hc.faults
                ],
                "data": [
                    d.model_copy(update={"entity_id": ids.get(d.entity_id, d.entity_id)})
                    for d in hc.data
                ],
            }
        )
        for hc in plan.cases
    ]
    return plan.model_copy(update={"cases": cases})


def _extras(
    llm: Sequence[M],
    mates: Sequence[M],
    key: str,
    prefix: str,
    fixed: Collection[str],
    ids: dict[str, str] | None,
) -> list[M]:
    """짝 없는 LLM 요소 (고정 밖 — 계약 검사가 재요청). id 가 재료나 앞의 요소와 겹치면 비어 있는 번호로 바꾼다 — 겹친 채 두면 id 가 둘이
    되고 검사가 고정 밖 요소를 지목하지 못한다 (LLM 이 e1 을 스케줄 명령에, e2 를 재료 명령에 쓴 경우). ids 가 있으면 바꾼 id 를 적는다 —
    LLM 답의 그 id 참조는 이 요소를 가리킨 것이다. 짝지은 요소와 같은 LLM id(답 안의 중복 id)면 참조는 짝 쪽에 둔다."""
    mate_ids = {getattr(x, key) for x in mates}
    used = {id(x) for x in mates}
    taken = set(fixed) | {getattr(x, key) for x in llm}
    seen = set(fixed)
    out: list[M] = []
    for x in llm:
        if id(x) in used:
            continue
        old = getattr(x, key)
        if old in seen:
            n = 1
            while f"{prefix}{n}" in taken:
                n += 1
            new = f"{prefix}{n}"
            taken.add(new)
            if ids is not None and old not in mate_ids:
                ids[old] = new
            x = x.model_copy(update={key: new})
        seen.add(getattr(x, key))
        out.append(x)
    return out


def fit_interface(
    out: PlanContractOutput,
    interface: MaterialInterface | None,
    req_ids: Sequence[str],
    public_ac_ids: Sequence[str],
) -> tuple[PlanContractOutput, list[str]]:
    """LLM 답의 명령·입출력 파일·엔티티를 재료 인터페이스로 바꾼다 (고정 밖 요소는 그대로 두고 계약 검사가 재요청). 인터페이스가 없으면 그대로
    — 다만 파일의 역할·형식·행 엔티티(코드만 채우는 칸, LLM 스키마에 없음)를 LLM 이 냈으면 비운다 (#256 2단계 — 도우미가 생기지 않게)."""
    if interface is None:
        if any(fx.role or fx.format or fx.entity for fx in out.contract.fixtures):
            cleared = [
                fx.model_copy(update={"role": None, "format": None, "entity": None})
                for fx in out.contract.fixtures
            ]
            out = out.model_copy(
                update={"contract": out.contract.model_copy(update={"fixtures": cleared})}
            )
        return out, []
    c = out.contract
    allowed = set(req_ids) | set(public_ac_ids)
    default = list(req_ids) or list(public_ac_ids)

    cli = [e for e in c.endpoints if e.method is None]
    files = [fx for fx in c.fixtures if fx.kind == "file"]
    cmd_pairs = _pair(
        interface.commands,
        cli,
        [
            lambda f, x: _norm(x.path) == _norm(f.path),
            lambda f, x: x.endpoint_id == f.endpoint_id,
        ],
        lone=True,
    )
    file_pairs = _pair(
        interface.files,
        files,
        [
            lambda f, x: _norm(x.path or "") == _norm(f.path),
            lambda f, x: bool(x.path) and _suffix(x.path or "") == _suffix(f.path),
            lambda f, x: x.fixture_id == f.fixture_id,
        ],
    )
    ent_pairs = _pair(
        interface.entities,
        c.entities,
        [lambda f, x: x.name == f.name, lambda f, x: x.entity_id == f.entity_id],
    )

    def mate(pairs: dict[int, int], llm: Sequence[L], i: int) -> L | None:
        return llm[pairs[i]] if i in pairs else None

    endpoints = [
        _endpoint(cmd, mate(cmd_pairs, cli, i), allowed, default)
        for i, cmd in enumerate(interface.commands)
    ]
    fixtures = [
        _fixture(f, mate(file_pairs, files, i), allowed, default)
        for i, f in enumerate(interface.files)
    ]
    entities = [
        _entity(m, mate(ent_pairs, c.entities, i), allowed, default)
        for i, m in enumerate(interface.entities)
    ]
    ids: dict[str, str] = {}  # LLM id → 재료 id·새 id (covers·숨은 케이스 참조를 한꺼번에 바꾼다)
    mates_e = [cli[j] for j in cmd_pairs.values()]
    mates_f = [files[j] for j in file_pairs.values()]
    mates_m = [c.entities[j] for j in ent_pairs.values()]
    endpoints += _extras(
        c.endpoints, mates_e, "endpoint_id", "e", [x.endpoint_id for x in interface.commands], ids
    )
    fixtures += _extras(
        c.fixtures, mates_f, "fixture_id", "f", [x.fixture_id for x in interface.files], None
    )
    entities += _extras(
        c.entities, mates_m, "entity_id", "m", [x.entity_id for x in interface.entities], ids
    )

    changed: list[str] = []
    for i, cmd in enumerate(interface.commands):
        e = mate(cmd_pairs, cli, i)
        if e is None:
            changed.append(f"명령 {cmd.endpoint_id}(새로 넣음)")
            continue
        if e.endpoint_id != cmd.endpoint_id:
            ids[e.endpoint_id] = cmd.endpoint_id
        if what := _changed_command(cmd, e):
            changed.append(f"명령 {cmd.endpoint_id}({'·'.join(what)})")
    for i, f in enumerate(interface.files):
        fx = mate(file_pairs, files, i)
        if fx is None:
            changed.append(f"파일 {f.fixture_id}(새로 넣음)")
        elif what := _changed_file(f, fx):
            changed.append(f"파일 {f.fixture_id}({'·'.join(what)})")
    for i, m in enumerate(interface.entities):
        e = mate(ent_pairs, c.entities, i)
        if e is None:
            changed.append(f"엔티티 {m.entity_id}(새로 넣음)")
            continue
        if e.entity_id != m.entity_id:
            ids[e.entity_id] = m.entity_id
        if what := _changed_entity(m, e):
            changed.append(f"엔티티 {m.entity_id}({'·'.join(what)})")

    covers = {k: list(dict.fromkeys(ids.get(x, x) for x in v)) for k, v in c.covers.items()}
    contract = c.model_copy(
        update={
            "endpoints": endpoints,
            "fixtures": fixtures,
            "entities": entities,
            "covers": covers,
        }
    )
    plan = _remap_plan(out.hidden_plan, ids)
    notes = [f"재료 인터페이스로 변경 — {', '.join(changed)}"] if changed else []
    return out.model_copy(update={"contract": contract, "hidden_plan": plan}), notes


def _listing(interface: MaterialInterface) -> str:
    return (
        f"명령 {'·'.join(c.endpoint_id for c in interface.commands)} · "
        f"파일 {'·'.join(f.fixture_id for f in interface.files)} · "
        f"엔티티 {'·'.join(m.entity_id for m in interface.entities)}"
    )


def _first(items: Sequence[M], key: str) -> dict[str, M]:
    """id → 첫 요소 (고정 요소가 앞에 온다 — 겹친 id 는 계약 검사 _ids 가 따로 말한다)."""
    out: dict[str, M] = {}
    for x in items:
        out.setdefault(getattr(x, key), x)
    return out


def interface_problems(
    contract: TaskContract, interface: MaterialInterface | None, manifest: TemplateManifest
) -> tuple[list[str], list[str]]:
    """(재요청 사유, 승인자 표시). 인터페이스가 있으면 고정 부분이 재료와 같은지(코드가 맞춘 뒤라 다르면 뒤 단계 맞춤의 결함)와 고정 밖
    명령·파일·엔티티가 없는지. 없으면 파일을 다루는 템플릿에만 표시를 남긴다."""
    if interface is None:
        return [], ([FLAG_NO_INTERFACE] if "file" in manifest.interfaces else [])
    have_e = _first(contract.endpoints, "endpoint_id")
    have_f = _first(contract.fixtures, "fixture_id")
    have_m = _first(contract.entities, "entity_id")
    differ: list[str] = []
    for cmd in interface.commands:
        e = have_e.get(cmd.endpoint_id)
        if (
            e is None
            or e.method is not None
            or _changed_command(cmd, e)
            or e.summary != cmd.summary
            or [r.description for r in e.responses] != [r.description for r in cmd.responses]
        ):
            differ.append(f"명령 {cmd.endpoint_id}")
    for f in interface.files:
        fx = have_f.get(f.fixture_id)
        if fx is None or _changed_file(f, fx) or (fx.summary, fx.content) != (f.summary, f.content):
            differ.append(f"파일 {f.fixture_id}")
    for m in interface.entities:
        ent = have_m.get(m.entity_id)
        if ent is None or _changed_entity(m, ent):
            differ.append(f"엔티티 {m.entity_id}")
    problems: list[str] = []
    if differ:
        problems.append(
            f"재료 인터페이스와 다른 요소: {', '.join(differ)} — 재료가 정한 명령·입출력 파일·엔티티는 그대로 둔다"
        )
    want = (
        {c.endpoint_id for c in interface.commands}
        | {f.fixture_id for f in interface.files}
        | {m.entity_id for m in interface.entities}
    )
    extras = [
        *(
            f"명령 {e.endpoint_id} {e.path}"
            for e in contract.endpoints
            if e.endpoint_id not in want
        ),
        *(
            f"입력 환경 {fx.fixture_id}({fx.kind})"
            for fx in contract.fixtures
            if fx.fixture_id not in want
        ),
        *(f"엔티티 {m.entity_id} {m.name}" for m in contract.entities if m.entity_id not in want),
    ]
    if extras:
        problems.append(
            f"재료가 정한 공개 계약 밖의 요소: {', '.join(extras)} — 이 과제의 명령·입출력 파일·엔티티는 재료가 정한다 "
            f"({_listing(interface)}). 이 요소를 빼고, 그것으로 담던 요구는 재료가 정한 명령·파일로 담는다"
        )
    return problems, []


def _name_pattern(name: str) -> re.Pattern[str]:
    """이름 경계를 ASCII 로 — 한글 조사가 붙어도 잡는다 (`run_date로`). \\w 는 한글에도 맞아 조사 붙은 이름을 놓친다."""
    return re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(name)}(?![A-Za-z0-9_])")


def interface_ref(interface: MaterialInterface | None) -> str | None:
    """재료 인터페이스 내용 지문 (sha256) — 같은 프롬프트 판이라도 이 파일이 바뀌면 계약 입력이 달라진다. 평가 보고서에 남긴다."""
    if interface is None:
        return None
    return hashlib.sha256(interface.model_dump_json().encode("utf-8")).hexdigest()


def stale_name_problems(llm: PlanContractOutput, interface: MaterialInterface | None) -> list[str]:
    """LLM 이 자기 답에서 지은 이름(명령 옵션·엔티티·칸) 가운데 재료에 없는 것이 공개 테스트 checks·숨은 케이스 글에 남았으면 재요청.
    코드는 계약의 이름만 재료 것으로 바꾸므로, 그 이름으로 쓴 테스트 글은 계약과 어긋난다 — 저장된 데이터 알파 계약을 재생해 보니
    모든 계약의 테스트 글에 옛 이름(run_date·total_amount 등)이 남았다 (입력 블록 없이 만든 답). llm 은 맞추기 전 답."""
    if interface is None:
        return []
    keep = (
        {o.name for c in interface.commands for o in c.options}
        | {f.name for m in interface.entities for f in m.fields}
        | {m.name for m in interface.entities}
    )
    c = llm.contract
    mine = (
        {f.name for e in c.endpoints if e.method is None for f in [*e.request, *e.query]}
        | {f.name for m in c.entities for f in m.fields}
        | {m.name for m in c.entities}
    ) - keep
    texts = [pt.checks for pt in c.public_tests] + [
        t for hc in llm.hidden_plan.cases for t in (hc.scenario, hc.expect, *hc.setup)
    ]
    hits = sorted(n for n in mine if any(_name_pattern(n).search(t) for t in texts))
    if not hits:
        return []
    names = " · ".join(
        [
            *(f"옵션 {o.name}" for cmd in interface.commands for o in cmd.options),
            *(f"{m.name}({', '.join(f.name for f in m.fields)})" for m in interface.entities),
        ]
    )
    return [
        f"공개 테스트·숨은 케이스가 재료 인터페이스에 없는 이름을 씀: {', '.join(hits)} — 재료가 정한 이름으로 고쳐 쓴다 ({names})"
    ]


def without_fixed(contract: TaskContract, interface: MaterialInterface | None) -> TaskContract:
    """고정 부분을 뺀 계약 — 누설 검사(_leaks)용. 재료 글은 사람이 쓰고 재료 낱말로 검사했다(테스트). 명세마다 다른 개념어가 재료 글에
    우연히 있어도 LLM 은 고칠 수 없어 재요청이 끝나지 않는다."""
    if interface is None:
        return contract
    e = {c.endpoint_id for c in interface.commands}
    f = {x.fixture_id for x in interface.files}
    m = {x.entity_id for x in interface.entities}
    return contract.model_copy(
        update={
            "endpoints": [x for x in contract.endpoints if x.endpoint_id not in e],
            "fixtures": [x for x in contract.fixtures if x.fixture_id not in f],
            "entities": [x for x in contract.entities if x.entity_id not in m],
        }
    )


def _field_line(f: InterfaceField) -> str:
    extra = f" — {f.constraints}" if f.constraints else ""
    return f"{f.name}({f.type}{', 선택' if not f.required else ''}{extra})"


def interface_text(interface: MaterialInterface | None) -> str:
    """계약 계획 입력에 붙는 고정 계약 블록 (재료 요구 needs_text 와 같은 자리). 인터페이스가 없으면 빈 글 — 입력이 예전과 같다."""
    if interface is None:
        return ""
    lines = [
        "",
        "- 재료가 정한 공개 계약 — 명령·입출력 파일·엔티티는 아래 id·이름·형식 그대로 둔다 (코드가 이 내용으로 맞춘다). "
        "다른 명령·입출력 파일·엔티티를 더하지 않는다. 공개 테스트 checks 와 숨은 케이스(scenario·setup·expect)는 이 이름으로 쓴다.",
    ]
    for c in interface.commands:
        options = " · ".join(_field_line(o) for o in c.options)
        codes = " · ".join(f"{r.status} {r.description}" for r in c.responses)
        lines.append(
            f"  - 명령 {c.endpoint_id} `{c.path}` — {c.summary}. 옵션: {options}. 종료 코드: {codes}"
        )
    for f in interface.files:
        role = "입력" if f.role == "input" else "출력"
        lines.append(
            f"  - 파일 {f.fixture_id} ({role}) `{f.path}` — {f.summary}. {f.content}".rstrip()
        )
    for m in interface.entities:
        lines.append(
            f"  - 엔티티 {m.entity_id} {m.name} — " + " · ".join(_field_line(x) for x in m.fields)
        )
    if interface.public_rules:  # 공개 테스트 입력 범위 (#256 3단계) — 계약에 채우지 않고 입력에만
        lines.append(
            "- 재료가 정한 공개 테스트 입력 범위 — 공개 테스트 checks 는 아래를 지킨다 (숨은 케이스는 해당 없음)."
        )
        lines += [f"  - {r}" for r in interface.public_rules]
    return "\n".join(lines)
