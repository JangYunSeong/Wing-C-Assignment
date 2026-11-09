"""공개 계약 생성 `plan_contract` (docs/design/spec-to-code.md §2 ②·§3) — LLM 1회 → 결정적 검사 → 1회 재요청.

입력은 승인된 기능형 공통 과제(TaskItem)와 템플릿뿐이다 — 공고·온톨로지·재료에서 나온 글, 지원서 없음 (불변식 3).
- 공개 운영 값은 LLM 이 값 + 근거 인용(hidden_plan.evidence)을 내고 코드가 자료와 대조한다. 근거가 "없음"인 필수 값은 명세 결함 — 재요청 없이 실패.
- 계약 검사(`starter.contract.check_contract`)의 `problems` 는 재요청 사유로 붙이고, `spec_gaps` 는 재요청 없이 실패로 둔다.
- 재요청 뒤에도 문제가 남으면 가장 문제가 적은 답을 돌려주고 표시를 남긴다 — 빌드 그래프가 `failed` 로 승인 화면에 올린다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.nodes.starter.generate import reason_line
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    FeatureSeed,
    HiddenPlan,
    NodeError,
    PlanContractInputV3,
    PlanContractOutput,
    Provenance,
    TaskContract,
    TaskItem,
)
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.contract import ROW_NEEDS, SpecView, check_contract, fit_covers
from task_spec.starter.seed_rows import RESERVED_ROWS, fit_seed
from task_spec.starter.state_rule import fit_state
from task_spec.starter.templates import Template

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


def plan_input(
    task: TaskItem, template: Template, note: str | None = None, seed: FeatureSeed | None = None
) -> PlanContractInputV3:
    m = template.manifest
    rx = m.naming
    params = "\n".join(
        f"- {p.name} ({p.kind}{', 필수' if p.required else ', 선택'}"
        f"{', 지원자에게 보임 — 자료·답에 적힌 값 그대로' if p.visible else ''}"
        f"{_range(p.minimum, p.maximum)}) — {p.description}"
        for p in m.harness_params
    )
    # 재료가 이 숨은 요구의 사유를 선언했으면 그것만 보인다 (FeatureSeed.reasons)
    allowed = set(seed.reasons) if seed is not None and seed.reasons else None
    reasons = "\n".join(
        reason_line(r) for r in m.failure_reasons if allowed is None or r.code in allowed
    )
    return PlanContractInputV3(
        template=(
            f"{m.ref} — 인터페이스: {', '.join(m.interfaces)}\n"
            f"이름 규칙: 경로 {rx.path} · 헤더 {rx.header} · 엔티티 {rx.entity} · 필드 {rx.field}"
            + (f" · 화면 test id {rx.test_id}" if "ui" in m.interfaces else "")
        ),
        harness_params=params or NONE,
        failure_reasons=reasons or NONE,
        title=task.title or NONE,
        scenario=task.scenario or NONE,
        requirements="\n".join(f"{r.req_id} {r.text}" for r in task.requirements) or NONE,
        acceptance_public="\n".join(f"{a.ac_id} {a.text}" for a in task.acceptance_public) or NONE,
        deliverables="\n".join(f"- {d}" for d in task.deliverables) or NONE,
        traps="\n".join(f"{t.trap_id} {t.description}" for t in task.traps) or NONE,
        acceptance_hidden="\n".join(
            f"{h.ac_id} ({h.trap_id}) {h.text}" for h in task.acceptance_hidden
        )
        or NONE,
        materials="\n\n".join(f"[{a.artifact_id}] {a.title}\n{a.content}" for a in task.artifacts)
        or NONE,
        clarifications="\n".join(
            f"[{c.clarification_id}] {c.topic}: {c.answer}" for c in task.clarifications
        )
        or NONE,
        revision_note=note or NONE,
        seed_rules=seed_rules_text(m)
        + needs_text(seed, m.seed_rows),  # 프론트 종류는 행 시드가 없는 템플릿에도
    )


async def plan_contract(
    task: TaskItem,
    template: Template,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    seed: FeatureSeed | None = None,
    reviewer_note: str | None = None,
) -> PlanResult:
    """승인된 기능형 명세 → 공개 계약 + 숨은 테스트 계획."""
    res = PlanResult()
    spec = SpecView.from_task(task)
    manifest = template.manifest
    # 2026-11-07: 템플릿 표기 패턴으로 못 찾으면 LLM 전에 실패시켰는데, 패턴이 한 예시 표현에만 맞아 값이 있는 명세도 막혔다
    # → LLM 이 값 + 근거 인용을 내고 코드가 확인한다 (check_contract). 근거가 "없음"이면 그때 명세 결함.

    prompt_t = prompts.get(TASK)
    base_note = f"승인자 수정 요청: {reviewer_note}" if reviewer_note else None
    note = base_note
    best: tuple[PlanContractOutput, list[str], list[str], list[str], list[str]] | None = None
    for attempt in range(ATTEMPTS):
        prompt = assemble(
            prompt_t,
            for_prompt(plan_input(task, template, note, seed), prompt_t.input_model),
            ontology_version=ontology_version,
        )
        try:
            r = await llm.call(prompt, prompt_t.output_model)  # type: ignore[arg-type]
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(node=TASK, code=type(exc).__name__, message="공개 계약 생성 실패")
            )
            res.fail_reasons.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            if (spent := getattr(exc, "provenance", None)) is not None:
                res.provenance.append(spent)  # 실패한 호출도 비용·호출 수에
            continue
        res.provenance.append(r.provenance)
        out: PlanContractOutput = r.parsed  # type: ignore[assignment]
        filled, notes = fill_hidden_defaults(out.hidden_plan, manifest)
        out = out.model_copy(update={"hidden_plan": filled})
        if manifest.seed_rows:  # 행 수 맞추기는 DB 행 과제에서만
            fitted, fitted_plan, seed_notes = fit_seed(out.contract, out.hidden_plan, manifest)
            out = out.model_copy(update={"contract": fitted, "hidden_plan": fitted_plan})
            notes += seed_notes
        stated, state_notes = fit_state(out.contract)
        out = out.model_copy(update={"contract": stated})
        notes += state_notes
        covered, cover_notes = fit_covers(out.contract, spec)
        out = out.model_copy(update={"contract": covered})
        notes += cover_notes
        check = check_contract(out.contract, out.hidden_plan, spec, manifest, seed)
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
        if check.ok or check.spec_gaps:
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
        note = "\n".join(
            x for x in (base_note, f"이전 답의 문제 (고쳐서 다시 쓰세요):\n{lines}") if x
        )
    if best is None:
        res.flags.append(FLAG_FAILED)
        return res
    out, res.problems, res.spec_gaps, res.adjustments, review = best
    res.contract, res.hidden_plan = out.contract, out.hidden_plan
    res.flags += review
    if res.spec_gaps:
        res.flags.append(FLAG_SPEC_GAP + "; ".join(res.spec_gaps))
    if res.problems:
        res.flags.append(FLAG_INVALID + "; ".join(res.problems))
    return res
