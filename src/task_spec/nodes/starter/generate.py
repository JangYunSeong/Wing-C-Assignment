"""시작 코드 생성기 4종 (docs/design/spec-to-code.md §4) — 생성 → 그 오버레이 정적 검사 → 1회 재요청.

| 생성기 | 보는 것 | 보지 않는 것 | 산출 kind |
|---|---|---|---|
| gen_starter | 계약, 명세 공개 부분, 템플릿 뼈대·쓸 수 있는 경로, 이미 있는 API | HiddenPlan, 숨은 요구, 허용 설계 | starter |
| gen_hidden | 계약, HiddenPlan, 숨은 요구·숨김 수용 기준, 하네스 API | 시작 코드, 참조 구현 | hidden |
| gen_naive | 계약, 시작 코드, 재료의 흔한 구현(+하나만 놓친 판의 지킬 것) | 숨은 테스트, 허용 설계 | ref_naive |
| gen_accepted | 계약, 시작 코드, 허용 설계 1건 + 스택 조건 | 숨은 테스트, 다른 허용 설계 | ref_accepted |

생성기끼리는 서로의 산출물을 보지 않는다 (참조 구현만 시작 코드 위에 쓴다). 정적 검사는 starter/static_check.py — 그 오버레이 하나만 넣어
그 생성기에 배정된 문제를 재요청 사유로 붙인다. 숨은 테스트에는 조절값 파일을 코드가 붙인다 (render_params).
입력은 공고·명세·재료·템플릿뿐, 지원서 없음 (불변식 3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    AcceptedDesign,
    FeatureSeed,
    GeneratedFile,
    GeneratedFiles,
    GenHiddenInput,
    GenRefInput,
    GenStarterInput,
    HiddenPlan,
    NodeError,
    Provenance,
    TaskContract,
    TaskItem,
)
from task_spec.schemas.build import FileKind
from task_spec.schemas.template import SlotKind, TemplateManifest
from task_spec.starter.contract import SpecView, mentioned_apis
from task_spec.starter.render import contract_markdown, public_scenario, render_params
from task_spec.starter.seed_rows import RowRange, allocate_rows
from task_spec.starter.static_check import KIND_TARGET, Overlay, static_check
from task_spec.starter.templates import Template

NONE = "(없음)"
FLAG_FAILED = "시작 코드 생성 실패 (LLM 응답 없음·형식 오류): "
FLAG_INVALID = "시작 코드 정적 검사 실패 (재요청 후에도): "
_SKIP = (".jar", "gradlew", "gradlew.bat")
_MAX_FILE_CHARS = 4000

logger = logging.getLogger(__name__)


@dataclass
class GenResult:
    task: str
    overlay: Overlay | None = None
    problems: list[str] = field(default_factory=list)  # 채택한 답에 남은 정적 검사 문제
    first_problems: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)
    errors: list[NodeError] = field(default_factory=list)
    fail_reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.overlay is not None and not self.problems


# ── 입력 글 ────────────────────────────────────────────────


def stack_text(m: TemplateManifest) -> str:
    s = m.stack
    head = f"템플릿 {m.ref} — 인터페이스 {', '.join(m.interfaces)}"
    if s is None:
        return head
    return (
        f"{head}\n언어 {s.language} {s.language_version} · {s.framework} {s.framework_version} · "
        f"빌드 {s.build_tool} {s.build_tool_version} (새 라이브러리 추가 금지)"
    )


def template_files_text(t: Template) -> str:
    out = []
    for rel in t.files("project"):
        if rel.endswith(_SKIP) or rel.split("/")[-1] in _SKIP:
            continue
        lock = " (잠김)" if rel in t.manifest.locked else ""
        body = (t.project / rel).read_text("utf-8", errors="ignore")[:_MAX_FILE_CHARS]
        out.append(f"=== {rel}{lock} ===\n{body.rstrip()}")
    return "\n\n".join(out) or NONE


def slots_text(m: TemplateManifest, slot: SlotKind) -> str:
    lines = ["쓸 수 있는 경로:"] + [f"- {g}" for g in m.fill_slots.get(slot, [])]
    lines += ["바꾸지 않는 파일(잠김):"] + [f"- {p}" for p in m.locked]
    if slot != "hidden":
        lines += ["쓰지 않는 곳(채점 자리):"] + [f"- {p}" for p in m.reserved]
    elif m.hidden_layout is not None:
        lines.append(f"조절값 파일({m.hidden_layout.params})은 쓰지 않는다 — 따로 만들어진다")
    return "\n".join(lines)


def files_text(files: Sequence[GeneratedFile]) -> str:
    return "\n\n".join(f"=== {f.path} ===\n{f.content.rstrip()}" for f in files) or NONE


def _range_text(r: RowRange, names: dict[str, str]) -> str:
    initial = ", ".join(f"{k}={v}" for k, v in r.initial.items())
    extra = [
        f"{r.rows}행",
        *([f"처음 {initial}"] if initial else []),
        *(["버스트 — 요청마다 다른 id"] if r.burst else []),
    ]
    return f"  데이터: {names.get(r.entity_id, r.entity_id)} id {r.first}~{r.last} ({', '.join(extra)}) — 이 범위만 쓴다 (시나리오에 적힌 id 보다 우선)"


def plan_text(plan: HiddenPlan, contract: TaskContract | None = None) -> str:
    """HiddenPlan → 숨은 테스트 생성기 입력. 계약을 주면 케이스마다 코드가 나눈 시드 id 범위를 붙인다 (seed_rows)."""
    alloc = allocate_rows(contract, plan) if contract is not None else None
    names = {m.entity_id: m.name for m in contract.entities} if contract is not None else {}
    lines = ["조절값 (GraderParams 로 읽는 이름 = 값):"]
    lines += [f"- {k} = {v}" for k, v in plan.harness.items()] or ["- (없음)"]
    lines.append("케이스:")
    for c in plan.cases:
        lines.append(
            f"- {c.case_id} ({c.ac_id}, 숨은 요구 {c.trap_id}, 실패 사유 {c.intended_reason}): {c.scenario} → 통과 조건: {c.expect}"
        )
        if alloc is not None:
            lines += [_range_text(r, names) for r in alloc.for_case(c.case_id)]
    return "\n".join(lines)


def existing_apis(task: TaskItem, contract: TaskContract) -> list[str]:
    """명세 공개 글에 나온 API 중 계약에 있는 것 = 이미 있는 기능 (시작 코드가 완전히 동작하게 만든다)."""
    named = set(mentioned_apis(SpecView.from_task(task).public_texts))
    out = []
    for e in contract.endpoints:
        key = mentioned_apis([f"{e.method} {e.path}"]) if e.method else []
        if key and key[0] in named:
            out.append(f"{e.method} {e.path} — {e.summary}")
    return out


def _lines(items: Sequence[str]) -> str:
    return "\n".join(items) or NONE


def starter_input(
    task: TaskItem, contract: TaskContract, template: Template, note: str | None = None
) -> GenStarterInput:
    m = template.manifest
    return GenStarterInput(
        stack=stack_text(m),
        template_files=template_files_text(template),
        slots=slots_text(m, "starter"),
        existing_apis=_lines(existing_apis(task, contract)),
        title=task.title or NONE,
        scenario=public_scenario(task) or NONE,
        requirements=_lines([f"{r.req_id} {r.text}" for r in task.requirements]),
        acceptance_public=_lines([f"{a.ac_id} {a.text}" for a in task.acceptance_public]),
        contract=contract_markdown(contract, tables=template.manifest.naming.table),
        revision_note=note or NONE,
    )


def hidden_input(
    task: TaskItem,
    contract: TaskContract,
    plan: HiddenPlan,
    template: Template,
    note: str | None = None,
) -> GenHiddenInput:
    m = template.manifest
    harness = template.hidden / "HARNESS.md"
    return GenHiddenInput(
        stack=stack_text(m),
        harness_api=harness.read_text("utf-8") if harness.is_file() else NONE,
        slots=slots_text(m, "hidden"),
        contract=contract_markdown(contract, tables=template.manifest.naming.table),
        hidden_plan=plan_text(plan, contract),
        traps=_lines([f"{t.trap_id} {t.description}" for t in task.traps]),
        acceptance_hidden=_lines(
            [f"{h.ac_id} ({h.trap_id}) {h.text}" for h in task.acceptance_hidden]
        ),
        failure_reasons=_lines([f"- {r.code} — {r.description}" for r in m.failure_reasons]),
        revision_note=note or NONE,
    )


def _ref_input(
    task: TaskItem,
    contract: TaskContract,
    template: Template,
    starter: Overlay,
    goal: str,
    conditions: str,
    note: str | None,
) -> GenRefInput:
    m = template.manifest
    return GenRefInput(
        stack=stack_text(m),
        slots=slots_text(m, "ref"),
        contract=contract_markdown(contract, tables=template.manifest.naming.table),
        starter_files=files_text(starter.files),
        requirements=_lines([f"{r.req_id} {r.text}" for r in task.requirements]),
        acceptance_public=_lines([f"{a.ac_id} {a.text}" for a in task.acceptance_public]),
        goal=goal,
        conditions=conditions or NONE,
        revision_note=note or NONE,
    )


def naive_input(
    task: TaskItem,
    contract: TaskContract,
    template: Template,
    starter: Overlay,
    seed: FeatureSeed,
    keep_traps: Sequence[str] = (),
    note: str | None = None,
) -> GenRefInput:
    """흔한 구현 — 재료의 naive_impl 만 (깨지는 이유는 주지 않는다: 알면 피해 간다). keep_traps = 하나만 놓친 판에서 지킬 숨은 요구."""
    keep = [t.description for t in task.traps if t.trap_id in keep_traps]
    return _ref_input(task, contract, template, starter, seed.naive_impl, _lines(keep), note)


def accepted_input(
    task: TaskItem,
    contract: TaskContract,
    template: Template,
    starter: Overlay,
    design: AcceptedDesign,
    note: str | None = None,
) -> GenRefInput:
    """허용 구현 — 허용 설계 1건 + 그 설계가 다루는 숨은 요구·숨김 수용 기준 + 템플릿 스택 조건."""
    traps = [t for t in task.traps if t.trap_id in design.trap_ids]
    hidden = [h for h in task.acceptance_hidden if h.trap_id in design.trap_ids]
    goal = "\n".join(
        [
            f"허용 설계 {design.design_id}: {design.summary}",
            f"조건·함정: {design.conditions}",
            *(f"숨은 요구 {t.trap_id}: {t.description}" for t in traps),
            *(f"숨김 수용 기준 {h.ac_id}: {h.text}" for h in hidden),
        ]
    )
    conditions = _lines([f"- {c}" for c in template.manifest.stack_conditions])
    return _ref_input(task, contract, template, starter, goal, conditions, note)


# ── 생성 루프 ─────────────────────────────────────────────


async def _generate(
    task_name: str,
    make_input: Callable[[str | None], Any],
    make_overlay: Callable[[list[GeneratedFile]], Overlay],
    check: Callable[[Overlay], list[str]],
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    note: str | None = None,
) -> GenResult:
    res = GenResult(task=task_name)
    prompt_t = prompts.get(task_name)
    best: tuple[Overlay, list[str]] | None = None
    for attempt in range(2):
        prompt = assemble(prompt_t, make_input(note), ontology_version=ontology_version)
        try:
            r = await llm.call(prompt, prompt_t.output_model)  # type: ignore[arg-type]
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(node=task_name, code=type(exc).__name__, message="시작 코드 생성 실패")
            )
            res.fail_reasons.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            continue
        res.provenance.append(r.provenance)
        out: GeneratedFiles = r.parsed  # type: ignore[assignment]
        overlay = make_overlay(list(out.files))
        problems = check(overlay)
        logger.info(
            "%s attempt=%d files=%d problems=%d latency_ms=%d",
            task_name,
            attempt + 1,
            len(overlay.files),
            len(problems),
            r.provenance.latency_ms,
        )
        if best is None or len(problems) < len(best[1]):
            best = (overlay, problems)
        if not problems:
            break
        if attempt == 0:
            res.first_problems = list(problems)
            res.errors.append(
                NodeError(
                    node=task_name,
                    code="static_invalid",
                    message=f"정적 검사 실패 {len(problems)}건 — 재요청",
                )
            )
            lines = "\n".join(f"- {p}" for p in problems)
            note = "\n".join(
                x for x in (note, f"이전 답의 문제 (고쳐서 다시 쓰세요):\n{lines}") if x
            )
    if best is None:
        res.flags.append(FLAG_FAILED + task_name)
        return res
    res.overlay, res.problems = best
    if res.problems:
        res.flags.append(FLAG_INVALID + f"{task_name}: " + "; ".join(res.problems))
    return res


@dataclass(frozen=True)
class BuildInputs:
    """생성기들이 함께 쓰는 입력 — 승인된 명세·계약·숨은 테스트 계획·템플릿·재료."""

    task: TaskItem
    contract: TaskContract
    plan: HiddenPlan
    template: Template
    seed: FeatureSeed | None = None
    avoid_terms: tuple[str, ...] = ()

    def check(self, overlay: Overlay) -> list[str]:
        res = static_check(
            [overlay],
            template=self.template,
            plan=self.plan,
            spec=SpecView.from_task(self.task),
            contract=self.contract,
            seed=self.seed,
            avoid_terms=self.avoid_terms,
        )
        return res.for_target(KIND_TARGET[overlay.kind])


def _overlay(kind: FileKind, label: str = "") -> Callable[[list[GeneratedFile]], Overlay]:
    return lambda files: Overlay(kind=kind, files=tuple(files), label=label)


async def gen_starter(
    b: BuildInputs,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    note: str | None = None,
) -> GenResult:
    return await _generate(
        "gen_starter",
        lambda n: starter_input(b.task, b.contract, b.template, n),
        _overlay("starter"),
        b.check,
        llm=llm,
        prompts=prompts,
        ontology_version=ontology_version,
        note=note,
    )


async def gen_hidden(
    b: BuildInputs,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    note: str | None = None,
) -> GenResult:
    params = render_params(b.plan, b.template.manifest)

    def with_params(files: list[GeneratedFile]) -> Overlay:
        kept = [f for f in files if f.path != params.path]  # 조절값 파일은 코드가 쓴다
        return Overlay(kind="hidden", files=(*kept, params))

    return await _generate(
        "gen_hidden",
        lambda n: hidden_input(b.task, b.contract, b.plan, b.template, n),
        with_params,
        b.check,
        llm=llm,
        prompts=prompts,
        ontology_version=ontology_version,
        note=note,
    )


async def gen_naive(
    b: BuildInputs,
    starter: Overlay,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    keep_traps: Sequence[str] = (),
    note: str | None = None,
) -> GenResult:
    if b.seed is None:
        raise ValueError("gen_naive 는 재료(feature seed)의 흔한 구현 설명이 필요하다")
    seed = b.seed
    label = (
        "naive-all"
        if not keep_traps
        else "naive-miss-"
        + "-".join(t.trap_id for t in b.task.traps if t.trap_id not in keep_traps)
    )
    return await _generate(
        "gen_naive",
        lambda n: naive_input(b.task, b.contract, b.template, starter, seed, keep_traps, n),
        _overlay("ref_naive", label),
        b.check,
        llm=llm,
        prompts=prompts,
        ontology_version=ontology_version,
        note=note,
    )


async def gen_accepted(
    b: BuildInputs,
    starter: Overlay,
    design: AcceptedDesign,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    note: str | None = None,
) -> GenResult:
    return await _generate(
        "gen_accepted",
        lambda n: accepted_input(b.task, b.contract, b.template, starter, design, n),
        _overlay("ref_accepted", design.design_id),
        b.check,
        llm=llm,
        prompts=prompts,
        ontology_version=ontology_version,
        note=note,
    )
