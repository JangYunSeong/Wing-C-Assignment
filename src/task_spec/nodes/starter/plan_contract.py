"""공개 계약 생성 `plan_contract` (docs/design/spec-to-code.md §2 ②·§3) — LLM 1회 → 결정적 검사 → 1회 재요청.

입력은 승인된 기능형 공통 과제(TaskItem)와 템플릿뿐이다 — 공고·온톨로지·재료에서 나온 글, 지원서 없음 (불변식 3).
- 명세에 필수 운영 수치가 없으면(`spec_missing_params`) LLM 을 부르지 않고 바로 실패한다 — 재요청으로 못 고치는 명세 결함이다.
- 계약 검사(`starter.contract.check_contract`)의 `problems` 는 재요청 사유로 붙이고, `spec_gaps` 는 재요청 없이 실패로 둔다.
- 재요청 뒤에도 문제가 남으면 가장 문제가 적은 답을 돌려주고 표시를 남긴다 — 빌드 그래프가 `failed` 로 승인 화면에 올린다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
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
from task_spec.starter.contract import SpecView, check_contract, spec_missing_params
from task_spec.starter.seed_rows import RESERVED_ROWS, fit_seed
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


def seed_rules_text(m: TemplateManifest) -> str:
    """시드 행 수 규칙 — 검사(seed_rows)와 같은 값을 프롬프트에 그대로."""
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


def plan_input(task: TaskItem, template: Template, note: str | None = None) -> PlanContractInputV3:
    m = template.manifest
    rx = m.naming
    params = "\n".join(
        f"- {p.name} ({p.kind}{', 필수' if p.required else ', 선택'}"
        f"{', 지원자에게 보임 — 자료·답에 적힌 값 그대로' if p.visible else ''}"
        f"{_range(p.minimum, p.maximum)}) — {p.description}"
        for p in m.harness_params
    )
    reasons = "\n".join(f"- {r.code} — {r.description}" for r in m.failure_reasons)
    return PlanContractInputV3(
        template=(
            f"{m.ref} — 인터페이스: {', '.join(m.interfaces)}\n"
            f"이름 규칙: 경로 {rx.path} · 헤더 {rx.header} · 엔티티 {rx.entity} · 필드 {rx.field}"
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
        seed_rules=seed_rules_text(m),
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
    missing = spec_missing_params(spec.visible_texts, manifest)
    if missing:
        res.spec_gaps = [f"필수 운영 수치 {name}" for name in missing]
        res.flags.append(FLAG_SPEC_GAP + ", ".join(missing))
        return res

    prompt_t = prompts.get(TASK)
    note = f"승인자 수정 요청: {reviewer_note}" if reviewer_note else None
    best: tuple[PlanContractOutput, list[str], list[str], list[str]] | None = None
    for attempt in range(2):
        prompt = assemble(
            prompt_t,
            for_prompt(plan_input(task, template, note), prompt_t.input_model),
            ontology_version=ontology_version,
        )
        try:
            r = await llm.call(prompt, prompt_t.output_model)  # type: ignore[arg-type]
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(node=TASK, code=type(exc).__name__, message="공개 계약 생성 실패")
            )
            res.fail_reasons.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            continue
        res.provenance.append(r.provenance)
        out: PlanContractOutput = r.parsed  # type: ignore[assignment]
        fitted, fitted_plan, notes = fit_seed(out.contract, out.hidden_plan, manifest)
        out = out.model_copy(update={"contract": fitted, "hidden_plan": fitted_plan})
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
            best = (out, check.problems, check.spec_gaps, notes)
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
            lines = "\n".join(f"- {p}" for p in check.problems)
            note = "\n".join(
                x for x in (note, f"이전 답의 문제 (고쳐서 다시 쓰세요):\n{lines}") if x
            )
    if best is None:
        res.flags.append(FLAG_FAILED)
        return res
    out, res.problems, res.spec_gaps, res.adjustments = best
    res.contract, res.hidden_plan = out.contract, out.hidden_plan
    if res.spec_gaps:
        res.flags.append(FLAG_SPEC_GAP + "; ".join(res.spec_gaps))
    if res.problems:
        res.flags.append(FLAG_INVALID + "; ".join(res.problems))
    return res
