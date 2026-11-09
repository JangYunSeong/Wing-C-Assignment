"""공개 계약 `TaskContract` · `HiddenPlan` 결정적 검사 (docs/design/spec-to-code.md §3.3) — LLM·네트워크 없음.

결과는 둘로 나눈다.
- `problems`: 계약·HiddenPlan 을 다시 만들면 고칠 수 있는 것 → `plan_contract` 재요청 사유 (문장 그대로 붙인다).
- `spec_gaps`: 승인된 명세에 없는 것 → 재요청으로 못 고친다 (명세는 이미 승인됐고 `plan_contract` 는 명세를 바꾸지 않음).
  빌드를 `failed` 로 두고 승인자가 명세 수정 요청(새 판)을 고른다.

검사
1. 양방향 대응 — 모든 r* 가 covers 에, 모든 p* 에 공개 테스트, 모든 h* 에 숨은 케이스. 거꾸로 계약의 모든 엔드포인트·엔티티가
   covers 에 연결되고, 모든 필드가 명세의 r*·p* 를 가리킨다 (요구에 없는 필드는 낱말 목록에 안 걸려도 구조로 해법을 흘린다).
2. 템플릿 이름 규칙·허용 인터페이스.
3. 숨은 요구·운영 조건 핵심어(재료 leak_terms·trigger_terms·stage2_cause_terms·먼저 말할 개념)가 계약에 없음 —
   기능형 서술 검사와 같은 낱말 기준(`spec.feature.find_terms`). 식별자는 낱말로 풀어서 본다 (connectionPoolSize → connection pool size).
4. HiddenPlan 운영 수치 = 명세에 보이는 수치 — 템플릿 `harness_params` 의 표기 패턴으로 1단계 자료 본문·추가 요구 구체 답에서
   값을 뽑아 대조한다. 자유 글의 숫자를 그대로 찾지 않는다 ("8" 은 아무 데나 나온다).
5. HiddenPlan 키는 템플릿 조절값 안, 케이스의 의도한 실패 사유는 템플릿 `failure_reasons` 코드 안.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from task_spec.schemas.build import (
    ContractField,
    Endpoint,
    HarnessValue,
    HiddenPlan,
    TaskContract,
)
from task_spec.schemas.domain import TaskItem
from task_spec.schemas.incident import FeatureSeed
from task_spec.schemas.template import HarnessParam, TemplateManifest
from task_spec.spec.feature import find_terms
from task_spec.starter.auth_rule import auth_flags, auth_problems
from task_spec.starter.header_values import header_problems
from task_spec.starter.seed_rows import case_rows_problems, seed_problems
from task_spec.starter.state_rule import legacy_rule, state_problems


@dataclass(frozen=True)
class SpecView:
    """승인된 기능형 명세에서 계약 검사가 보는 부분."""

    req_ids: tuple[str, ...]
    public_ac_ids: tuple[str, ...]
    hidden_acs: dict[str, str]  # h* → trap_id
    trap_ids: tuple[str, ...]
    # 지원자가 운영 수치를 발견하는 곳 — 1단계 자료 본문 + 추가 요구 구체 답 (상황 글에는 운영 조건을 쓰지 않는다)
    visible_texts: tuple[str, ...]
    concepts: tuple[
        str, ...
    ] = ()  # 먼저 말할 개념 (1·2단계 first_mention_of) — 계약에 나오면 안 됨
    # 지원자에게 처음부터 보이는 명세 글 — 제목·상황·공개 요구·공개 수용 기준 (여기 나온 API 는 계약에 있어야 함)
    public_texts: tuple[str, ...] = ()

    @classmethod
    def from_task(cls, task: TaskItem) -> SpecView:
        obs = [*task.observables, *(task.debug_stage.observables if task.debug_stage else [])]
        return cls(
            req_ids=tuple(r.req_id for r in task.requirements),
            public_ac_ids=tuple(a.ac_id for a in task.acceptance_public),
            hidden_acs={h.ac_id: h.trap_id for h in task.acceptance_hidden},
            trap_ids=tuple(t.trap_id for t in task.traps),
            visible_texts=(
                *(a.content for a in task.artifacts),
                *(c.answer for c in task.clarifications),
            ),
            concepts=tuple(dict.fromkeys(o.value for o in obs if o.kind == "first_mention_of")),
            public_texts=(
                task.title or "",
                task.scenario or "",
                *(r.text for r in task.requirements),
                *(a.text for a in task.acceptance_public),
            ),
        )


@dataclass(frozen=True)
class ContractCheck:
    problems: list[str] = field(default_factory=list)  # 재요청으로 고칠 수 있음
    spec_gaps: list[str] = field(default_factory=list)  # 명세 수정이 필요함 — 재요청하지 않음
    review_flags: list[str] = field(
        default_factory=list
    )  # 통과지만 승인자 확인 (운영 값 후보 여럿 등)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.spec_gaps


# ── 운영 수치 ──────────────────────────────────────────────


def _number(raw: str) -> float | None:
    s = re.sub(r"[\s,_]", "", raw)
    try:
        return float(s)
    except ValueError:
        return None


_NUMERIC = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def _cells(line: str) -> list[str]:
    """표 한 줄 → 칸. `|` 가 있으면 그것으로, 없으면 공백으로 나눈다."""
    if "|" in line:
        return [c.strip() for c in line.strip().strip("|").split("|")]
    return line.split()


def _column_values(text: str, columns: list[re.Pattern[str]]) -> list[str]:
    """머리 줄에 열 이름이 맞는 표에서 그 열의 숫자 값을 뽑는다 (예: 요청 지표 표의 inflight_requests 열)."""
    found: list[str] = []
    lines = text.splitlines()
    for i, head in enumerate(lines):
        cells = _cells(head)
        idx = next((j for j, c in enumerate(cells) if any(rx.fullmatch(c) for rx in columns)), None)
        if idx is None:
            continue
        for row in lines[i + 1 :]:
            if not row.strip():
                break
            vals = _cells(row)
            if all(set(v) <= set("-: ") for v in vals):  # 마크다운 구분 줄
                continue
            if len(vals) <= idx:
                break
            if _NUMERIC.match(vals[idx]):
                found.append(vals[idx])
    return found


def extract_values(param: HarnessParam, texts: Iterable[str]) -> list[str]:
    """표기 패턴(그룹 1)·표 열 이름으로 명세 글에서 이 조절값의 값을 뽑는다. 못 뽑으면 빈 목록 = "명세에 없음"."""
    texts = list(texts)
    found: list[str] = []
    for pattern in param.spec_patterns:
        rx = re.compile(pattern)
        for text in texts:
            found += [m.group(1).strip() for m in rx.finditer(text) if m.group(1)]
    if param.spec_columns:
        cols = [re.compile(c, re.IGNORECASE) for c in param.spec_columns]
        for text in texts:
            found += _column_values(text, cols)
    return list(dict.fromkeys(found))


def _same(value: HarnessValue, raw: str) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        n = _number(raw)
        return n is not None and n == float(value)
    if isinstance(value, str):
        return value.strip().casefold() == raw.strip().casefold()
    return False  # 목록 값은 명세 대조 대상이 아니다 (주입 대상 id 등)


def picked_values(param: HarnessParam, seen: Sequence[str]) -> list[str]:
    """표기 패턴으로 뽑은 값 중 정답 후보 (`pick`). unique 인데 서로 다른 값이 여럿이면 모두 돌려준다 (모호 — 승인자 확인)."""
    nums = [(raw, _number(raw)) for raw in seen]
    if param.pick != "unique" and nums and all(n is not None for _, n in nums):
        target = (max if param.pick == "max" else min)(n for _, n in nums)  # type: ignore[type-var]
        return [next(raw for raw, n in nums if n == target)]
    distinct: dict[object, str] = {}
    for raw, n in nums:
        distinct.setdefault(n if n is not None else raw.strip().casefold(), raw)
    return list(distinct.values())


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 근거 인용 안에서 값 후보가 아닌 숫자 — 백분율(38%)·시각(19:00)
_NOT_VALUE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*%|\d{1,2}:\d{2}")
NO_EVIDENCE = "없음"
MIN_QUOTE = 6
MAX_QUOTE = 160  # 분포·표 전체를 통째로 인용하면 그 안의 아무 숫자나 근거가 된다


def _norm(text: str) -> str:
    return " ".join(text.split())


def evidence_ok(value: HarnessValue, quote: str, texts: Iterable[str]) -> bool:
    """근거 인용 확인 — 인용이 자료(1단계 자료·구체 답)에 그대로 있고(공백 무시) 값이 인용 안에 있다.
    정규식 표기 패턴 대신 쓰는 길 (템플릿마다 표현을 맞추지 않아도 됨)."""
    return evidence_check(value, quote, texts) != "bad"


def evidence_check(
    value: HarnessValue, quote: str, texts: Iterable[str]
) -> Literal["ok", "ambiguous", "bad"]:
    """`evidence_ok` 의 자세한 판정. 백분율·시각은 값 후보에서 빼고, 남은 후보가 여럿이면 ambiguous
    ("1개: 38% 2개: 41% … 4개 이상: 7%" 를 인용하면 1·2·3·4 아무것이나 근거가 된다 — 알파 테스트 2026-11-07)."""
    q = _norm(quote).strip(" \"'“”‘’…")
    if not MIN_QUOTE <= len(q) <= MAX_QUOTE or not any(q in _norm(t) for t in texts):
        return "bad"
    numbers = {_number(n) for n in _NUM.findall(_NOT_VALUE.sub(" ", q))}
    if isinstance(value, list):
        return "ok" if all(float(v) in numbers for v in value) else "bad"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value) not in numbers:
            return "bad"
        return "ok" if len(numbers) == 1 else "ambiguous"
    return "ok" if str(value).lower() in q.lower() else "bad"


def spec_missing_params(visible_texts: Sequence[str], manifest: TemplateManifest) -> list[str]:
    """명세 글에서 표기 패턴으로 값이 안 뽑히는 필수·visible 조절값 이름.

    명세 단계 사전 검사용 (§3.3 [제안] — 기능형 서술 검증에서 템플릿이 정해지면 재요청 사유로 쓴다).
    """
    return [
        p.name
        for p in manifest.harness_params
        if p.visible and p.required and not extract_values(p, visible_texts)
    ]


def _type_problem(param: HarnessParam, value: HarnessValue) -> str | None:
    ok = {
        "int": isinstance(value, int) and not isinstance(value, bool),
        "float": isinstance(value, int | float) and not isinstance(value, bool),
        "str": isinstance(value, str),
        "int_list": isinstance(value, list) and all(isinstance(x, int) for x in value),
    }[param.kind]
    if not ok:
        return f"HiddenPlan {param.name} 의 값 형식이 {param.kind} 가 아님"
    nums = value if isinstance(value, list) else [value]
    for n in nums:
        if isinstance(n, int | float) and not isinstance(n, bool):
            if param.minimum is not None and n < param.minimum:
                return f"HiddenPlan {param.name}={n} 이 템플릿 하한 {param.minimum:g} 보다 작음"
            if param.maximum is not None and n > param.maximum:
                return f"HiddenPlan {param.name}={n} 이 템플릿 상한 {param.maximum:g} 보다 큼"
    return None


def _harness(
    plan: HiddenPlan, spec: SpecView, manifest: TemplateManifest
) -> tuple[list[str], list[str], list[str]]:
    problems: list[str] = []
    gaps: list[str] = []
    flags: list[str] = []  # 통과는 시키되 승인자가 볼 것
    known = {p.name for p in manifest.harness_params}
    unknown = [k for k in plan.harness if k not in known]
    if unknown:
        problems.append(
            f"HiddenPlan harness 에 템플릿에 없는 조절값: {', '.join(unknown)} "
            f"(쓸 수 있는 것: {', '.join(sorted(known)) or '없음'})"
        )
    for p in manifest.harness_params:
        if p.name not in plan.harness:
            if p.required:
                problems.append(f"HiddenPlan harness 에 필수 조절값 {p.name} 이 없음")
            elif p.visible and (seen := extract_values(p, spec.visible_texts)):
                # 지원자가 자료에서 본 운영 값은 채점에도 그대로 — 빼면 하네스 기본값이 대신 들어가 실패 사유가 달라진다
                problems.append(
                    f"명세 자료에 운영 값 {p.name}({', '.join(seen)})이 있으니 HiddenPlan harness 에 넣는다"
                )
            continue
        value = plan.harness[p.name]
        bad = _type_problem(p, value)
        if bad:
            problems.append(bad)
            continue
        if not p.visible:
            continue
        seen = extract_values(p, spec.visible_texts)
        quote = plan.evidence.get(p.name)
        cited = (
            evidence_check(value, quote, spec.visible_texts)
            if quote and quote.strip() != NO_EVIDENCE
            else "bad"
        )
        if seen:
            # 템플릿 표기 패턴이 값을 뽑았으면 그 값(pick)이 정답 — 패턴과 다른 값은 근거가 하나로 분명할 때만 받는다
            allowed = picked_values(p, seen)
            if any(_same(value, raw) for raw in allowed):
                if len(allowed) > 1:
                    flags.append(
                        f"운영 값 {p.name} 후보가 자료에 여럿({', '.join(allowed)}) — {value} 을 썼음, 승인자 확인"
                    )
                continue
            if cited == "ok":
                flags.append(
                    f"운영 값 {p.name}={value} 이 템플릿 표기 패턴의 값({', '.join(allowed)})과 다름 — 근거 인용 {quote!r} 로 받음, 승인자 확인"
                )
                continue
            problems.append(
                f"HiddenPlan {p.name}={value} 이 명세 자료의 값({', '.join(allowed)})과 다름 — 명세 자료의 값을 쓰고 evidence 에 그 값이 적힌 부분만 인용"
            )
            continue
        if cited == "ok":
            continue  # 근거 인용으로 확인
        if cited == "ambiguous":
            flags.append(
                f"운영 값 {p.name}={value} 의 근거 인용에 숫자가 여럿 — {quote!r}, 승인자 확인"
            )
            continue
        if quote and quote.strip() == NO_EVIDENCE:
            if p.required:
                gaps.append(
                    f"명세 1단계 자료·추가 요구 구체 답에 운영 수치 {p.name} 이 없음 — "
                    "지원자가 발견할 수 없는 조건으로 채점하게 되므로 명세 수정 필요"
                )
            else:
                problems.append(
                    f"HiddenPlan {p.name} 은 자료에 없다고 했으니 harness 에서 뺀다 (선택 조절값)"
                )
        elif quote:
            problems.append(
                f"HiddenPlan {p.name}={value} 의 근거 {quote[:120]!r} 가 자료에 그대로 없거나 값이 그 안에 없음 — "
                f"자료·답의 문장을 그대로 인용하고 그 안의 값을 쓴다 ({MAX_QUOTE}자 이내, 값이 적힌 부분만)"
            )
        else:
            problems.append(
                f"공개 조절값 {p.name}={value} 의 근거가 없음 — evidence 에 그 값이 적힌 자료·답 문장을 그대로 적는다 "
                f'(자료에 없으면 "{NO_EVIDENCE}")'
            )
    return problems, gaps, flags


# ── 계약 ───────────────────────────────────────────────────

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _words(identifier: str) -> str:
    """connectionPoolSize · max_pool · /orders/{id}/retry-key → 낱말로 풀어 쓴 글."""
    return re.sub(r"[_\-/{}.]+", " ", _CAMEL.sub(" ", identifier)).strip().lower()


def _fields(contract: TaskContract) -> list[tuple[str, ContractField]]:
    """(위치, 필드) — 요청·응답 본문·엔티티의 모든 필드."""
    out: list[tuple[str, ContractField]] = []
    for e in contract.endpoints:
        out += [(f"{e.endpoint_id} 헤더", f) for f in e.headers]
        out += [(f"{e.endpoint_id} 쿼리", f) for f in e.query]
        out += [(f"{e.endpoint_id} 요청", f) for f in e.request]
        for r in e.responses:
            out += [(f"{e.endpoint_id} 응답 {r.status}", f) for f in r.body]
    for m in contract.entities:
        out += [(f"{m.entity_id} {m.name}", f) for f in m.fields]
    return out


def contract_fields(contract: TaskContract) -> list[tuple[str, ContractField]]:
    """(위치, 필드) — 정적 검사에서도 쓰는 공개 이름."""
    return _fields(contract)


def contract_text(contract: TaskContract) -> str:
    """계약에서 지원자에게 보이는 글 전부 + 식별자를 낱말로 푼 것 (숨은 요구 핵심어 검사용)."""
    parts: list[str] = []
    for e in contract.endpoints:
        parts += [e.path, _words(e.path), e.summary]
        parts += [r.description for r in e.responses]
    for m in contract.entities:
        parts += [m.name, _words(m.name), *m.relations]
    for _, f in _fields(contract):
        parts += [f.name, _words(f.name), f.type, f.constraints or ""]
    for fx in contract.fixtures:
        parts += [fx.summary, fx.content]
    for sc in contract.screens:
        parts += [sc.route, sc.summary]
        parts += [
            f"{el.test_id} {_words(el.test_id)} {el.description} {el.action or ''}"
            for el in sc.elements
        ]
        parts += [f"{k.name} {k.shape}" for k in sc.storage]
    for s in contract.seed:
        parts += s.rules
        parts += [f"{k} {v}" for g in s.groups for k, v in g.values.items()]
    parts += [t.checks for t in contract.public_tests]
    return "\n".join(p for p in parts if p)


def element_refs(contract: TaskContract) -> dict[str, list[str]]:
    """계약 요소 id → 그 요소가 스스로 적은 refs (요소·필드·화면 요소), 처음 나온 순서."""
    out: dict[str, list[str]] = {}

    def add(eid: str, refs: Iterable[str]) -> None:
        got = out.setdefault(eid, [])
        got += [r for r in refs if r not in got]

    for e in contract.endpoints:
        add(e.endpoint_id, e.refs)
        for f in [*e.headers, *e.query, *e.request, *(f for r in e.responses for f in r.body)]:
            add(e.endpoint_id, f.refs)
    for m in contract.entities:
        add(m.entity_id, [r for f in m.fields for r in f.refs])
    for sc in contract.screens:
        add(sc.screen_id, [*sc.refs, *(r for el in sc.elements for r in el.refs)])
        add(sc.screen_id, [r for k in sc.storage for r in k.refs])
    return out


def fit_covers(contract: TaskContract, spec: SpecView) -> tuple[TaskContract, list[str]]:
    """covers 에 빠진 계약 요소를 그 요소가 스스로 적은 refs 의 공개 요구에 잇는다 — 역색인은 코드가 (기계적 출력).

    2026-11-08 상류 평가: 200 응답 모양으로 만든 엔티티(필드 refs r1·p1)를 covers 에 잇지도 빼지도 않아 재요청 2회 모두 실패.
    refs 는 공개 id 만 허용되므로 잇는다고 숨은 요구가 계약에 들어오지는 않는다. refs 가 없는 요소는 그대로 문제로 남긴다."""
    covered = {i for ids in contract.covers.values() for i in ids}
    covers = {k: list(v) for k, v in contract.covers.items()}
    notes: list[str] = []
    for eid, refs in element_refs(contract).items():
        if eid in covered:
            continue
        keys = [r for r in refs if r in spec.req_ids] or [
            r for r in refs if r in spec.public_ac_ids
        ]
        if not keys:
            continue
        for k in keys:
            covers.setdefault(k, []).append(eid)
        notes.append(
            f"계약 요소 {eid} 를 covers {', '.join(keys)} 에 이음 (그 요소가 적은 refs 대로)"
        )
    if not notes:
        return contract, []
    return contract.model_copy(update={"covers": covers}), notes


def _dups(label: str, ids: Sequence[str]) -> list[str]:
    dup = sorted(i for i, n in Counter(ids).items() if n > 1)
    return [f"{label} 이 겹침: {', '.join(dup)}"] if dup else []


def _ids(contract: TaskContract, plan: HiddenPlan) -> list[str]:
    problems = _dups("엔드포인트 id", [e.endpoint_id for e in contract.endpoints])
    problems += _dups("엔티티 id", [m.entity_id for m in contract.entities])
    problems += _dups("엔티티 이름", [m.name for m in contract.entities])
    problems += _dups("공개 테스트 id", [t.test_id for t in contract.public_tests])
    problems += _dups("입력 환경 id", [f.fixture_id for f in contract.fixtures])
    problems += _dups("화면 id", [sc.screen_id for sc in contract.screens])
    for sc in (
        contract.screens
    ):  # 같은 부품이 여러 화면에 있는 것은 정상 — 화면 안에서만 유일 (2026-11-07 종이 단계)
        problems += _dups(f"화면 {sc.screen_id} 의 test id", [el.test_id for el in sc.elements])
    problems += _dups("숨은 케이스 id", [c.case_id for c in plan.cases])
    problems += _dups(
        "엔드포인트(메서드·경로)", [f"{e.method or ''} {e.path}" for e in contract.endpoints]
    )
    return problems


def _coverage(contract: TaskContract, plan: HiddenPlan, spec: SpecView) -> list[str]:
    problems: list[str] = []
    refs_ok = set(spec.req_ids) | set(spec.public_ac_ids)
    element_ids = (
        {e.endpoint_id for e in contract.endpoints}
        | {m.entity_id for m in contract.entities}
        | {sc.screen_id for sc in contract.screens}
    )

    # 계약 요소 → 명세 (거꾸로 대응)
    for e in contract.endpoints:
        bad = [r for r in e.refs if r not in refs_ok]
        if bad:
            problems.append(
                f"엔드포인트 {e.endpoint_id} 의 refs 에 명세에 없는 id: {', '.join(bad)}"
            )
    for sc in contract.screens:
        for where, refs in (
            [(f"화면 {sc.screen_id}", sc.refs)]
            + [(f"화면 {sc.screen_id} 요소 {el.test_id}", el.refs) for el in sc.elements]
            + [(f"화면 {sc.screen_id} 저장소 {k.name}", k.refs) for k in sc.storage]
        ):
            bad = [r for r in refs if r not in refs_ok]
            if bad:
                problems.append(f"{where} 의 refs 에 명세에 없는 id: {', '.join(bad)}")
    for fx in contract.fixtures:
        bad = [r for r in fx.refs if r not in refs_ok]
        if bad:
            problems.append(
                f"입력 환경 {fx.fixture_id} 의 refs 에 명세에 없는 id: {', '.join(bad)}"
            )
    for where, f in _fields(contract):
        bad = [r for r in f.refs if r not in refs_ok]
        if bad:
            problems.append(f"{where} 필드 {f.name} 의 refs 에 명세에 없는 id: {', '.join(bad)}")
    covered = {i for ids in contract.covers.values() for i in ids}
    loose = sorted(element_ids - covered)
    if loose:
        problems.append(
            f"어느 공개 요구(covers)에도 연결되지 않은 계약 요소: {', '.join(loose)} — "
            "관련된 공개 요구가 있으면 그 요구의 covers 에 잇는다 (기존 API 가 다루는 대상 엔티티는 그 API 를 쓰는 요구에). "
            "어느 요구와도 관련 없을 때만 뺀다 — 요구에 없는 인터페이스·엔티티는 해법을 흘린다"
        )

    # 명세 → 계약. covers 값에는 입력 환경 id(f*)도 된다 (요구를 입력 환경에 잇는 것은 자연스럽다 — 덮였는지 따지지는 않음).
    # 공개 수용 기준 id(p*)를 키로 쓴 것은 무시한다 (2026-11-07 종이 단계: 재요청해도 되풀이, 해는 없음 — p* 는 공개 테스트로 대응)
    targets = element_ids | {fx.fixture_id for fx in contract.fixtures}
    for key, ids in contract.covers.items():
        if key not in spec.req_ids and key not in spec.public_ac_ids:
            problems.append(f"covers 의 키 {key} 가 명세의 공개 요구 id 가 아님")
        bad = [i for i in ids if i not in targets]
        if bad:
            problems.append(f"covers[{key}] 에 계약에 없는 id: {', '.join(bad)}")
    for r in spec.req_ids:
        if not contract.covers.get(r):
            problems.append(f"공개 요구 {r} 가 계약 covers 에 없음")

    tested = {t.ac_id for t in contract.public_tests}
    for t in contract.public_tests:
        if t.ac_id not in spec.public_ac_ids:
            problems.append(
                f"공개 테스트 {t.test_id} 의 ac_id {t.ac_id} 가 명세의 공개 수용 기준이 아님"
            )
    for p in spec.public_ac_ids:
        if p not in tested:
            problems.append(f"공개 수용 기준 {p} 에 공개 테스트가 없음")

    planned = {c.ac_id for c in plan.cases}
    for c in plan.cases:
        if c.ac_id not in spec.hidden_acs:
            problems.append(
                f"숨은 케이스 {c.case_id} 의 ac_id {c.ac_id} 가 명세의 숨김 수용 기준이 아님"
            )
        elif spec.hidden_acs[c.ac_id] != c.trap_id:
            problems.append(
                f"숨은 케이스 {c.case_id} 의 trap_id {c.trap_id} 가 {c.ac_id} 의 숨은 요구"
                f"({spec.hidden_acs[c.ac_id]})와 다름"
            )
        if c.trap_id not in spec.trap_ids:
            problems.append(
                f"숨은 케이스 {c.case_id} 의 trap_id {c.trap_id} 가 명세의 숨은 요구가 아님"
            )
    for h in spec.hidden_acs:
        if h not in planned:
            problems.append(f"숨김 수용 기준 {h} 에 숨은 케이스가 없음")
    return problems


def _naming(contract: TaskContract, manifest: TemplateManifest) -> list[str]:
    problems: list[str] = []
    if contract.interface_kind not in manifest.interfaces:
        problems.append(
            f"인터페이스 {contract.interface_kind} 를 템플릿 {manifest.ref} 이 지원하지 않음 "
            f"(지원: {', '.join(manifest.interfaces)})"
        )
    rx = manifest.naming
    for e in contract.endpoints:
        if contract.interface_kind in ("http", "ui"):  # ui 의 엔드포인트는 화면이 부르는 서버 API
            if e.method is None:
                problems.append(f"엔드포인트 {e.endpoint_id} 에 HTTP 메서드가 없음")
            if not re.fullmatch(rx.path, e.path):
                problems.append(
                    f"엔드포인트 {e.endpoint_id} 경로 {e.path!r} 가 템플릿 경로 규칙과 다름"
                )
        elif e.method is not None:
            problems.append(
                f"엔드포인트 {e.endpoint_id} 는 {contract.interface_kind} 라 HTTP 메서드를 쓰지 않음"
            )
    for sc in contract.screens:
        if not re.fullmatch(rx.path, sc.route):
            problems.append(f"화면 {sc.screen_id} 경로 {sc.route!r} 가 템플릿 경로 규칙과 다름")
        bad_ids = [el.test_id for el in sc.elements if not re.fullmatch(rx.test_id, el.test_id)]
        if bad_ids:
            problems.append(
                f"화면 {sc.screen_id} 의 test id 가 템플릿 규칙과 다름: {', '.join(bad_ids)}"
            )
    for m in contract.entities:
        if not re.fullmatch(rx.entity, m.name):
            problems.append(f"엔티티 이름 {m.name!r} 가 템플릿 이름 규칙과 다름")
    headers = {id(f) for e in contract.endpoints for f in e.headers}
    bad_fields = sorted(
        {
            f.name
            for _, f in _fields(contract)
            if id(f) not in headers and not re.fullmatch(rx.field, f.name)
        }
    )
    if bad_fields:
        problems.append(f"필드 이름이 템플릿 이름 규칙과 다름: {', '.join(bad_fields)}")
    bad_headers = sorted(
        {
            f.name
            for e in contract.endpoints
            for f in e.headers
            if not re.fullmatch(rx.header, f.name)
        }
    )
    if bad_headers:
        problems.append(f"헤더 이름이 템플릿 규칙과 다름: {', '.join(bad_headers)}")
    return problems


def hidden_terms(seed: FeatureSeed | None, spec: SpecView) -> list[str]:
    """계약에 나오면 안 되는 말 — 재료의 숨은 요구·원인·운영 조건 낱말 + 먼저 말할 개념. 공개된 업무 요구에 나온 말은 뺀다."""
    terms = list(spec.concepts)
    if seed is not None:
        terms += [*seed.leak_terms, *seed.stage2_cause_terms, *seed.trigger_terms]
        public = set(find_terms(terms, seed.business_need))
        terms = [t for t in terms if t not in public]
    return list(dict.fromkeys(terms))


def _leaks(contract: TaskContract, spec: SpecView, seed: FeatureSeed | None) -> list[str]:
    shown = find_terms(hidden_terms(seed, spec), contract_text(contract))
    if not shown:
        return []
    return [
        "숨은 요구·운영 조건을 드러내는 말이 공개 계약(경로·필드·설명·시드·공개 테스트)에 나옴: "
        + ", ".join(shown)
        + " — 계약은 '무엇을'만, 숨은 요구와 운영 조건은 담지 않는다"
    ]


def _reasons(
    plan: HiddenPlan, manifest: TemplateManifest, seed: FeatureSeed | None = None
) -> list[str]:
    codes = set(manifest.reason_codes)
    if (
        seed is not None and seed.reasons
    ):  # 재료가 선언한 이 숨은 요구의 사유만 (FeatureSeed.reasons)
        codes &= set(seed.reasons)
    bad = sorted({c.intended_reason for c in plan.cases if c.intended_reason not in codes})
    if not bad:
        return []
    return [
        f"숨은 케이스의 의도한 실패 사유가 템플릿 코드 목록(재료가 정했으면 그 숨은 요구의 사유)에 없음: {', '.join(bad)} "
        f"(쓸 수 있는 것: {', '.join(sorted(codes)) or '없음'})"
    ]


_API = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/[A-Za-z0-9_\-/{}.]*)")


def _api_key(method: str, path: str) -> str:
    return f"{method.upper()} {re.sub(r'{[^}]*}', '{}', path.rstrip('/') or '/')}"


def mentioned_apis(texts: Iterable[str]) -> list[str]:
    """글에 나온 'METHOD /path' (경로 변수는 {} 로) — 예: 'PATCH /deliveries/{}/status'."""
    return list(dict.fromkeys(_api_key(m, p) for t in texts for m, p in _API.findall(t)))


def existing_endpoints(contract: TaskContract, spec: SpecView) -> list[Endpoint]:
    """이미 있는 API (시작 코드가 동작하게 만든다) = 계약이 `existing` 으로 표시한 것 + 명세 공개 글에 `METHOD /path` 로 나온 계약 API.
    시작 코드 생성 입력(generate.existing_apis)과 시작 코드 정적 검사(static_check._starter_new_api_tests)가 같은 판정을 쓴다."""
    named = set(mentioned_apis(spec.public_texts))
    out: list[Endpoint] = []
    for e in contract.endpoints:
        key = mentioned_apis([f"{e.method} {e.path}"]) if e.method else []
        if e.existing or (key and key[0] in named):
            out.append(e)
    return out


def _contract_api_rx(contract: TaskContract) -> list[tuple[str, re.Pattern[str]]]:
    """계약 API → (메서드, 경로 정규식). 경로 변수 자리는 아무 한 마디 — 글에 나온 `/deliveries/15/status` 도 맞는다."""
    out: list[tuple[str, re.Pattern[str]]] = []
    for e in contract.endpoints:
        if e.method:
            path = re.escape(e.path.rstrip("/") or "/")
            out.append(
                (
                    e.method.upper(),
                    re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", path) + "/?$"),
                )
            )
    return out


def _in_contract(api: str, rx: list[tuple[str, re.Pattern[str]]]) -> bool:
    method, _, path = api.partition(" ")
    return any(method == m and pat.match(path) for m, pat in rx)


def _api_refs(contract: TaskContract, plan: HiddenPlan, spec: SpecView) -> list[str]:
    """명세·공개 테스트·숨은 케이스에 나온 API 는 계약에 있어야 한다 — 시작 코드·숨은 테스트가 그 계약으로 요청한다.
    글의 구체 id(`/deliveries/15/status`)는 계약의 경로 변수 자리와 맞는다 (2026-11-07 v3 스모크에서 오탐)."""
    rx = _contract_api_rx(contract)
    problems: list[str] = []
    missing = [a for a in mentioned_apis(spec.public_texts) if not _in_contract(a, rx)]
    if missing:
        problems.append(
            f"명세(상황·요구·수용 기준)에 나온 API 가 계약에 없음: {', '.join(missing)} — "
            "기존 API 도 요청 헤더·본문·응답·오류까지 계약에 넣는다"
        )
    used = mentioned_apis(
        [
            *(t.checks for t in contract.public_tests),
            *(f"{c.scenario} {c.expect}" for c in plan.cases),
        ]
    )
    stray = [a for a in used if not _in_contract(a, rx)]
    if stray:
        problems.append(f"공개 테스트·숨은 케이스가 계약에 없는 API 를 씀: {', '.join(stray)}")
    entity_ids = {m.entity_id for m in contract.entities}
    bad = sorted(
        {
            f"{e.endpoint_id} {r.status} → {r.returns}"
            for e in contract.endpoints
            for r in e.responses
            if r.returns and r.returns.removesuffix("[]") not in entity_ids
        }
    )
    if bad:
        problems.append(f"응답 returns 가 계약에 없는 엔티티를 가리킴: {', '.join(bad)}")
    return problems


def _no_seed_rows(
    contract: TaskContract, plan: HiddenPlan, manifest: TemplateManifest
) -> list[str]:
    """DB 행을 바꾸지 않는 템플릿(cli·file·ui) — 시드·행 배정 대신 입력 환경(fixtures)과 숨은 케이스 setup.
    2026-11-07 종이 단계: 데이터·ML 과제가 결과 파일·평가 기록을 시드 행으로 억지로 적고 코드가 행 수를 부풀렸다."""
    out: list[str] = []
    if contract.seed:
        out.append(
            f"템플릿 {manifest.ref} 은 DB 행 시드를 쓰지 않는다 — seed 를 비우고, 시작 코드가 깔아 줄 입력 환경은 fixtures 에 적는다"
        )
    with_data = [c.case_id for c in plan.cases if c.data]
    if with_data:
        out.append(
            f"숨은 케이스 {', '.join(with_data)} 의 data 를 비운다 — 이 템플릿은 행 배정이 없다. "
            "케이스에서 하네스가 입력 환경을 바꾸는 단계는 setup 에 적는다"
        )
    return out


_CHANGES = {"POST", "PUT", "PATCH", "DELETE"}


def _target_rows(contract: TaskContract, plan: HiddenPlan) -> list[str]:
    """경로 변수({id})로 기존 대상을 바꾸는 API 가 있으면 그 대상의 시드 행과 숨은 케이스의 data 가 있어야 한다.

    2026-11-07 알파 테스트 (새 공고): covers 재요청에 LLM 이 대상 엔티티(거래)를 잇지 않고 지워, 계약에 감사 기록 엔티티(0행)만
    남았다. 시작 코드는 기존 API 를 TODO 로 비웠고, 숨은 케이스는 없는 id 로만 요청해 모든 응답이 404 — 흔한 구현도 통과했다.
    """
    apis = [
        f"{e.method} {e.path}"
        for e in contract.endpoints
        if (e.method or "").upper() in _CHANGES and "{" in e.path
    ]
    if not apis:
        return []
    out: list[str] = []
    shown = ", ".join(apis)
    if sum(s.rows for s in contract.seed) == 0:
        out.append(
            f"시드에 행이 하나도 없는데 경로 변수로 기존 대상을 바꾸는 API 가 있음: {shown} — "
            "그 API 가 다루는 대상 엔티티(상태를 가진 것)를 entities·seed 에 두고 covers 로 그 API 를 쓰는 요구에 잇는다. "
            "없는 id 로만 요청하면 모든 응답이 404 라 숨은 케이스가 구현을 가르지 못한다"
        )
    empty = [c.case_id for c in plan.cases if not c.data and not c.setup]
    if empty:
        out.append(
            f"숨은 케이스 {', '.join(empty)} 가 바꿀 행(data)을 선언하지 않음 — {shown} 처럼 기존 대상을 바꾸는 API 를 "
            "시험하면 그 대상 엔티티의 행을 data 로 선언한다 (id 범위는 코드가 나눈다)"
        )
    return out


def _entity_existing(contract: TaskContract, plan: HiddenPlan) -> tuple[list[str], list[str]]:
    """숨은 케이스 data 는 이미 있는 엔티티(existing)에만 — 엔티티에 표시가 하나도 없으면(옛 계약) 판단하지 않는다.
    새로 만드는 엔티티에 시드 행이 있으면 표시만 (드물게 새 참조 테이블일 수 있음)."""
    existing = {m.entity_id for m in contract.entities if m.existing}
    if not existing:
        return [], []
    names = {m.entity_id: m.name for m in contract.entities}
    problems: list[str] = []
    for c in plan.cases:
        bad = sorted({d.entity_id for d in c.data if d.entity_id not in existing})
        if bad:
            problems.append(
                f"숨은 케이스 {c.case_id} 의 data 가 새로 만드는 엔티티({', '.join(names.get(b, b) for b in bad)})에 있음 — "
                "바꿀 행은 이미 있는 엔티티(existing: true, 기존 API 가 다루는 대상)에 둔다. 새 엔티티는 요청으로 쌓이는 것이라 시드 행이 아니다"
            )
    flags = [
        f"새로 만드는 엔티티 {names[s.entity_id]} 에 시드 {s.rows}행 — 이 과제 전에 없던 테이블이면 처음 0행이어야 한다 (사람 확인)"
        for s in contract.seed
        if s.rows > 0 and s.entity_id not in existing and s.entity_id in names
    ]
    return problems, flags


_MUTATING = ("POST", "PUT", "PATCH", "DELETE")
ROW_NEEDS = ("stateful_target", "transition_rules", "empty_record_entity")


def _needs(
    contract: TaskContract, plan: HiddenPlan, seed: FeatureSeed | None, row_seeds: bool = True
) -> list[str]:
    """재료가 선언한 '숨은 요구에 계약이 꼭 가져야 할 것'(FeatureSeed.contract_needs)을 기계적으로 대조 — 빠진 것을 구체적으로 알려 다시 요청.
    2026-11-07~08 알파 평가: 대상 엔티티 없음(감사 기록에 시드), 상태 전이 규칙 없음(숨은 테스트·시작 코드가 다른 전이를 짐작)."""
    if seed is None or not seed.contract_needs:
        return []
    # 행 기반 종류(대상 엔티티·전이 규칙·기록 엔티티)는 행 시드 템플릿에서만, 프론트 종류(바꾸는 API·결과 화면)는 모든 템플릿에서
    need = {n.kind: n.note for n in seed.contract_needs if row_seeds or n.kind not in ROW_NEEDS}
    rows = {s.entity_id: s.rows for s in contract.seed}
    flagged = any(m.existing for m in contract.entities)
    in_cases = {d.entity_id for c in plan.cases for d in c.data}
    targets = [
        m
        for m in contract.entities
        if rows.get(m.entity_id, 0) > 0 and (m.existing or not flagged) and m.entity_id in in_cases
    ]
    out: list[str] = []
    if "stateful_target" in need and not targets:
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 상태를 가진 대상 엔티티: {need['stateful_target']}. "
            "entities 에 existing: true 로 두고, seed 에 행을 두고, 숨은 케이스 data 를 그 엔티티에 둔다"
        )
    # 칸(state)으로 — 화살표 글만 적은 답(plan_contract v9 이하)은 칸으로 옮길 수 있으면 있는 것으로 본다 (fit_state 가 옮긴다)
    if "transition_rules" in need and not any(m.state or legacy_rule(contract, m) for m in targets):
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 상태 전이 규칙: {need['transition_rules']}. 대상 엔티티의 state 칸에 "
            "상태 값 전부(values)·허용 전이(transitions, 'A→B' 꼴로 빠짐없이)·거절 코드(rejected_status)를 적는다 "
            "(숨은 테스트와 시작 코드가 같은 규칙을 따르게)"
        )
    mutating = [e for e in contract.endpoints if (e.method or "").upper() in _MUTATING]
    if "mutation_endpoint" in need and not mutating:
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 화면이 부르는 바꾸는 API: {need['mutation_endpoint']}. "
            "endpoints 에 POST·PUT·PATCH·DELETE 중 하나로 (요청 본문·성공 응답 본문 포함) 적는다"
        )
    if "result_screen" in need and not any(sc.elements for sc in contract.screens):
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 결과를 보여 주는 화면 요소: {need['result_screen']}. "
            "screens 에 그 값을 보여 주는 요소(test id·역할·설명)를 적는다"
        )
    if "empty_record_entity" in need and not [
        m for m in contract.entities if rows.get(m.entity_id, 0) == 0 and not m.existing
    ]:
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 요청마다 쌓이는 기록 엔티티: {need['empty_record_entity']}. "
            "entities 에 existing: false 로 두고 seed 는 rows 0"
        )
    return out


def _internal_expect(plan: HiddenPlan, manifest: TemplateManifest) -> list[str]:
    """숨은 케이스 통과 조건(expect)이 조절값 이름을 말하면 재요청 — 조절값 이름(식별자)을 정확히 맞추는 판정이라 오탐이 드물다.
    2026-11-07 알파 5회차: "모든 요청이 pool_size(30) 내에서 연결을 점유" 처럼 HTTP 로 관찰할 수 없는 조건이라, 숨은 테스트가 단언으로
    옮기다 '시간 초과가 일어나야 한다'로 거꾸로 써 올바른 구현까지 떨어뜨렸다. 2026-11-08 상류 기준선: 9회 중 3회 — 표시만으로는 줄지 않아 재요청으로."""
    names = [p.name for p in manifest.harness_params]
    out: list[str] = []
    for c in plan.cases:
        hit = [
            n
            for n in names
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", c.expect)
        ]
        if hit:
            out.append(
                f"숨은 케이스 {c.case_id} 의 통과 조건(expect)이 조절값({', '.join(hit)})을 말함: {c.expect[:120]} — "
                "통과 조건은 HTTP 로 관찰할 수 있는 결과로만 적는다 (예: '모든 요청이 기대 상태 코드 — 시간 초과·500 없음', "
                "'요청마다 결과와 맞는 기록 1건', '롤백된 요청에 SUCCESS 기록 없음'). 연결 수·풀 크기 같은 내부 동작은 쓰지 않는다 — "
                "조절값은 harness 에만"
            )
    return out


def _foreign_keys(contract: TaskContract) -> list[str]:
    """foreign_keys 의 필드·대상 엔티티가 계약에 있다."""
    names = {m.name for m in contract.entities}
    out: list[str] = []
    for m in contract.entities:
        fields = {f.name for f in m.fields}
        for fk_field, target in m.fk_pairs():
            if fk_field not in fields:
                out.append(f"외래 키 {m.name}.{fk_field} → {target}: {m.name} 에 없는 필드")
            if target not in names:
                out.append(f"외래 키 {m.name}.{fk_field} → {target}: 계약에 없는 엔티티")
    return out


INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "input",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "link",
        "tab",
        "menuitem",
    }
)


def _ui_actions(contract: TaskContract) -> list[str]:
    """조작하는 화면 요소(버튼·입력 등)에 조작 방법(action)이 없음 — 무엇을 하면 언제 서버 요청이 나가는지 계약이 정해야 테스트와 구현이
    같은 조작을 한다. 2026-11-09 React 알파(낙관적 갱신): 수량 입력의 저장 시점이 계약에 없어 테스트는 입력 즉시, 허용 구현은 포커스 이동 때 요청."""
    missing = [
        el.test_id
        for sc in contract.screens
        for el in sc.elements
        if el.role in INTERACTIVE_ROLES and not (el.action or "").strip()
    ]
    if not missing:
        return []
    return [
        f"조작하는 화면 요소에 조작 방법(action)이 없음: {', '.join(missing[:8])} — 무엇을 하면 무엇이 일어나고 서버 요청이 언제 나가는지 "
        "한 줄로 적는다 (예: '클릭하면 수량 1 증가 — 바로 저장 요청', '값을 바꾸고 포커스를 옮기면 저장 요청')"
    ]


_ENUM_SQL = re.compile(r"\bENUM\s*\(([^)]*)\)", re.I)
_ENUM_BLOCK = re.compile(r"\benum\s+\w+\s*\{([^}]*)\}")
_PIPES = re.compile(r"""(?:["']?[A-Za-z][\w-]*["']?\s*\|\s*)+["']?[A-Za-z][\w-]*["']?""")
_TOKEN = re.compile(r"[A-Za-z][\w-]*")
_UPPER = re.compile(r"^[A-Z][A-Z0-9_]*$")
FLAG_MATERIAL = "계약 상태 값이 자료와 다름 (재요청 후에도 — 요구가 새 상태를 더하는지 확인): "


def material_enums(texts: Iterable[str]) -> list[list[str]]:
    """1단계 자료가 적은 값 목록 — SQL ENUM(…), enum X { … }, A|B|C (따옴표 없는 것은 대문자 값만)."""
    out: list[list[str]] = []
    for text in texts:
        for m in _ENUM_SQL.finditer(text):
            out.append(re.findall(r"""["']([^"']+)["']""", m.group(1)))
        for m in _ENUM_BLOCK.finditer(text):
            out.append([x for x in _TOKEN.findall(m.group(1)) if _UPPER.match(x)])
        for m in _PIPES.finditer(text):
            quoted = "'" in m.group(0) or '"' in m.group(0)
            vals = [x for x in _TOKEN.findall(m.group(0)) if quoted or _UPPER.match(x)]
            out.append(vals)
    return [list(dict.fromkeys(v)) for v in out if len(set(v)) >= 2]


def material_state_problems(contract: TaskContract, spec: SpecView) -> list[str]:
    """state 칸 값이 자료의 값 목록과 대부분 겹치는데 자료 밖 값이 있음 — 기존 API 자료가 MEMBER 인데 계약이 GENERAL 처럼.
    2026-11-09 smilegate_2026#002 계약 14개 중 8개가 자료 밖 상태 값(GENERAL·NORMAL·SUB_MASTER, 값이 아닌 RANK 까지)을 지어냈다."""
    enums = material_enums(spec.visible_texts)
    out: list[str] = []
    for m in contract.entities:
        if m.state is None or not enums:
            continue
        values = set(m.state.values)
        best = max(enums, key=lambda e: len(values & set(e)))
        common = values & set(best)
        outside = sorted(values - set(best))
        if outside and len(common) >= max(2, (len(values) + 1) // 2):
            out.append(
                f"{m.name}.state values 의 {', '.join(outside)} 가 1단계 자료의 값({', '.join(best)})에 없음 — "
                "자료가 쓰는 값 그대로 쓴다 (요구가 새 상태를 더하는 것이면 그대로 둔다)"
            )
    return out


def _ui_load_and_background(contract: TaskContract) -> list[str]:
    """ui 계약: 화면마다 진입 때 요청(on_load), 화면 조작 없이 나가는 요청(background)을 정한다 — 없으면 테스트와 구현이
    서로 다르게 짐작한다. 2026-11-09 React 알파: 공개 테스트는 '목록에 들어오면 GET', 구현 4개는 로컬 저장소만 읽어 모두
    '요청이 오지 않음'. 서버 요청이 없는 화면은 '서버 요청 없음', 없는 배경 요청은 '없음' 이라고 적으면 된다."""
    if contract.interface_kind != "ui":
        return []
    out: list[str] = []
    missing = [sc.route for sc in contract.screens if not (sc.on_load or "").strip()]
    if missing:
        out.append(
            f"화면에 들어올 때 동작(on_load)이 없음: {', '.join(missing)} — 무엇을 읽고(로컬 저장소·서버) 어떤 서버 요청이 "
            "나가는지 한 줄로 (서버 요청이 없으면 '서버 요청 없음', 예: '로컬 IndexedDB 의 초안을 읽어 보여 줌 — 서버 요청 없음')"
        )
    if not (contract.background or "").strip():
        out.append(
            "화면 조작 없이 나가는 서버 요청(background)이 없음 — 동기화·재시도처럼 버튼 없이 나가는 요청과 그 시점을 한 줄로 "
            "(예: '브라우저 online 이벤트가 오면 대기열 요청을 순서대로 보냄'). 없으면 '없음'"
        )
    return out


def check_contract(
    contract: TaskContract,
    plan: HiddenPlan,
    spec: SpecView,
    manifest: TemplateManifest,
    seed: FeatureSeed | None = None,
    first_answer: bool = True,
) -> ContractCheck:
    """공개 계약·HiddenPlan 결정적 검사 (§3.3). problems 는 재요청 사유, spec_gaps 는 명세 수정 사유.
    first_answer: 자료와 다른 상태 값은 첫 답에서만 재요청하고 그 뒤엔 확인 표시 (요구가 새 상태를 더할 수 있다)."""
    problems = _ids(contract, plan)
    problems += _coverage(contract, plan, spec)
    problems += _naming(contract, manifest)
    problems += _api_refs(contract, plan, spec)
    problems += _leaks(contract, spec, seed)
    problems += _ui_actions(contract)
    problems += _ui_load_and_background(contract)
    harness_problems, gaps, flags = _harness(plan, spec, manifest)
    problems += harness_problems
    problems += _reasons(plan, manifest, seed)
    if manifest.seed_rows:
        problems += seed_problems(contract)
        problems += case_rows_problems(contract, plan, manifest)
        problems += _target_rows(contract, plan)
    else:
        problems += _no_seed_rows(contract, plan, manifest)
    problems += _foreign_keys(contract)
    problems += _internal_expect(plan, manifest)
    if manifest.seed_rows:
        entity_problems, entity_flags = _entity_existing(contract, plan)
        problems += entity_problems
        flags += entity_flags
    # 재료가 선언한 필요 요소는 행 시드가 없는 템플릿(프론트엔드)에서도 — 2026-11-09 팩트 체크: 행 시드 템플릿에서만 돌아 프론트 재료의 선언이 무시됐다
    problems += _needs(contract, plan, seed, manifest.seed_rows)
    problems += state_problems(contract, plan)
    materials = material_state_problems(contract, spec)
    if first_answer:
        problems += materials
    else:
        flags += [FLAG_MATERIAL + p for p in materials]
    problems += auth_problems(contract, manifest.seed_rows)
    flags += auth_flags(contract)
    problems += header_problems(contract)
    return ContractCheck(problems=problems, spec_gaps=gaps, review_flags=flags)
