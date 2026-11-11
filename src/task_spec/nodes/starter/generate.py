"""시작 코드 생성기 4종 (docs/design/spec-to-code.md §4) — 생성 → 그 오버레이 정적 검사 → 1회 재요청.
숨은 테스트는 케이스마다 따로 부르고(gen_hidden), 수리 때는 사유가 가리키는 케이스만 다시 만든다.

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

import asyncio
import logging
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    AcceptedDesign,
    FeatureSeed,
    GeneratedFile,
    GeneratedFiles,
    GenHiddenInput,
    GenHiddenInputV3,
    GenPublicInput,
    GenRefInput,
    GenRefInputV3,
    GenStarterInput,
    GenStarterInputV3,
    HiddenPlan,
    NodeError,
    Provenance,
    TaskContract,
    TaskItem,
)
from task_spec.schemas.build import FileKind
from task_spec.schemas.template import FailureReason, HiddenHelper, SlotKind, TemplateManifest
from task_spec.starter.config_merge import merge_config
from task_spec.starter.contract import SpecView, existing_endpoints
from task_spec.starter.hidden_helpers import render_contract_case, render_persistence_check
from task_spec.starter.init_scripts import ensure_statements
from task_spec.starter.java_imports import ensure_java_imports
from task_spec.starter.render import contract_markdown, public_scenario, render_params
from task_spec.starter.seed_rows import RowRange, allocate_rows
from task_spec.starter.seed_sql import render_seed_sql
from task_spec.starter.static_check import KIND_TARGET, Overlay, static_check
from task_spec.starter.templates import Template, is_java
from task_spec.starter.ts_helpers import ensure_vitest_imports, render_app_types

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
    adjustments: list[str] = field(
        default_factory=list
    )  # 코드가 고친 것 (starter/init_scripts.py) — 승인 화면에
    # 숨은 테스트 수리에서 재요청 뒤에도 문제가 남아 이전 파일을 그대로 둔 케이스 → 그 문제 (승인 화면·수리 기록 kept-previous)
    kept: dict[str, list[str]] = field(default_factory=dict)
    # 채택하지 않은(문제가 남은) 생성 출력 — 산출물로만 남긴다 (git 밖, 원인 확인용)
    rejected: list[Overlay] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.overlay is not None and not self.problems


# ── 입력 글 ────────────────────────────────────────────────


def stack_text(m: TemplateManifest) -> str:
    s = m.stack
    head = f"템플릿 {m.ref} — 인터페이스 {', '.join(m.interfaces)}"
    if s is None:
        return head
    text = (
        f"{head}\n언어 {s.language} {s.language_version} · {s.framework} {s.framework_version} · "
        f"빌드 {s.build_tool} {s.build_tool_version} (새 라이브러리 추가 금지)"
    )
    if (
        m.seed_sql is not None and m.seed_sql.id_column
    ):  # 시드가 id 를 직접 넣는 테이블의 id 열 꼴 (스택 문법 그대로)
        text += f"\n시드 테이블 id 열: `{m.seed_sql.id_column}` (시드가 id 를 직접 넣는다 — 이 꼴 그대로)"
    if m.stack_notes:  # 이 스택의 API 사실 (manifest stack_notes — 함정과 무관한 쓰는 법)
        text += "\n이 스택에서 쓰는 법:\n" + "\n".join(f"- {n}" for n in m.stack_notes)
    return text


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
        *(
            ["버스트 — Burst.rounds(동시 요청 수, 버스트 수, …) 로 차례로, 요청마다 다른 id"]
            if r.burst
            else []
        ),
    ]
    return f"  데이터: {names.get(r.entity_id, r.entity_id)} id {r.first}~{r.last} ({', '.join(extra)}) — 이 범위만 쓴다 (시나리오에 적힌 id 보다 우선)"


def reason_line(r: FailureReason) -> str:
    """실패 사유 한 줄 — 일으키는 법(manifest how)이 있으면 함께 (계약·숨은 테스트 생성기 입력)."""
    return f"- {r.code} — {r.description}" + (f" — 일으키는 법: {r.how}" if r.how else "")


def plan_text(
    plan: HiddenPlan,
    contract: TaskContract | None = None,
    how: Mapping[str, str] | None = None,
    only: Collection[str] | None = None,
) -> str:
    """HiddenPlan → 숨은 테스트 생성기 입력. 계약을 주면 케이스마다 코드가 나눈 시드 id 범위를 붙인다 (seed_rows).
    how(사유 → 일으키는 법)를 주면 케이스마다 그 사유를 일으키는 법을 붙인다 — 시나리오가 사유와 어긋나도(예: 롤백 사유인데
    '허용되지 않은 전이') 사유 쪽을 따르게 (2026-11-08 상류 평가: 롤백 사유 케이스 9개 중 8개)."""
    alloc = allocate_rows(contract, plan) if contract is not None else None
    names = {m.entity_id: m.name for m in contract.entities} if contract is not None else {}
    lines = ["조절값 (GraderParams 로 읽는 이름 = 값):"]
    lines += [f"- {k} = {v}" for k, v in plan.harness.items()] or ["- (없음)"]
    lines.append("케이스:")
    for c in plan.cases:
        if (
            only is not None and c.case_id not in only
        ):  # 케이스별 생성 — 행 배정은 계획 전체로 (도우미 상수와 같은 값)
            continue
        lines.append(
            f"- {c.case_id} ({c.ac_id}, 숨은 요구 {c.trap_id}, 실패 사유 {c.intended_reason}): {c.scenario} → 통과 조건: {c.expect}"
        )
        if how and c.intended_reason in how:
            lines.append(
                f"  일으키는 법 ({c.intended_reason} — 시나리오보다 우선): {how[c.intended_reason]}"
            )
        if alloc is not None:
            lines += [_range_text(r, names) for r in alloc.for_case(c.case_id)]
        lines += [f"  준비 (하네스가 입력 환경을 바꿈): {step}" for step in c.setup]
    return "\n".join(lines)


def existing_apis(task: TaskItem, contract: TaskContract) -> list[str]:
    """이미 있는 기능 (시작 코드가 완전히 동작하게 만든다) — 판정은 contract.existing_endpoints (계약 표시 + 명세에 나온 경로).
    2026-11-07 알파 테스트: 명세가 "거래 상태 변경 API" 라고만 써 목록이 비자 시작 코드가 범위를 짐작했다 → 계약 표시를 먼저."""
    return [
        f"{e.method or ''} {e.path} — {e.summary}".strip()
        for e in existing_endpoints(contract, SpecView.from_task(task))
    ]


def _lines(items: Sequence[str]) -> str:
    return "\n".join(items) or NONE


def helper_block(
    m: TemplateManifest, helper: GeneratedFile, rule: HiddenHelper | None, what: str
) -> str:
    """하네스 문서 끝에 붙이는 이 과제의 도우미 소스 — Java 는 클래스 이름·```java, TypeScript 는 파일 이름·```ts."""
    if is_java(m):
        name, lang = (rule.class_name if rule else ""), "java"
    else:  # TypeScript contract.ts · Python contract.py
        name = PurePosixPath(helper.path).name
        lang = "python" if helper.path.endswith(".py") else "ts"
    return (
        f"\n\n## {name} — 이 과제의 {what} "
        f"(`{helper.path}`, 빌드 파이프라인이 만든다 — 쓰지 않는다)\n\n```{lang}\n{helper.content}```\n"
    )


def test_import_fix(
    m: TemplateManifest,
) -> Callable[[list[GeneratedFile]], tuple[list[GeneratedFile], list[str]]] | None:
    """숨은·공개 테스트의 빠진 import 를 코드가 채운다 — TypeScript 는 vitest, Java 는 JDK 표준 이름 (starter/java_imports)."""
    return ensure_java_imports if is_java(m) else ensure_vitest_imports


def public_text(public: Overlay | None) -> str:
    """생성기 입력의 공개 테스트 — 테스트 먼저(TDD): 시작 코드·참조 구현이 이것을 보고 맞춘다 (숨은 테스트는 보지 않음)."""
    return files_text(public.files) if public is not None else NONE


def starter_input(
    task: TaskItem,
    contract: TaskContract,
    template: Template,
    note: str | None = None,
    public: Overlay | None = None,
) -> GenStarterInput:
    m = template.manifest
    return GenStarterInputV3(
        stack=stack_text(m),
        template_files=_with_app_types(template_files_text(template), contract, template),
        slots=slots_text(m, "starter"),
        existing_apis=_lines(existing_apis(task, contract)),
        title=task.title or NONE,
        scenario=public_scenario(task) or NONE,
        requirements=_lines([f"{r.req_id} {r.text}" for r in task.requirements]),
        acceptance_public=_lines([f"{a.ac_id} {a.text}" for a in task.acceptance_public]),
        contract=contract_markdown(contract, tables=template.manifest.naming.table),
        revision_note=note or NONE,
        public_tests=public_text(public),
    )


def public_input(
    task: TaskItem,
    contract: TaskContract,
    plan: HiddenPlan,
    template: Template,
    note: str | None = None,
) -> GenPublicInput:
    """공개 테스트 생성 입력 — 공개 정보만 (숨은 요구·HiddenPlan 의 케이스·운영 조건 없음). plan 은 공개 몫 행을 숨은 범위와 겹치지 않게
    나누는 데만 쓴다 (PublicCase 상수 — 숨은 케이스 내용은 들어가지 않는다)."""
    m = template.manifest
    doc = template.hidden / "PUBLIC.md"
    api = doc.read_text("utf-8") if doc.is_file() else NONE
    helper = render_contract_case(contract, plan, m, audience="public")
    if helper is not None:
        api += helper_block(m, helper, m.public_helper, "공개 테스트 도우미")
    return GenPublicInput(
        stack=stack_text(m),
        harness_api=api,
        slots=slots_text(m, "public_test"),
        contract=contract_markdown(contract, tables=m.naming.table),
        public_tests=_lines([f"{t.test_id} ({t.ac_id}) {t.checks}" for t in contract.public_tests]),
        requirements=_lines([f"{r.req_id} {r.text}" for r in task.requirements]),
        acceptance_public=_lines([f"{a.ac_id} {a.text}" for a in task.acceptance_public]),
        revision_note=note or NONE,
    )


def hidden_input(
    task: TaskItem,
    contract: TaskContract,
    plan: HiddenPlan,
    template: Template,
    note: str | None = None,
    only: Collection[str] | None = None,
) -> GenHiddenInput:
    """only: 이 케이스들만 계획에 보인다 (케이스별 생성). 도우미·조절값은 계획 전체로 — 합친 숨은 테스트와 같은 상수."""
    m = template.manifest
    harness = template.hidden / "HARNESS.md"
    api = harness.read_text("utf-8") if harness.is_file() else NONE
    helper = render_contract_case(contract, plan, m)
    if (
        helper is not None
    ):  # 이 과제의 계약 도우미 — 숨은 케이스가 상속(Java)·import(TypeScript)한다 (starter/hidden_helpers.py)
        api += helper_block(m, helper, m.hidden_helper, "계약 도우미")
    return GenHiddenInputV3(
        stack=stack_text(m),
        harness_api=api,
        slots=slots_text(m, "hidden"),
        contract=contract_markdown(contract, tables=template.manifest.naming.table),
        hidden_plan=plan_text(
            plan, contract, {r.code: r.how for r in m.failure_reasons if r.how}, only
        ),
        traps=_lines([f"{t.trap_id} {t.description}" for t in task.traps]),
        acceptance_hidden=_lines(
            [f"{h.ac_id} ({h.trap_id}) {h.text}" for h in task.acceptance_hidden]
        ),
        failure_reasons=_lines([reason_line(r) for r in m.failure_reasons]),
        revision_note=note or NONE,
        accepted_designs=_lines(
            [f"{d.design_id}: {d.summary} — {d.conditions}" for d in task.accepted_designs]
        ),
    )


def config_files(template: Template, under: Overlay | None = None) -> dict[str, str]:
    """지금 적용되는 설정 파일 (config_merge 경로) — 템플릿 project/ 의 것, 그 위에 시작 코드가 쓴 것 (시작 코드 설정은 생성 직후
    이미 템플릿 위에 합쳐졌다). 설정 합치기의 바탕이자 참조 구현 입력에 보이는 설정."""
    rule = template.manifest.config_merge
    if rule is None:
        return {}
    out = {
        p: (template.project / p).read_text("utf-8")
        for p in rule.paths
        if (template.project / p).is_file()
    }
    if under is not None:
        out |= {f.path: f.content for f in under.files if f.path in rule.paths}
    return out


def _ref_input(
    task: TaskItem,
    contract: TaskContract,
    template: Template,
    starter: Overlay,
    goal: str,
    conditions: str,
    note: str | None,
    public: Overlay | None = None,
) -> GenRefInput:
    m = template.manifest
    return GenRefInputV3(
        stack=stack_text(m),
        slots=slots_text(m, "ref"),
        contract=contract_markdown(contract, tables=template.manifest.naming.table),
        # 시작 코드가 쓰지 않은 설정 파일도 지금 적용되는 내용으로 보인다 — 모르고 새로 쓰는 일을 줄인다 (합치기는 생성 직후 코드가 함)
        starter_files=files_text(
            [
                *starter.files,
                *(
                    GeneratedFile(path=p, content=c)
                    for p, c in config_files(template, starter).items()
                    if p not in {f.path for f in starter.files}
                ),
            ]
        ),
        requirements=_lines([f"{r.req_id} {r.text}" for r in task.requirements]),
        acceptance_public=_lines([f"{a.ac_id} {a.text}" for a in task.acceptance_public]),
        goal=goal,
        conditions=conditions or NONE,
        revision_note=note or NONE,
        public_tests=public_text(public),
    )


def naive_input(
    task: TaskItem,
    contract: TaskContract,
    template: Template,
    starter: Overlay,
    seed: FeatureSeed,
    keep_traps: Sequence[str] = (),
    note: str | None = None,
    public: Overlay | None = None,
) -> GenRefInput:
    """흔한 구현 — 재료의 naive_impl 만 (깨지는 이유는 주지 않는다: 알면 피해 간다). keep_traps = 하나만 놓친 판에서 지킬 숨은 요구."""
    keep = [t.description for t in task.traps if t.trap_id in keep_traps]
    goal = seed.naive_impl
    if (
        template.manifest.naive_conditions
    ):  # 설명한 구조가 이 스택에서 정말 그렇게 동작하게 (깨지는 이유는 아님)
        goal += "\n\n이 스택에서 위 구조를 설명대로 동작하게 쓰는 법:\n" + _lines(
            [f"- {c}" for c in template.manifest.naive_conditions]
        )
    return _ref_input(task, contract, template, starter, goal, _lines(keep), note, public)


def accepted_input(
    task: TaskItem,
    contract: TaskContract,
    template: Template,
    starter: Overlay,
    design: AcceptedDesign,
    note: str | None = None,
    public: Overlay | None = None,
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
    return _ref_input(task, contract, template, starter, goal, conditions, note, public)


# ── 생성 루프 ─────────────────────────────────────────────


def fit_input(inp: BaseModel, model: type[BaseModel]) -> BaseModel:
    """프롬프트 버전이 선언한 입력 모델로 줄인다 — 옛 버전은 previous_files 자리가 없다 (Schema 는 모르는 필드를 거부)."""
    return model.model_validate({k: v for k, v in dict(inp).items() if k in model.model_fields})


def merge_files(
    previous: Sequence[GeneratedFile], changed: Sequence[GeneratedFile]
) -> list[GeneratedFile]:
    """수리 답 합치기 — 이전 파일에 받은 파일만 덮어쓰고, 내용이 빈 파일은 지운다. 손대지 않은 파일은 그대로."""
    merged = {f.path: f for f in previous}
    for f in changed:
        if f.content.strip():
            merged[f.path] = f
        else:
            merged.pop(f.path, None)
    return list(merged.values())


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
    previous: Overlay | None = None,
    fix: Callable[[list[GeneratedFile]], tuple[list[GeneratedFile], list[str]]] | None = None,
    prompt_name: str | None = None,
) -> GenResult:
    """생성 → (fix: 코드가 고칠 수 있는 것은 고침) → 정적 검사 → 1회 재요청. 프롬프트가 previous_files 를 받으면(수리판) 이전 출력을 주고 고칠 파일만 받아 합친다 —
    밖에서 준 이전 출력(수리 루프)이든 이 함수 안의 첫 답(정적 검사 재요청)이든 같다."""
    res = GenResult(task=task_name)
    # 템플릿이 정한 프롬프트 (manifest prompt_tasks) — 없으면 생성기 이름 그대로
    prompt_t = prompts.get(prompt_name or task_name)
    repairable = "previous_files" in prompt_t.input_model.model_fields
    prev = previous if repairable else None
    best: tuple[Overlay, list[str], list[str]] | None = None
    for attempt in range(2):
        inp = make_input(note)
        if prev is not None:
            inp = inp.model_copy(update={"previous_files": files_text(prev.files)})
        prompt = assemble(
            prompt_t, fit_input(inp, prompt_t.input_model), ontology_version=ontology_version
        )
        try:
            r = await llm.call(  # 마지막 재시도는 추론 없이 — 출력은 검사·수리 루프가 다시 본다
                prompt,
                prompt_t.output_model,  # type: ignore[arg-type]
                reasoning_off_last=True,
            )
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(node=task_name, code=type(exc).__name__, message="시작 코드 생성 실패")
            )
            res.fail_reasons.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            if (spent := getattr(exc, "provenance", None)) is not None:
                res.provenance.append(spent)  # 실패한 호출도 비용·호출 수에
            continue
        res.provenance.append(r.provenance)
        out: GeneratedFiles = r.parsed  # type: ignore[assignment]
        files = merge_files(prev.files, out.files) if prev is not None else list(out.files)
        fixed: list[str] = []
        if fix is not None:
            files, fixed = fix(files)
        overlay = make_overlay(files)
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
            best = (overlay, problems, fixed)
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
            if repairable:
                prev = overlay  # 첫 답을 이전 출력으로 — 문제만 고치게
    if best is None:
        res.flags.append(FLAG_FAILED + task_name)
        return res
    res.overlay, res.problems, res.adjustments = best
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

    def check(
        self,
        overlay: Overlay,
        starter: Overlay | None = None,
        cases: Collection[str] | None = None,
    ) -> list[str]:
        """그 오버레이만 정적 검사. 참조 구현은 시작 코드를 같이 줘 시작 코드 설정 값과 비교한다 (하네스 설정 검사).
        cases: 숨은 테스트를 케이스별로 만들 때 그 케이스만 있는지 본다."""
        res = static_check(
            [overlay] if starter is None else [starter, overlay],
            template=self.template,
            plan=self.plan,
            spec=SpecView.from_task(self.task),
            contract=self.contract,
            seed=self.seed,
            avoid_terms=self.avoid_terms,
            hidden_cases_only=cases,
        )
        return res.for_target(KIND_TARGET[overlay.kind])

    def fix(
        self, files: list[GeneratedFile], starter: Overlay | None = None
    ) -> tuple[list[GeneratedFile], list[str]]:
        """참조 구현 출력에서 코드가 고칠 것 — 시드 파일은 빼고(시작 코드의 계약 시드를 씀), 설정 파일은 시작 코드 설정 위에
        합치고, 빈 시작 스크립트 보정."""
        return self._fixed(files, starter=False, base=starter)

    def fix_starter(self, files: list[GeneratedFile]) -> tuple[list[GeneratedFile], list[str]]:
        """시작 코드 출력에서 코드가 고칠 것 — 시드 파일을 계약 시드 렌더본으로 바꾸고, 설정 파일은 템플릿 설정 위에 합치고,
        빈 시작 스크립트 보정."""
        return self._fixed(files, starter=True)

    def _fixed(
        self, files: list[GeneratedFile], *, starter: bool, base: Overlay | None = None
    ) -> tuple[list[GeneratedFile], list[str]]:
        m = self.template.manifest
        files, notes = merge_config(files, m, config_files(self.template, base))
        rule = m.seed_sql
        if rule is not None:
            mine = next((f for f in files if f.path == rule.path), None)
            rest = [f for f in files if f.path != rule.path]
            if starter:
                schema = next((f.content for f in files if f.path == rule.schema_path), None)
                rendered = render_seed_sql(self.contract, m, schema)
                if rendered is not None:
                    if mine is None or mine.content != rendered.content:
                        notes.append(
                            f"{rule.path} 를 계약 시드로 코드가 씀 (생성기가 쓴 시드는 쓰지 않음)"
                        )
                    files = [*rest, rendered]
            elif mine is not None:
                notes.append(f"{rule.path} 를 뺌 — 시드는 시작 코드의 계약 시드를 쓴다")
                files = rest
        files, fixed = ensure_statements(files, m)
        # 기존 기능 테스트의 빠진 vitest import (starter/ts_helpers.ensure_vitest_imports)
        if not is_java(m):
            files, more = ensure_vitest_imports(files)
            fixed += more
        return files, notes + fixed


def _overlay(kind: FileKind, label: str = "") -> Callable[[list[GeneratedFile]], Overlay]:
    return lambda files: Overlay(kind=kind, files=tuple(files), label=label)


def _with_app_types(text: str, contract: TaskContract, template: Template) -> str:
    """시작 코드 입력의 템플릿 파일 목록에 앱 쪽 계약 타입(잠김, 코드가 씀)을 붙인다 — 그 파일을 import 해 응답을 읽게."""
    f = render_app_types(contract, template.manifest)
    if f is None:
        return text
    return f"{text}\n\n=== {f.path} (잠김 — 계약에서 코드가 만든 타입, 이것을 import 한다) ===\n{f.content.rstrip()}"


def _app_overlay(
    b: BuildInputs, kind: FileKind, label: str = ""
) -> Callable[[list[GeneratedFile]], Overlay]:
    """앱 쪽 계약 타입(src/api/contract-types.ts)은 코드가 쓴다 — 생성기가 같은 경로를 쓰면 버리고, 시작 코드에는 코드 판을 넣는다
    (참조 구현은 시작 코드 위에 놓이므로 시작 코드 판이 그대로 쓰인다)."""
    types = render_app_types(b.contract, b.template.manifest)
    if types is None:
        return _overlay(kind, label)

    def make(files: list[GeneratedFile]) -> Overlay:
        kept = [f for f in files if f.path != types.path]
        return Overlay(
            kind=kind, files=(*kept, *((types,) if kind == "starter" else ())), label=label
        )

    return make


async def gen_starter(
    b: BuildInputs,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    note: str | None = None,
    previous: Overlay | None = None,
    public: Overlay | None = None,
) -> GenResult:
    return await _generate(
        "gen_starter",
        lambda n: starter_input(b.task, b.contract, b.template, n, public),
        _app_overlay(b, "starter"),
        b.check,
        llm=llm,
        prompts=prompts,
        ontology_version=ontology_version,
        note=note,
        previous=previous,
        fix=b.fix_starter,
        prompt_name=b.template.manifest.prompt_tasks.get("gen_starter"),
    )


async def gen_public(
    b: BuildInputs,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    note: str | None = None,
    previous: Overlay | None = None,
) -> GenResult:
    """공개 테스트 — 공개 수용 기준(pt*)마다 기능 테스트. 테스트 먼저(TDD): 시작 코드·참조 구현이 이것을 보고 맞춘다.
    도우미(PublicCase)·저장 확인(PersistenceCheckTest)은 코드가 쓴다 — 생성기가 같은 경로를 쓰면 버린다."""
    m = b.template.manifest
    code_written = [
        f
        for f in (
            render_contract_case(b.contract, b.plan, m, audience="public"),
            render_persistence_check(b.contract, m),
        )
        if f is not None
    ]

    def with_helpers(files: list[GeneratedFile]) -> Overlay:
        kept = [f for f in files if f.path not in {c.path for c in code_written}]
        return Overlay(kind="public_test", files=(*kept, *code_written))

    return await _generate(
        "gen_public",
        lambda n: public_input(b.task, b.contract, b.plan, b.template, n),
        with_helpers,
        b.check,
        llm=llm,
        prompts=prompts,
        ontology_version=ontology_version,
        note=note,
        previous=previous,
        prompt_name=b.template.manifest.prompt_tasks.get("gen_public"),
        fix=test_import_fix(m),
    )


def case_owner(f: GeneratedFile, rx: re.Pattern[str], ids: Collection[str]) -> str | None:
    """숨은 테스트 파일의 케이스 — 계획의 케이스 id 가 하나만 나오면 그 케이스, 아니면 None (공용 파일)."""
    found = {i for i in rx.findall(f.content) if i in ids}
    return next(iter(found)) if len(found) == 1 else None


def repair_targets(
    note: str | None, files: Sequence[GeneratedFile], rx: re.Pattern[str], ids: Sequence[str]
) -> list[str]:
    """수리 사유가 가리키는 케이스 — 사유에 나온 케이스 id, 또는 사유에 경로가 나온 파일(컴파일 오류)의 케이스.
    못 가리면 모든 케이스 (사유가 숨은 테스트 전체에 대한 것)."""
    text = note or ""
    named = {i for i in rx.findall(text) if i in ids}
    named |= {o for f in files if f.path in text and (o := case_owner(f, rx, ids)) is not None}
    return [i for i in ids if i in named] or list(ids)


def case_note(
    note: str | None,
    cid: str,
    files: Sequence[GeneratedFile],
    rx: re.Pattern[str],
    ids: Sequence[str],
) -> str | None:
    """케이스 하나에 넘길 수정 요청 — 다른 케이스만 가리키는 줄은 뺀다 (케이스 id 또는 그 케이스 파일 경로로, repair_targets 와 같은 기준).
    어느 케이스도 가리키지 않는 줄은 모든 케이스에 간다.
    2026-11-11 재측정 gen-item: 'hc1 통과; hc3 통과; hc4 통과' 문구를 세 케이스 모두에 그대로 넘겨, 케이스 파일마다 다른 케이스 id 를
    적었다 — 케이스별 재요청 뒤에도 남아 합친 뒤 '케이스 id 가 여러 파일에 나옴'으로 숨은 테스트 수리 전체가 실패했다."""
    if not note:
        return note
    owners = {f.path: o for f in files if (o := case_owner(f, rx, ids)) is not None}
    kept = []
    for line in note.split("\n"):
        named = {i for i in rx.findall(line) if i in ids}
        named |= {o for path, o in owners.items() if path in line}
        if not named or cid in named:
            kept.append(line)
    return "\n".join(kept) if kept else note


def rename_for_case(
    f: GeneratedFile, cid: str, taken: Mapping[str, GeneratedFile]
) -> GeneratedFile:
    """다른 케이스 파일과 경로가 같으면 케이스 id 를 넣어 이름을 바꾼다 — Java 는 클래스 이름도 파일 이름과 같게.

    2026-11-09 fix12-be: 의도한 사유가 같은 숨은 케이스 hc2·hc3 을 따로 만들었더니 둘 다 RollbackLeftSuccessTest.java 라
    합칠 때 hc3 이 hc2 를 덮어 '케이스가 숨은 테스트에 없음: hc2' 로 생성 실패 — 루프가 시작도 못 했다."""
    p = PurePosixPath(f.path)
    tag = cid[:1].upper() + cid[1:]
    if p.suffix == ".java":
        stem = p.stem
        base = stem[: -len("Test")] if stem.endswith("Test") and len(stem) > 4 else stem
        new = f"{base}{tag}Test"
        path = str(p.with_name(new + ".java"))
        content = re.sub(rf"\b{re.escape(stem)}\b", new, f.content)
    else:
        head, _, rest = p.name.partition(".")
        path = str(p.with_name(f"{head}.{cid}.{rest}" if rest else f"{head}.{cid}"))
        content = f.content
    n = 2
    while path in taken:  # 그래도 겹치면 번호
        q = PurePosixPath(path)
        path = str(q.with_name(f"{q.stem}{n}{q.suffix}"))
        n += 1
    return f.model_copy(update={"path": path, "content": content})


async def gen_hidden(
    b: BuildInputs,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    note: str | None = None,
    previous: Overlay | None = None,
    keep_previous: Collection[str] = (),
) -> GenResult:
    """숨은 테스트 — 케이스마다 따로 (동시에) 부르고 합친다. 입력 계획에는 그 케이스만 보이고, 도우미·조절값은 계획 전체로 코드가 쓴다.
    수리(previous)는 사유가 가리키는 케이스만 그 케이스의 이전 파일로 다시 만들고, 다른 케이스 파일은 그대로 둔다.
    keep_previous: 재요청 뒤에도 문제가 남으면 이전 파일을 그대로 둘 케이스 — 빌드가 고른다 (사유 게이트가 모두 ③·③'·④ 이고
    직전 검증에서 모든 구현이 그 케이스를 판정까지 간 것만). 컴파일·setup·static 수리나 테스트 예외로 끝난 케이스는 이전 파일 자체가
    깨졌으니 비운다 — 남기면 같은 실패가 되풀이된다.
    2026-11-09 (사용자 결정): 한 번에 모든 케이스를 쓰면 한 케이스의 문제로 숨은 테스트 전체를 다시 만들어, 맞던 케이스도 바뀔 수 있다."""
    m = b.template.manifest
    params = render_params(b.plan, m)
    helper = render_contract_case(b.contract, b.plan, m)
    code_written = [params, *([helper] if helper is not None else [])]
    written = {c.path for c in code_written}
    rx = re.compile(m.hidden_rules.case_id_pattern)
    ids = [c.case_id for c in b.plan.cases]
    before = [f for f in (previous.files if previous is not None else ()) if f.path not in written]
    targets = repair_targets(note, before, rx, ids) if previous is not None else ids

    def mine(files: list[GeneratedFile]) -> Overlay:
        # 조절값 파일·계약 도우미는 코드가 쓴다 — 생성기가 같은 경로를 쓰면 버린다
        return Overlay(kind="hidden", files=tuple(f for f in files if f.path not in written))

    def one(cid: str) -> Any:
        prev = [f for f in before if case_owner(f, rx, ids) == cid]
        return _generate(
            "gen_hidden",
            lambda n: hidden_input(b.task, b.contract, b.plan, b.template, n, only={cid}),
            mine,
            lambda o: b.check(o, cases={cid}),
            llm=llm,
            prompts=prompts,
            ontology_version=ontology_version,
            note=case_note(note, cid, before, rx, ids),
            previous=Overlay(kind="hidden", files=tuple(prev)) if prev else None,
            prompt_name=m.prompt_tasks.get("gen_hidden"),
            fix=test_import_fix(m),
        )

    parts: list[GenResult] = list(await asyncio.gather(*(one(c) for c in targets)))
    res = GenResult(task="gen_hidden")
    for cid, r in zip(targets, parts, strict=True):
        res.provenance += r.provenance
        res.errors += r.errors
        res.fail_reasons += r.fail_reasons
        res.first_problems += [f"{cid}: {p}" for p in r.first_problems]
        res.flags += [
            f.replace("gen_hidden: ", f"gen_hidden({cid}): ", 1)
            for f in r.flags
            if f.startswith((FLAG_FAILED, FLAG_INVALID))
        ]
    if any(r.overlay is None for r in parts):
        return res
    kept = [f for f in before if case_owner(f, rx, ids) not in targets]
    files = {f.path: f for f in kept}
    unfixed: list[str] = []
    for cid, r in zip(targets, parts, strict=True):
        if r.problems:
            # 재요청 뒤에도 문제가 남은 답은 합치지 않는다 — 합치면 문제를 안은 채 다른 케이스까지 막는다 (2026-11-11 gen-item).
            # 수리(이전 파일이 판정까지 간 것)면 그 케이스는 이전 파일을 그대로 두고 표시만, 아니면 실패로 드러낸다.
            res.rejected.append(
                Overlay(kind="hidden", files=r.overlay.files, label=f"gen_hidden-{cid}")  # type: ignore[union-attr]
            )
            prev = [f for f in before if case_owner(f, rx, ids) == cid]
            if (
                cid in keep_previous
                and prev
                and not b.check(Overlay(kind="hidden", files=tuple(prev)), cases={cid})
            ):
                res.kept[cid] = list(r.problems)
                files.update({f.path: f for f in prev})
                continue
            unfixed += [f"{cid}: {p}" for p in r.problems]
            continue
        for f in r.overlay.files:  # type: ignore[union-attr]
            if f.path in files and case_owner(files[f.path], rx, ids) not in (None, cid):
                f = rename_for_case(f, cid, files)
            files[f.path] = f
    res.overlay = Overlay(kind="hidden", files=(*files.values(), *code_written))
    # 합친 뒤 계획 전체로 (케이스 누락·두 파일에 같은 케이스) + 합치지 않은 케이스의 남은 문제
    res.problems = unfixed + [p for p in b.check(res.overlay) if p not in unfixed]
    if res.problems:
        res.flags.append(FLAG_INVALID + "gen_hidden: " + "; ".join(res.problems))
    logger.info(
        "gen_hidden cases=%d regenerated=%d kept_files=%d problems=%d",
        len(ids),
        len(targets),
        len(kept),
        len(res.problems),
    )
    return res


async def gen_naive(
    b: BuildInputs,
    starter: Overlay,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    keep_traps: Sequence[str] = (),
    note: str | None = None,
    previous: Overlay | None = None,
    public: Overlay | None = None,
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
        lambda n: naive_input(b.task, b.contract, b.template, starter, seed, keep_traps, n, public),
        _app_overlay(b, "ref_naive", label),
        b.check,
        llm=llm,
        prompts=prompts,
        ontology_version=ontology_version,
        note=note,
        previous=previous,
        fix=lambda fs: b.fix(fs, starter),
        prompt_name=b.template.manifest.prompt_tasks.get("gen_naive"),
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
    previous: Overlay | None = None,
    public: Overlay | None = None,
) -> GenResult:
    return await _generate(
        "gen_accepted",
        lambda n: accepted_input(b.task, b.contract, b.template, starter, design, n, public),
        _app_overlay(b, "ref_accepted", design.design_id),
        lambda o: b.check(o, starter),
        llm=llm,
        prompts=prompts,
        ontology_version=ontology_version,
        note=note,
        previous=previous,
        fix=lambda fs: b.fix(fs, starter),
        prompt_name=b.template.manifest.prompt_tasks.get("gen_accepted"),
    )
