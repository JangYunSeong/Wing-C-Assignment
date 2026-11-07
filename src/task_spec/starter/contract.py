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

from task_spec.schemas.build import ContractField, HarnessValue, HiddenPlan, TaskContract
from task_spec.schemas.domain import TaskItem
from task_spec.schemas.incident import FeatureSeed
from task_spec.schemas.template import HarnessParam, TemplateManifest
from task_spec.spec.feature import find_terms
from task_spec.starter.seed_rows import case_rows_problems, seed_problems


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


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
NO_EVIDENCE = "없음"
MIN_QUOTE = 6


def _norm(text: str) -> str:
    return " ".join(text.split())


def evidence_ok(value: HarnessValue, quote: str, texts: Iterable[str]) -> bool:
    """근거 인용 확인 — 인용이 자료(1단계 자료·구체 답)에 그대로 있고(공백 무시) 값이 인용 안에 있다.
    정규식 표기 패턴 대신 쓰는 길 (템플릿마다 표현을 맞추지 않아도 됨)."""
    q = _norm(quote).strip(" \"'“”‘’…")
    if len(q) < MIN_QUOTE or not any(q in _norm(t) for t in texts):
        return False
    numbers = [_number(n) for n in _NUM.findall(q)]
    if isinstance(value, list):
        return all(float(v) in numbers for v in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) in numbers
    return str(value).lower() in q.lower()


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
) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    gaps: list[str] = []
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
        if seen and any(_same(value, raw) for raw in seen):
            continue  # 템플릿 표기 패턴으로 확인 (빠른 길)
        if quote and quote.strip() != NO_EVIDENCE and evidence_ok(value, quote, spec.visible_texts):
            continue  # 근거 인용으로 확인
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
                "자료·답의 문장을 그대로 인용하고 그 안의 값을 쓴다"
            )
        elif seen:
            problems.append(
                f"HiddenPlan {p.name}={value} 이 명세 자료의 값({', '.join(seen)})과 다름 — 명세 자료의 값을 쓰고 evidence 에 근거 문장을"
            )
        else:
            problems.append(
                f"공개 조절값 {p.name}={value} 의 근거가 없음 — evidence 에 그 값이 적힌 자료·답 문장을 그대로 적는다 "
                f'(자료에 없으면 "{NO_EVIDENCE}")'
            )
    return problems, gaps


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
        parts += [f"{el.test_id} {_words(el.test_id)} {el.description}" for el in sc.elements]
        parts += [f"{k.name} {k.shape}" for k in sc.storage]
    for s in contract.seed:
        parts += s.rules
        parts += [f"{k} {v}" for g in s.groups for k, v in g.values.items()]
    parts += [t.checks for t in contract.public_tests]
    return "\n".join(p for p in parts if p)


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
            "요구에 없는 인터페이스·엔티티는 해법을 흘린다"
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


def _reasons(plan: HiddenPlan, manifest: TemplateManifest) -> list[str]:
    codes = manifest.reason_codes
    bad = sorted({c.intended_reason for c in plan.cases if c.intended_reason not in codes})
    if not bad:
        return []
    return [
        f"숨은 케이스의 의도한 실패 사유가 템플릿 코드 목록에 없음: {', '.join(bad)} "
        f"(쓸 수 있는 것: {', '.join(sorted(codes)) or '없음'})"
    ]


_API = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/[A-Za-z0-9_\-/{}.]*)")


def _api_key(method: str, path: str) -> str:
    return f"{method.upper()} {re.sub(r'{[^}]*}', '{}', path.rstrip('/') or '/')}"


def mentioned_apis(texts: Iterable[str]) -> list[str]:
    """글에 나온 'METHOD /path' (경로 변수는 {} 로) — 예: 'PATCH /deliveries/{}/status'."""
    return list(dict.fromkeys(_api_key(m, p) for t in texts for m, p in _API.findall(t)))


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


def check_contract(
    contract: TaskContract,
    plan: HiddenPlan,
    spec: SpecView,
    manifest: TemplateManifest,
    seed: FeatureSeed | None = None,
) -> ContractCheck:
    """공개 계약·HiddenPlan 결정적 검사 (§3.3). problems 는 재요청 사유, spec_gaps 는 명세 수정 사유."""
    problems = _ids(contract, plan)
    problems += _coverage(contract, plan, spec)
    problems += _naming(contract, manifest)
    problems += _api_refs(contract, plan, spec)
    problems += _leaks(contract, spec, seed)
    harness_problems, gaps = _harness(plan, spec, manifest)
    problems += harness_problems
    problems += _reasons(plan, manifest)
    if manifest.seed_rows:
        problems += seed_problems(contract)
        problems += case_rows_problems(contract, plan, manifest)
    else:
        problems += _no_seed_rows(contract, plan, manifest)
    problems += _foreign_keys(contract)
    return ContractCheck(problems=problems, spec_gaps=gaps)
