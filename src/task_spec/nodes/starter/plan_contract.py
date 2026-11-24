"""공개 계약 생성 `plan_contract` (docs/design/spec-to-code.md §2 ②·§3) — LLM 1회 → 결정적 검사 → 1회 재요청.

입력은 승인된 기능형 공통 과제(TaskItem)와 템플릿뿐이다 — 공고·온톨로지·재료에서 나온 글, 지원서 없음 (불변식 3).
- 공개 운영 값은 LLM 이 값 + 근거 인용(hidden_plan.evidence) + 그 안의 값 글자(evidence_value)를 내고 코드가 자료와 대조한다. 근거가 "없음"인 필수 값은 명세 결함 — 재요청 없이 실패.
- 계약 검사(`starter.contract.check_contract`)의 `problems` 는 재요청 사유로 붙이고, `spec_gaps` 는 재요청 없이 실패로 둔다.
- 재요청 뒤에도 문제가 남으면 가장 문제가 적은 답을 돌려주고 표시를 남긴다 — 빌드 그래프가 `failed` 로 승인 화면에 올린다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass, field, replace

from pydantic import BaseModel

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.nodes.starter.generate import reason_line
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    FeatureSeed,
    HiddenPlan,
    MaterialGuide,
    MaterialInterface,
    NodeError,
    PlanContractInputAll,
    PlanContractInputV3,
    PlanContractOutput,
    Provenance,
    TaskContract,
    TaskItem,
)
from task_spec.schemas.domain import HiddenAcceptance
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.auth_rule import fit_auth
from task_spec.starter.contract import (
    EVIDENCE_FRAGMENT,
    NO_EVIDENCE,
    ROW_NEEDS,
    SpecView,
    check_contract,
    describe_rule,
    evidence_fragment,
    field_style,
    fit_covers,
    fit_field_actions,
    fit_forms,
    fit_traps,
    hidden_terms,
    param_unit,
    timed_hints,
    unevidenced,
)
from task_spec.starter.header_values import fit_per_request
from task_spec.starter.hidden_helpers import SETTING_MIN_ROWS, SETTING_PROBE, SETTING_PROBE_ALT
from task_spec.starter.interface import fit_interface, interface_text, stale_name_problems
from task_spec.starter.material_guide import contract_rules_text, with_guide
from task_spec.starter.seed_rows import RESERVED_ROWS, fit_row_targets, fit_seed
from task_spec.starter.state_rule import fit_state
from task_spec.starter.templates import Template, restrict_reasons, supports_settings

TASK = "plan_contract"
NONE = "(없음)"
FLAG_SPEC_GAP = "명세 1단계 자료·추가 요구 답에 운영 수치가 없음 — 명세 수정 요청 필요: "
FLAG_INVALID = "공개 계약 자동 검증 실패 (재요청 후에도) — 확인 필요: "
FLAG_FAILED = "공개 계약 생성 실패 (LLM 응답 없음·형식 오류)"

logger = logging.getLogger(__name__)


@dataclass
class PlanResult:
    contract: TaskContract | None = None
    hidden_plan: HiddenPlan | None = None
    problems: list[str] = field(default_factory=list)  # 채택한 답에 남은 문제
    first_problems: list[str] = field(default_factory=list)  # 첫 답의 문제 (평가용)
    spec_gaps: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)
    errors: list[NodeError] = field(default_factory=list)
    fail_reasons: list[str] = field(default_factory=list)  # 위치·오류 종류만
    adjustments: list[str] = field(
        default_factory=list
    )  # 코드가 맞춘 행 수 (seed_rows.fit_seed) — 승인 화면에 보임
    raw: PlanContractOutput | None = None  # 채택한 답의 LLM 원본 (코드가 맞추기 전) — 회귀 검사용

    @property
    def ok(self) -> bool:
        return self.contract is not None and not self.problems and not self.spec_gaps


def _range(lo: float | None, hi: float | None) -> str:
    if lo is None and hi is None:
        return ""
    return f", {'' if lo is None else f'{lo:g}'}–{'' if hi is None else f'{hi:g}'}"


# 계약 생성 시도 수 — 첫 답 + 재요청 2회. 2026-11-08 상류 평가: 검사 항목이 늘자(전이 규칙 + 관찰 불가 통과 조건) 재요청 한 번에
# 다 고치지 못해 계약 실패가 0/9 → 3/9 로 늘었다 (재요청은 필요할 때만 — 회당 약 $0.03).
ATTEMPTS = 3


def plan_evidence_missing(plan: HiddenPlan, name: str) -> bool:
    quote = plan.evidence.get(name)
    return name in plan.harness and (not quote or quote.strip() == NO_EVIDENCE)


def for_material(
    template: Template, seed: FeatureSeed | None, guide: MaterialGuide | None = None
) -> Template:
    """재료가 선언한 조절값(FeatureSeed.harness)만 필수이고, 지원자가 자료에서 찾을 운영 조건이라 보이는 값 — 명세 자료의 근거
    인용으로 값을 확인한다. 재료가 harness 를 적지 않으면(None) 템플릿 그대로 (예전과 같음).
    2026-11-10 nplus1-d: query_delay_ms·request_timeout_s 가 템플릿에서 안 보이는 값이라 명세의 '쿼리당 11ms·응답 한도 1초' 대신
    하네스 기본값(20ms·30초)이 들어가, 지연 로딩 흔한 구현(200건 ≈ 8초)도 hc1 을 통과했다.
    guide: 재료 숨은 안내 (#256 재료 옮기기) — 재료 사유를 템플릿 사유에 합친 뒤 좁힌다 (빌드 BuildInputs 와 같은 순서)."""
    template = with_guide(template, guide)
    if seed is None:
        return template
    template = restrict_reasons(
        template, seed.reasons
    )  # 사유도 재료가 적은 것만 — 빌드(BuildInputs)와 같은 목록
    if seed.harness is None:
        return template
    need = set(seed.harness)
    notes = dict(seed.harness_notes)
    for (
        name,
        rule,
    ) in seed.harness_rules.items():  # 계약 검사가 대조하는 규칙을 계약 입력에도 같은 말로
        how = f"근거에서 값을 만드는 규칙: {describe_rule(rule, param_unit(name))}"
        notes[name] = f"{notes[name]} ({how})" if name in notes else how
    mechanics = template_mechanics(template.manifest)
    params = [
        p.model_copy(
            update={
                "required": p.name in need,
                "visible": p.visible or p.name in need,
                "description": f"{p.description} — 이 과제: {notes[p.name]}"
                if p.name in notes
                else p.description,
            }
        )
        for p in template.manifest.harness_params
        if not seed.harness
        or p.name
        in need  # 조절값을 나열한 재료는 그것만 — 검사·입력·기본값 채우기가 같은 목록을 본다
        or p.name
        in mechanics  # 숨은 장치(기본값 있는 안 보이는 값)는 재료 나열과 무관하게 템플릿 것 — template_mechanics
    ]
    return replace(
        template, manifest=template.manifest.model_copy(update={"harness_params": params})
    )


def fill_hidden_defaults(plan: HiddenPlan, m: TemplateManifest) -> tuple[HiddenPlan, list[str]]:
    """지원자에게 보이지 않는 조절값이 버스트 행 계산(burst_rows)·필수인데 빠졌으면 manifest 기본값으로 — 재요청하지 않는다.
    2026-11-07 알파 3×3: bursts 가 빠져 '버스트 행 수를 확인할 조절값이 없음' 이 재요청에도 안 고쳐져 계약 생성 실패.
    보이는 조절값(자료에 적힌 운영 값)은 채우지 않는다 — 그건 명세에서 와야 한다."""
    need = list(dict.fromkeys([*m.burst_rows, *(p.name for p in m.harness_params if p.required)]))
    harness = dict(plan.harness)
    notes: list[str] = []
    for name in need:
        p = m.param(name)
        if name in harness or p is None or p.visible or p.default is None:
            continue
        value = int(p.default) if p.kind == "int" else p.default
        harness[name] = value
        notes.append(f"숨은 조절값 {name} 가 없어 템플릿 기본값 {value} 을 넣음")
    if not notes:
        return plan, []
    return plan.model_copy(update={"harness": harness}), notes


def seed_rules_text(m: TemplateManifest) -> str:
    """시드 행 수 규칙 — 검사(seed_rows)와 같은 값을 프롬프트에 그대로. DB 행을 바꾸지 않는 템플릿이면 입력 환경 안내."""
    if not m.seed_rows:
        return (
            "- 이 템플릿은 DB 행 시드를 쓰지 않는다: seed 와 숨은 케이스 data 는 비운다.\n"
            "- 시작 코드가 깔아 줄 입력 환경(DB 배치·입력 파일·외부 서비스 응답·브라우저 상태)은 contract.fixtures 에, "
            "숨은 케이스에서 하네스가 그 환경을 바꾸는 단계는 hidden_plan.cases.setup 에 적는다."
        )
    lines = [
        f"- 숨은 케이스는 작은 id {RESERVED_ROWS}행(id 1~{RESERVED_ROWS})을 쓰지 않는다 (지원자·공개 테스트 몫). "
        f"그래서 엔티티마다 rows ≥ 그 엔티티를 바꾸는 숨은 케이스 data 행 수의 합 + {RESERVED_ROWS}, "
        "처음 값별로도 맞는 groups 의 행이 그만큼 있어야 한다.",
    ]
    if m.burst_rows:
        lines.append(
            f"- 버스트 케이스(data.burst=true)의 rows ≥ {' × '.join(m.burst_rows)} (hidden_plan.harness 에 넣은 값의 곱)."
        )
    return "\n".join(lines)


def row_rules_text(m: TemplateManifest, seed: FeatureSeed | None = None) -> str:
    """v17~ 행 규칙 — 행 수 계산식 대신 무엇을 적으면 코드가 맞추는지만. 행 시드를 쓰지 않는 템플릿은 입력 환경 안내."""
    if not m.seed_rows:
        return seed_rules_text(m) + needs_text(seed, m.seed_rows)
    lines = [
        "- 숨은 케이스 data 에는 바꿀 행의 엔티티·행 수·처음 값만 적는다. 시드 rows·groups 의 행 수는 어림해도 된다 — "
        "숨은 케이스 몫·공개 테스트 몫·작은 id 지원자 몫이 모자라면 코드가 그 처음 값의 범위를 시드 끝에 덧붙인다.",
    ]
    if m.burst_rows:
        lines.append(
            f"- 동시 요청마다 다른 행을 쓰는 케이스는 data.burst=true — 행 수는 코드가 {' × '.join(m.burst_rows)} 로 맞춘다."
        )
    return "\n".join(lines) + needs_text(seed, m.seed_rows)


_UI_RULES = (
    "- `ui`: `screens` 에 화면마다 경로·요약·요소(`test_id`·역할·설명·조작)·브라우저 저장소(이름·종류·모양)를 적고, 화면이 부르는 서버 API 는 "
    "`endpoints` 에 (메서드·경로). covers 에 화면 id(s1…)도 넣는다. test id 는 이름 규칙을 따르고 숨은 요구·운영 조건을 드러내는 이름·설명을 쓰지 않는다.\n"
    "  - 조작하는 요소(버튼·입력·선택)마다 `action`: 무엇을 하면 무엇이 일어나고 **서버 요청이 언제 나가는지** 한 줄 "
    "(예: '클릭하면 수량 1 증가 — 바로 저장 요청', '값을 바꾸고 포커스를 옮기면 저장 요청').\n"
    "  - 화면마다 `on_load`: 들어올 때 무엇을 읽고 어떤 서버 요청이 나가는지 한 줄 (없으면 '서버 요청 없음', "
    "예: '로컬 IndexedDB 의 초안을 읽어 보여 줌 — 서버 요청 없음').\n"
    "  - 계약 `background`: 화면 조작 없이 나가는 서버 요청과 그 시점 (예: '브라우저 online 이벤트가 오면 대기열 요청을 순서대로 보냄', 없으면 '없음'). "
    "공개 수용 기준의 '서버에 반영됨'은 서버 상태로 확인할 일이지 앱이 조회 요청을 보낸다는 뜻이 아니다 — 앱이 실제로 보내는 요청만 적는다."
)
_FIXTURE_RULES = (
    "- 입력 환경: `seed`·케이스 `data` 는 비우고, 시작 코드가 깔아 줄 입력 환경(DB 배치·입력 파일·외부 서비스 응답·브라우저 상태)을 "
    "`contract.fixtures` 에 (공개), 숨은 케이스에서 하네스가 그 환경을 바꾸는 단계를 `hidden_plan.cases.setup` 에 (숨김) 적는다. "
    "숨은 요구·운영 조건은 fixtures 에 쓰지 않고 setup 에만."
)


def interface_rules_text(m: TemplateManifest) -> str:
    """v17~ 템플릿이 지원하는 인터페이스의 안내만 — v16 은 백엔드 계약에도 ui·cli·입력 환경 안내가 다 들어갔다."""
    lines: list[str] = []
    if "http" in m.interfaces:
        lines.append("- `http`: 엔드포인트마다 메서드·경로, 응답은 HTTP 상태 코드.")
    if {"cli", "file"} & set(m.interfaces):
        lines.append(
            "- `cli`·`file`: 엔드포인트는 명령·입출력 파일(메서드 없음, `path` 에 명령 이름이나 파일 경로), 응답 `status` 는 "
            "**종료 코드 0~255** (예: 0 성공, 1 검증 실패)."
        )
    if "ui" in m.interfaces:
        lines.append(_UI_RULES)
    if not m.seed_rows:
        lines.append(_FIXTURE_RULES)
    return "\n".join(lines) or "- 템플릿 인터페이스 그대로."


def for_prompt(inp: PlanContractInputV3, model: type[BaseModel]) -> BaseModel:
    """프롬프트 버전이 선언한 입력 모델로 — v1·v2 는 seed_rules 자리가 없다 (Schema 는 모르는 필드를 거부)."""
    return model.model_validate({k: v for k, v in dict(inp).items() if k in model.model_fields})


def needs_text(seed: FeatureSeed | None, row_seeds: bool = True) -> str:
    """재료가 선언한 '숨은 요구에 계약이 꼭 가져야 할 것' — 시드 규칙 칸 뒤에 붙는다 (검사 _needs 와 같은 목록 — 행 기반 종류는 행 시드 템플릿만)."""
    needs = [
        n
        for n in (seed.contract_needs if seed is not None else [])
        if row_seeds or n.kind not in ROW_NEEDS
    ]
    if not needs:
        return ""
    return "\n- 숨은 요구가 계약에 꼭 필요로 하는 것 (빠지면 다시 요청):\n" + "\n".join(
        f"  - {n.note or n.kind}" for n in needs
    )


def forbidden_text(seed: FeatureSeed | None, spec: SpecView) -> str:
    """공개 계약에 쓰지 않는 말 — 생성 뒤 검사(contract._leaks)가 쓰는 목록 그대로 미리 알린다. 2026-11-10 데이터 알파 9~11회차:
    gpt-4.1 첫 답이 매번 '수신 시각'(재료의 운영 조건 낱말) 하나로 재요청 — 입력에 목록이 없어 모델과 무관하게 호출 하나를 썼다."""
    terms = hidden_terms(seed, spec)
    if not terms:
        return ""
    return (
        "\n- 공개 계약(경로·필드 이름·설명·fixtures·seed·공개 테스트 checks)에 쓰지 않는 말 — 숨은 요구·운영 조건을 드러낸다 "
        f"(숨은 케이스 scenario·expect·setup 에만): {', '.join(terms)}"
    )


def template_mechanics(m: TemplateManifest) -> set[str]:
    """재료가 조절값을 나열해도 남기는 숨은 장치 — 지원자에게 보이지 않고 기본값이 있는 조절값(예: bursts 반복 수).
    재료 나열은 지원자가 자료에서 찾을 **운영 값**을 고르는 것이고, 숨은 장치는 재료와 무관한 템플릿 것이라 코드가 기본값으로 채운다
    (fill_hidden_defaults). 2026-11-13 갱신 손실 재료가 concurrency 를 나열하자 bursts 까지 걸러져 버스트 행 수를 못 정했다."""
    return {p.name for p in m.harness_params if not p.visible and p.default is not None}


def fit_evidence_fragments(
    plan: HiddenPlan, m: TemplateManifest, spec: SpecView, seed: FeatureSeed | None = None
) -> tuple[HiddenPlan, list[str]]:
    """근거 인용이 자료에 그대로 없으면(LLM 이 글자를 바꿈) 자료에 그대로 있는 가장 긴 조각으로 바꾼다 — 그 조각을 값 글자 검사(value_check)로
    다시 보고, 결과와 상관없이 승인자 표시를 붙인다 (값이 자료에서 왔다는 건 확인되지만 그 값이 이 조절값이라는 연결은 LLM 이
    고친 부분이 만들 수 있다). 저장하는 근거도 자료 원문 조각 — 승인자가 실제 문장을 본다. 결정적이라 replay_contracts 가 다시 만든다.
    [확정: 2026-11-12 사용자 — 날짜·시각 'T' 같은 하나씩 땜질 대신 일반적인 방법]"""
    evidence = dict(plan.evidence)
    notes: list[str] = []
    for p in m.harness_params:
        quote = evidence.get(p.name)
        if not p.visible or p.name not in plan.harness or not quote or quote.strip() == NO_EVIDENCE:
            continue
        frag = evidence_fragment(
            plan.harness[p.name],
            quote,
            plan.evidence_value.get(p.name),
            spec.visible_texts,
            param_unit(p.name),
            seed.harness_rules.get(p.name) if seed is not None else None,
        )
        if frag is None:
            continue
        evidence[p.name] = frag
        notes.append(
            f"{EVIDENCE_FRAGMENT}{p.name}={plan.harness[p.name]}: 계약의 인용 {quote[:100]!r} 대신 자료 원문 조각 {frag!r} 을 근거로 — "
            "값과 이 조절값의 연결은 승인자 확인"
        )
    if not notes:
        return plan, []
    return plan.model_copy(update={"evidence": evidence}), notes


def fit_unevidenced(
    plan: HiddenPlan, m: TemplateManifest, spec: SpecView
) -> tuple[HiddenPlan, list[str]]:
    """근거를 '없음'으로 쓴 선택 조절값(보이는 값, 필수 아님)이 자료 어디에도 없으면 뺀다 — 검사는 '빼라'고 다시 묻지만
    LLM 이 끝까지 못 뺐다 [확정: 2026-11-11 사용자]. 2026-11-11 백엔드 중복 처리 알파: 재료가 조절값을 나열하지 않아(harness: [])
    템플릿 조절값이 다 보였고, concurrency 를 '자료에 없음'으로 적고도 넣어 계약이 세 번 다 막혔다.
    필수 조절값은 빼지 않는다 (명세 수정 필요로 남는다). 동시 요청 조절값이 빠지면 버스트 표시도 끈다."""
    drop = sorted(
        p.name
        for p in m.harness_params
        if p.name in plan.harness
        and p.visible
        and not p.required
        and (plan.evidence.get(p.name) or "").strip() == NO_EVIDENCE
        and unevidenced(p, plan.harness[p.name], spec.visible_texts)
    )
    if not drop:
        return plan, []
    harness = {k: v for k, v in plan.harness.items() if k not in drop}
    evidence = {k: v for k, v in plan.evidence.items() if k not in drop}
    values = {k: v for k, v in plan.evidence_value.items() if k not in drop}
    no_burst = any(n in drop for n in m.burst_rows)
    cases = [
        c.model_copy(update={"data": [d.model_copy(update={"burst": False}) for d in c.data]})
        if no_burst and any(d.burst for d in c.data)
        else c
        for c in plan.cases
    ]
    fixed = plan.model_copy(
        update={"harness": harness, "evidence": evidence, "evidence_value": values, "cases": cases}
    )
    return fixed, [
        f"근거가 '없음'인 선택 조절값 뺌: {', '.join(drop)} (자료에 그 값이 없음)"
        + (" — 버스트 표시도 끔" if no_burst and cases != list(plan.cases) else "")
    ]


def fit_material_params(
    plan: HiddenPlan, m: TemplateManifest, known: Collection[str] | None = None
) -> tuple[HiddenPlan, list[str]]:
    """재료가 고르지 않은 템플릿 조절값을 LLM 이 넣으면 뺀다 (입력에 보이지 않는 값 — 안전망). m 은 재료로 좁힌 매니페스트
    (for_material), known 은 좁히기 전 템플릿의 조절값 이름. 템플릿에 없는 키는 지우지 않는다 — 계약 검사가 '템플릿에 없는 조절값'으로
    다시 묻는다 (#179: 예전에는 그것까지 조용히 지워 그 검사가 운영에서 한 번도 걸리지 않았다).
    동시 요청 조절값(burst_rows)이 빠지면 숨은 케이스 데이터의 버스트 표시도 끈다 (버스트 행 수를 잴 조절값이 없다)."""
    keep = {p.name for p in m.harness_params}
    template = set(known) if known is not None else keep
    extra = sorted(k for k in plan.harness if k not in keep and k in template)
    if not extra:
        return plan, []
    harness = {k: v for k, v in plan.harness.items() if k not in extra}
    evidence = {k: v for k, v in plan.evidence.items() if k not in extra}
    values = {k: v for k, v in plan.evidence_value.items() if k not in extra}
    no_burst = any(n not in keep for n in m.burst_rows)
    cases = [
        c.model_copy(update={"data": [d.model_copy(update={"burst": False}) for d in c.data]})
        if no_burst and any(d.burst for d in c.data)
        else c
        for c in plan.cases
    ]
    fixed = plan.model_copy(
        update={"harness": harness, "evidence": evidence, "evidence_value": values, "cases": cases}
    )
    return fixed, [
        f"재료가 쓰지 않는 조절값 뺌: {', '.join(extra)} (FeatureSeed.harness 에 없음)"
        + (" — 버스트 표시도 끔" if no_burst and cases != list(plan.cases) else "")
    ]


def settings_text(m: TemplateManifest) -> str:
    """템플릿 정보의 설정 칸 줄 — 쓸 수 있는지(templates.supports_settings)·키 형식·그 조건의 시드 행 수(hidden_helpers 상수)를 코드가.
    #185: 프롬프트가 'Java 템플릿일 때만'·'4행 이상'을 글로 박아, 덮는 값이 바뀌면 조용히 어긋났다."""
    if not supports_settings(m):
        return (
            "\n- 설정 칸(settings): 이 템플릿은 쓰지 않는다 — `settings: []` 로 두고, '설정에 따라' 정해지는 값도 계약"
            "(응답 설명·명령 옵션)에 적는다"
        )
    return (
        "\n- 설정 칸(settings): 쓸 수 있다 — `key` 는 스프링 속성 키(소문자·점·하이픈, 예 `marketplace.listing.page-size`). "
        f"그 설정을 따르는 조건의 시드 행은 {SETTING_MIN_ROWS}행 이상 (채점 테스트가 설정을 {SETTING_PROBE}"
        f"(기본값이 {SETTING_PROBE}이면 {SETTING_PROBE_ALT})로 덮어 결과가 정확히 그만큼인지 본다)"
    )


def plan_input(
    task: TaskItem,
    template: Template,
    note: str | None = None,
    seed: FeatureSeed | None = None,
    interface: MaterialInterface | None = None,
) -> PlanContractInputV3:
    m = template.manifest
    rx = m.naming
    params = "\n".join(
        f"- {p.name} ({p.kind}{', 필수' if p.required else ', 선택'}"
        f"{', 지원자에게 보임 — 자료·답에 적힌 값 그대로' if p.visible else ''}"
        f"{_range(p.minimum, p.maximum)}) — {p.description}"
        for p in m.harness_params  # 재료로 좁힌 템플릿 (for_material) — 조절값·사유를 여기서 다시 거르지 않는다 (#179)
    )
    reasons = "\n".join(reason_line(r) for r in m.failure_reasons)
    return PlanContractInputAll(
        template=(
            f"{m.ref} — 인터페이스: {', '.join(m.interfaces)}\n"
            f"이름 규칙: 경로 {rx.path} · 헤더 {rx.header} · 엔티티 {rx.entity} · 필드 {rx.field}"
            f"\n- 필드 이름(엔티티 필드·요청 필드·옵션 모두)은 {field_style(rx.field)}"
            + (f" · 화면 test id {rx.test_id}" if "ui" in m.interfaces else "")
            + "".join(f"\n- {g}" for g in m.contract_guide)
            + settings_text(m)
        ),
        harness_params=params or NONE,
        failure_reasons=reasons or NONE,
        title=task.title or NONE,
        scenario=task.scenario or NONE,
        requirements="\n".join(f"{r.req_id} {r.text}" for r in task.requirements) or NONE,
        acceptance_public="\n".join(f"{a.ac_id} {a.text}" for a in task.acceptance_public) or NONE,
        deliverables="\n".join(f"- {d}" for d in task.deliverables) or NONE,
        traps="\n".join(f"{t.trap_id} {t.description}" for t in task.traps) or NONE,
        acceptance_hidden="\n".join(hidden_ac_line(h) for h in task.acceptance_hidden) or NONE,
        materials="\n\n".join(f"[{a.artifact_id}] {a.title}\n{a.content}" for a in task.artifacts)
        or NONE,
        clarifications="\n".join(
            f"[{c.clarification_id}] {c.topic}: {c.answer}" for c in task.clarifications
        )
        or NONE,
        revision_note=note or NONE,
        seed_rules=seed_rules_text(m)
        + needs_text(seed, m.seed_rows)  # 프론트 종류는 행 시드가 없는 템플릿에도
        # 재료가 정한 명령·입출력 파일·엔티티 (#256) — 없으면 빈 글. needs_text 처럼 seed_rules(V3 판)·row_rules(V4 판) 둘 다에
        # 붙인다 — 판마다 둘 중 하나만 렌더된다 (for_prompt 가 판의 입력 모델로 거른다)
        + interface_text(interface)
        + contract_rules_text(template)  # 재료 안내의 공개 테스트 입력 범위 (#256) — 없으면 빈 글
        + forbidden_text(seed, SpecView.from_task(task)),
        row_rules=row_rules_text(m, seed)
        + interface_text(interface)
        + contract_rules_text(template),
        interface_rules=interface_rules_text(m),
    )


def fit_fk_targets(contract: TaskContract) -> tuple[TaskContract, list[str]]:
    """외래 키 대상을 엔티티 id(m2)로 쓴 것을 이름(Customer)으로 — 2026-11-10 목록 재료 계약: 'customerId → m2' 를 재요청에도
    그대로 내 '계약에 없는 엔티티' 로 계약이 실패했다 (검사는 이름으로 찾는다)."""
    names = {m.entity_id: m.name for m in contract.entities}
    notes: list[str] = []
    entities = []
    for m in contract.entities:
        fks = []
        for fk in m.foreign_keys:
            field_name, _, target = (x.strip() for x in re.split(r"(→|->)", fk, maxsplit=1))
            if target in names and target not in names.values():
                notes.append(
                    f"{m.name} 외래 키 {field_name} → {target} 를 엔티티 이름 {names[target]} 로"
                )
                fk = f"{field_name} → {names[target]}"
            fks.append(fk)
        entities.append(m.model_copy(update={"foreign_keys": fks}) if fks != m.foreign_keys else m)
    return (contract.model_copy(update={"entities": entities}), notes) if notes else (contract, [])


def hidden_ac_line(h: HiddenAcceptance) -> str:
    """숨김 수용 기준 한 줄 + 승인된 실패 지정 (계약 단계가 그 케이스 faults 로 옮긴다 — 코드가 같은 값인지 대조)."""
    lines = [f"{h.ac_id} ({h.trap_id}) {h.text}"]
    if h.items:
        lines.append(
            f"  테스트가 만들 항목 (승인됨 — 이 기준의 숨은 케이스는 이 이름 그대로 만든다): {', '.join(h.items)}"
        )
    for f in h.faults:
        what = f"{f.status} 응답" if f.status is not None else "연결 실패"
        lines.append(
            f'  실패 지정 (승인됨 — 이 기준의 숨은 케이스 faults 로 옮긴다): {f.operation} — 항목 "{f.item}" 처음 {f.times}번 → {what}'
        )
    return "\n".join(lines)


def fit_output(
    out: PlanContractOutput,
    manifest: TemplateManifest,
    spec: SpecView,
    seed: FeatureSeed | None = None,
    known: Collection[str] | None = None,
    interface: MaterialInterface | None = None,
) -> tuple[PlanContractOutput, list[str]]:
    """LLM 답 → 코드가 맞춘 계약 (조절값 기본값 · 시드 행 수 · 상태 칸 · 권한 칸 · 요청 번호 헤더 · covers). 결정적 — 저장한 LLM 답으로
    회귀 검사(eval/starter/replay_contracts.py)가 LLM 없이 같은 결과를 다시 만든다. manifest 는 재료로 좁힌 것(for_material),
    known 은 좁히기 전 템플릿의 조절값 이름 (없으면 manifest 것). interface 는 재료가 정한 명령·입출력 파일·엔티티 (#256) — 맨 먼저
    계약에 채운다 (뒤 단계 맞춤이 그 위에서 돈다)."""
    out, notes = fit_interface(out, interface, spec.req_ids, spec.public_ac_ids)
    trimmed, param_notes = fit_material_params(out.hidden_plan, manifest, known)
    notes += param_notes
    trimmed, free_notes = fit_unevidenced(trimmed, manifest, spec)
    notes += free_notes
    trimmed, frag_notes = fit_evidence_fragments(trimmed, manifest, spec, seed)
    notes += frag_notes
    filled, fill_notes = fill_hidden_defaults(trimmed, manifest)
    notes += fill_notes
    named, fk_notes = fit_fk_targets(out.contract)
    out = out.model_copy(update={"hidden_plan": filled, "contract": named})
    notes += fk_notes
    targeted, target_notes = fit_row_targets(
        out.contract
    )  # rows 는 API 가 돌려주는 행만 — 시드 그룹을 만들기 전에
    out = out.model_copy(update={"contract": targeted})
    notes += target_notes
    if manifest.seed_rows:  # 행 수 맞추기는 DB 행 과제에서만
        fitted, fitted_plan, seed_notes = fit_seed(out.contract, out.hidden_plan, manifest)
        out = out.model_copy(update={"contract": fitted, "hidden_plan": fitted_plan})
        notes += seed_notes
    stated, state_notes = fit_state(out.contract)
    out = out.model_copy(update={"contract": stated})
    notes += state_notes
    authed, auth_notes = fit_auth(
        out.contract, manifest.seed_rows
    )  # 요청자 행은 행 수 맞춘 뒤 시드 끝에
    out = out.model_copy(update={"contract": authed})
    notes += auth_notes
    keyed, key_notes = fit_per_request(out.contract)  # 요청 번호·멱등 키 헤더 — 권한 칸이 정해진 뒤
    out = out.model_copy(update={"contract": keyed})
    notes += key_notes
    covered, cover_notes = fit_covers(out.contract, spec)
    out = out.model_copy(update={"contract": covered})
    notes += cover_notes
    formed, form_notes = fit_forms(
        out.contract
    )  # 폼 입력란·제출 버튼 — 폼이 적은 제출 요청의 칸에서
    out = out.model_copy(update={"contract": formed})
    notes += form_notes
    acted, act_notes = fit_field_actions(
        out.contract
    )  # 조작 방법이 빠진 입력란 — 제출 요소가 하나뿐인 화면만 (폼 보정이 만든 입력란 뒤에)
    out = out.model_copy(update={"contract": acted})
    notes += act_notes
    trapped, trap_notes = fit_traps(
        out.hidden_plan, spec
    )  # 사유 코드를 적은 trap_id → 숨김 수용 기준의 숨은 요구
    out = out.model_copy(update={"hidden_plan": trapped})
    notes += trap_notes
    unfaulted, fault_notes = fit_faults(out.hidden_plan, spec)
    out = out.model_copy(update={"hidden_plan": unfaulted})
    notes += fault_notes
    return out, notes


def fit_faults(plan: HiddenPlan, spec: SpecView) -> tuple[HiddenPlan, list[str]]:
    """실패 지정(HiddenCase.faults)은 명세의 숨김 수용 기준에 승인된 실패 지정이 있는 케이스에서만 — 나머지는 LLM 이 채워도 뺀다
    (무엇을 실패시킬지는 명세가 정한다). 출력 스키마가 프롬프트 본문에 들어가 이전 프롬프트 판에도 이 칸이 보인다 — 뺀 그대로면
    실패 지정이 없는 명세의 계약·숨은 테스트는 전과 같다."""
    dropped = [c.case_id for c in plan.cases if c.faults and c.ac_id not in spec.hidden_faults]
    if not dropped:
        return plan, []
    cases = [c.model_copy(update={"faults": []}) if c.case_id in dropped else c for c in plan.cases]
    return plan.model_copy(update={"cases": cases}), [
        f"실패 지정 뺌 ({', '.join(dropped)}) — 명세의 숨김 수용 기준에 승인된 실패 지정이 없음"
    ]


async def plan_contract(
    task: TaskItem,
    template: Template,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    seed: FeatureSeed | None = None,
    reviewer_note: str | None = None,
    interface: MaterialInterface | None = None,
    guide: MaterialGuide | None = None,
) -> PlanResult:
    """승인된 기능형 명세 → 공개 계약 + 숨은 테스트 계획. guide: 재료 숨은 안내 (사유·공개 테스트 입력 범위 — for_material)."""
    res = PlanResult()
    spec = SpecView.from_task(task)
    known = {
        p.name for p in template.manifest.harness_params
    }  # 좁히기 전 — 템플릿에 없는 키는 검사가 말한다
    template = for_material(template, seed, guide)
    manifest = template.manifest
    # 2026-11-07: 템플릿 표기 패턴으로 못 찾으면 LLM 전에 실패시켰는데, 패턴이 한 예시 표현에만 맞아 값이 있는 명세도 막혔다
    # → LLM 이 값 + 근거 인용을 내고 코드가 확인한다 (check_contract). 근거가 "없음"이면 그때 명세 결함.

    prompt_t = prompts.get(TASK)
    base_note = f"승인자 수정 요청: {reviewer_note}" if reviewer_note else None
    note = base_note
    best: tuple[PlanContractOutput, list[str], list[str], list[str], list[str]] | None = None
    best_raw: PlanContractOutput | None = None
    for attempt in range(ATTEMPTS):
        prompt = assemble(
            prompt_t,
            for_prompt(plan_input(task, template, note, seed, interface), prompt_t.input_model),
            ontology_version=ontology_version,
        )
        try:
            r = await llm.call(  # 마지막 재시도는 추론 없이 — 출력은 검사·수리 루프가 다시 본다
                prompt,
                prompt_t.output_model,  # type: ignore[arg-type]
                reasoning_off_last=True,
            )
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(node=TASK, code=type(exc).__name__, message="공개 계약 생성 실패")
            )
            # 종류(':' 앞)에 끝난 이유를 붙인다 — 빈 응답이 잘림(length)인지 그냥 빈 답(stop)인지 실패 사유만 보고 안다
            fr = getattr(exc, "finish_reason", None)
            res.fail_reasons.append(
                f"{type(exc).__name__}{f'(finish_reason={fr})' if fr else ''}: {str(exc)[:300]}"
            )
            if (spent := getattr(exc, "provenance", None)) is not None:
                res.provenance.append(spent)  # 실패한 호출도 비용·호출 수에
            continue
        res.provenance.append(r.provenance)
        out: PlanContractOutput = r.parsed  # type: ignore[assignment]
        raw = out
        out, notes = fit_output(out, manifest, spec, seed, known, interface)
        check = check_contract(
            out.contract,
            out.hidden_plan,
            spec,
            manifest,
            seed,
            first_answer=attempt == 0,
            interface=interface,
        )
        # 코드가 계약 이름을 재료 것으로 바꿨는데 테스트 글이 LLM 의 옛 이름을 쓰면 재요청 (#256) — 맞추기 전 답(raw)에서 본다
        check.problems.extend(stale_name_problems(raw, interface))
        logger.info(
            "%s attempt=%d problems=%d gaps=%d latency_ms=%d",
            TASK,
            attempt + 1,
            len(check.problems),
            len(check.spec_gaps),
            r.provenance.latency_ms,
        )
        score = len(check.problems) + len(check.spec_gaps)
        if best is None or score < len(best[1]) + len(best[2]):
            best = (out, check.problems, check.spec_gaps, notes, check.review_flags)
            best_raw = raw
        # 근거를 '없음'으로 쓴 시간 조절값인데 자료에 그 단위의 수가 있으면 한 번 더 묻는다 — 2026-11-10 nplus1-f: 자료의
        # '응답 p99 ≤ 800 ms' 를 두고 request_timeout_s=2 · 근거 없음으로 써 명세 결함으로 끝났다
        hints = (
            [
                f"- {p.name}: " + " / ".join(h)
                for p in manifest.harness_params
                if p.visible
                and plan_evidence_missing(out.hidden_plan, p.name)
                and (
                    h := timed_hints(p.name, spec.visible_texts)
                )  # 검사와 같은 후보 (TIMED_CANDIDATES)
            ]
            if check.spec_gaps and attempt < ATTEMPTS - 1
            else []
        )
        if check.ok or (check.spec_gaps and not hints):
            break  # 통과 — 또는 명세 결함이라 재요청해도 소용없음
        if attempt == 0:
            res.first_problems = list(check.problems)
        res.errors.append(
            NodeError(
                node=TASK,
                code="contract_invalid",
                message=f"자동 검증 실패 {len(check.problems)}건 — 재요청",
            )
        )
        lines = "\n".join(
            f"- {p}" for p in check.problems
        )  # 직전 답의 문제만 (앞 회차 문제는 이미 고쳤을 수 있다)
        if hints:
            lines += (
                "\n- 운영 수치 근거를 '없음'으로 썼지만 자료에 그 단위의 수가 있다 — 이 중 근거가 되는 문장을 그대로 인용하고 "
                "evidence_value 에 그 안의 값 글자를, 값은 조절값 단위로 환산·반올림해 쓴다. 정말 근거가 아니면 '없음':\n"
                + "\n".join(hints)
            )
        note = "\n".join(
            x for x in (base_note, f"이전 답의 문제 (고쳐서 다시 쓰세요):\n{lines}") if x
        )
    if best is None:
        res.flags.append(FLAG_FAILED)
        return res
    out, res.problems, res.spec_gaps, res.adjustments, review = best
    res.contract, res.hidden_plan = out.contract, out.hidden_plan
    res.raw = best_raw
    res.flags += review
    res.flags += [
        n for n in res.adjustments if n.startswith(EVIDENCE_FRAGMENT)
    ]  # 일부만 일치한 근거 — 판정과 상관없이 승인자에게
    if res.spec_gaps:
        res.flags.append(FLAG_SPEC_GAP + "; ".join(res.spec_gaps))
    if res.problems:
        res.flags.append(FLAG_INVALID + "; ".join(res.problems))
    return res
