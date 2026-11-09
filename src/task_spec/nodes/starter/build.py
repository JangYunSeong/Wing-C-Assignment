"""시작 코드 빌드 루프 — 생성 → 정적 검사 → 검증 → 실패 배정 → 그 생성기만 다시 (docs/design/spec-to-code.md §2 ③~⑥ · §7).

6단계 앞부분: 그래프 연결(State 는 경로·해시만, interrupt, 저장 #52)은 verifier(#55)·저장소가 생긴 뒤 이 순서를 노드로 옮긴다.
여기서는 같은 순서를 함수 하나로 돌린다 — 테스트는 가짜 verifier, 스모크는 개발 서버 Gradle verifier(eval/starter/gradle_verify.py).

예산 (§7): 산출물(생성기·라벨)마다 재생성 1회(컴파일 오류만이면 2회), 빌드 전체 재생성 최대 3회. 예산을 넘거나 배정이 모호하면 다시 만들지 않고 `failed`.
시작 코드를 다시 만들면 그 위에 쓴 참조 구현도 다시 만든다 (참조 구현 예산은 쓰지 않음). 숨은 테스트는 계약만 보므로 그대로.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal, Protocol

from task_spec.llm import LLMClient
from task_spec.nodes.starter.generate import (
    BuildInputs,
    GenResult,
    gen_accepted,
    gen_hidden,
    gen_naive,
    gen_public,
    gen_starter,
)
from task_spec.prompting import PromptRegistry
from task_spec.schemas import AcceptedDesign, CompileError, Provenance, VerifyReport
from task_spec.schemas.build import RepairRecord
from task_spec.starter.contract import SpecView
from task_spec.starter.hidden_helpers import render_persistence_check
from task_spec.starter.render import render_params
from task_spec.starter.repair import STARTER, RepairAction, RepairPlan, assign
from task_spec.starter.static_check import HARNESS_OVERRIDE, Overlay, static_check

MAX_ROUNDS = 3  # 빌드 전체 재생성 상한
PER_ARTIFACT = 1  # 산출물마다 재생성 상한
COMPILE_BUDGET = 2  # 컴파일 오류만 고칠 때는 2회까지 — 고치기 싸고, 2026-11-07 루프에서 1회 수리가 틀린 방법으로 고쳐 예산이 끝났다
COMPILE_TEST_ATTEMPTS = 2  # 채점 코드 컴파일 오류 재요청 상한 (구현 만들기 전, compile_and_fix)
COMPILE_IMPL_ATTEMPTS = (
    2  # 구현(시작 코드·참조 구현) 컴파일 오류 재요청 상한 (무거운 검증 전, compile_impl_and_fix)
)
HIDDEN = "hidden"
PUBLIC = "public"  # 공개 테스트 오버레이 키 (gen_public — 테스트 먼저, 2026-11-08)

logger = logging.getLogger(__name__)


class Verifier(Protocol):
    """채점 경로로 대상마다 돌린 결과 — 시작 코드(①②), 시작 코드가 기동하면 시작 코드 그대로 + 숨은 테스트
    (kind `starter_hidden`, ②' — 이미 풀려 있으면 안 됨), 참조 구현마다 시작 코드 + 구현 + 숨은 테스트(③~)."""

    async def verify(
        self,
        starter: Overlay,
        hidden: Overlay,
        refs: Sequence[Overlay],
        public: Overlay | None = None,
    ) -> VerifyReport: ...


class TestCompiler(Protocol):
    """채점 코드(숨은·공개 테스트 + 하네스 + 코드가 쓴 도우미)만 구현 없이 컴파일 — 템플릿 commands.test_compile."""

    async def compile_tests(
        self, hidden: Overlay, public: Overlay | None
    ) -> list[CompileError]: ...

    # 오버레이 조합을 컴파일 (구현 + 둘레 산출물 — compile_impl_and_fix). 없는 컴파일러는 구현 컴파일 루프를 건너뛴다.
    async def compile_overlays(self, overlays: Sequence[Overlay]) -> list[CompileError]: ...


@dataclass
class CompileFix:
    """compile_and_fix 결과 — 고친 테스트 오버레이, 컴파일 횟수, 첫·마지막 오류, 수리 이력, 재요청 결과."""

    hidden: Overlay
    public: Overlay | None
    compiles: int = 0
    first_errors: list[CompileError] = field(default_factory=list)
    errors: list[CompileError] = field(default_factory=list)  # 마지막 컴파일에 남은 오류
    repairs: list[RepairRecord] = field(default_factory=list)
    results: list[GenResult] = field(default_factory=list)


def _code_written(b: BuildInputs) -> set[str]:
    """코드가 쓴 테스트 파일 — 생성기를 다시 불러도 바뀌지 않는다 (조절값 파일·도우미·저장 확인)."""
    m = b.template.manifest
    paths = {render_params(b.plan, m).path}
    paths |= {h.path for h in (m.hidden_helper, m.public_helper) if h is not None}
    check = render_persistence_check(b.contract, m)
    return paths | ({check.path} if check is not None else set())


TESTS_NOTE = (
    "채점 코드 컴파일 오류 (javac·tsc — 구현 없이 테스트·하네스·도우미만 컴파일) — 고쳐서 다시 쓴다. "
    "하네스·도우미에 없는 이름은 문서·소스에 있는 이름으로, 쓰는 타입은 import 해서:"
)
IMPL_NOTE = (
    "구현 컴파일 오류 (javac·tsc — 이 구현을 시작 코드·공개·숨은 테스트와 함께 컴파일) — 오류가 난 파일만 고쳐서 다시 쓴다. "
    "쓰는 타입은 import 하고, 계약·시작 코드에 있는 이름 그대로:"
)


def compile_note(errors: Sequence[CompileError], overlay: Overlay, header: str = TESTS_NOTE) -> str:
    """재요청 사유 — javac 오류마다 위치·메시지·symbol/location, 그리고 그 줄의 코드."""
    files = {f.path: f.content.splitlines() for f in overlay.files}
    lines = [header]
    for e in errors[:10]:
        src = files.get(e.path, [])
        lines.append(f"- {e.path}:{e.line} {e.message}" + (f" ({e.detail})" if e.detail else ""))
        if 0 < e.line <= len(src):
            lines.append(f"  코드: {src[e.line - 1].strip()[:200]}")
    return "\n".join(lines)


async def compile_and_fix(
    b: BuildInputs,
    hidden: Overlay,
    public: Overlay | None,
    compiler: TestCompiler,
    kw: dict[str, object],
    attempts: int = COMPILE_TEST_ATTEMPTS,
) -> CompileFix:
    """테스트 먼저 — 구현을 만들기 전에 채점 코드를 컴파일하고, 오류가 난 파일을 쓴 생성기(gen_hidden·gen_public)에 javac 오류를
    붙여 다시 요청한다 (최대 attempts 회). 코드가 쓴 파일·Gradle 자체 오류는 생성기로 못 고치므로 거기서 멈춘다.

    2026-11-08 Gradle 로그 314개: 숨은 테스트 컴파일 오류 141줄 — Duration·람다·검사 예외·import·없는 이름(`Burst.IntUnaryOperator`) 등
    종류가 많아 이름 검사만으로는 못 막는다. 상류 평가(구현 없이 compileTestJava)로 컴파일할 수 있음을 확인했다."""
    fix = CompileFix(hidden=hidden, public=public)
    fixed = _code_written(b)
    for attempt in range(attempts + 1):
        errors = await compiler.compile_tests(fix.hidden, fix.public)
        fix.compiles += 1
        if attempt == 0:
            fix.first_errors = list(errors)
        fix.errors = list(errors)
        if not errors or attempt == attempts:
            break
        owners = {
            name: o
            for name, o in (("gen_hidden", fix.hidden), ("gen_public", fix.public))
            if o is not None
        }
        mine = {
            name: [e for e in errors if e.path in {f.path for f in o.files} - fixed]
            for name, o in owners.items()
        }
        if not any(mine.values()):
            break
        jobs = [
            (
                name,
                (gen_hidden if name == "gen_hidden" else gen_public)(
                    b,
                    note=compile_note(errs, owners[name]),
                    previous=owners[name],
                    **kw,  # type: ignore[arg-type]
                ),
            )
            for name, errs in mine.items()
            if errs
        ]
        results = await asyncio.gather(*(j for _, j in jobs))
        for (name, _), r in zip(jobs, results, strict=True):
            fix.results.append(r)
            e = mine[name][0]
            ok = r.overlay is not None and r.ok
            fix.repairs.append(
                RepairRecord(
                    gate="compile",
                    target=name,  # type: ignore[arg-type]
                    reason=f"{PurePosixPath(e.path).name}:{e.line} {e.message}"[:300],
                    result="regenerated" if ok else "failed",
                )
            )
            if ok and name == "gen_hidden":
                fix.hidden = r.overlay  # type: ignore[assignment]
            elif ok:
                fix.public = r.overlay
    return fix


@dataclass
class ImplFix:
    """compile_impl_and_fix 결과 — 고친 구현 오버레이, 컴파일 횟수, 첫·마지막 오류, 수리 이력, 재요청 결과."""

    overlay: Overlay
    compiles: int = 0
    first_errors: list[CompileError] = field(default_factory=list)
    errors: list[CompileError] = field(default_factory=list)
    repairs: list[RepairRecord] = field(default_factory=list)
    results: list[GenResult] = field(default_factory=list)


async def compile_impl_and_fix(
    generator: str,
    overlay: Overlay,
    around: Callable[[Overlay], list[Overlay]],
    regen: Callable[[str, Overlay], Awaitable[GenResult]],
    compiler: TestCompiler,
    attempts: int = COMPILE_IMPL_ATTEMPTS,
) -> ImplFix:
    """구현 하나(시작 코드·참조 구현)를 둘레 산출물과 함께 컴파일하고, 그 구현이 쓴 파일의 javac 오류만 그 생성기에 붙여 다시
    요청한다 (최대 attempts 회). 다른 산출물 파일의 오류는 이 생성기로 못 고치므로 거기서 멈춘다 — 무거운 검증(기동·테스트)
    전에 싸게 고친다. 2026-11-08 알파: 시작 코드가 자기 기존 테스트에서 import 를 빠뜨려(`Map`) 첫 검증 회차가 통째로
    compile_error, 이어서 참조 구현 컴파일 수리로 회차 3·$0.40 — 컴파일 루프가 채점 코드만 보던 탓."""
    fix = ImplFix(overlay=overlay)
    compile_overlays = getattr(compiler, "compile_overlays", None)
    if compile_overlays is None:
        return fix
    for attempt in range(attempts + 1):
        errors = await compile_overlays(around(fix.overlay))
        fix.compiles += 1
        if attempt == 0:
            fix.first_errors = list(errors)
        fix.errors = list(errors)
        if not errors or attempt == attempts:
            break
        mine = [e for e in errors if e.path in {f.path for f in fix.overlay.files}]
        if not mine:
            break
        r = await regen(compile_note(mine, fix.overlay, IMPL_NOTE), fix.overlay)
        fix.results.append(r)
        ok = r.overlay is not None and r.ok
        e = mine[0]
        fix.repairs.append(
            RepairRecord(
                gate="compile",
                target=generator,  # type: ignore[arg-type]
                reason=(
                    (f"{overlay.label}: " if overlay.label else "")
                    + f"{PurePosixPath(e.path).name}:{e.line} {e.message}"
                )[:300],
                result="regenerated" if ok else "failed",
            )
        )
        if ok:
            fix.overlay = r.overlay  # type: ignore[assignment]
    return fix


def _starter_around(public: Overlay | None) -> Callable[[Overlay], list[Overlay]]:
    return lambda s: [s, *([public] if public is not None else [])]


def _ref_around(
    starter: Overlay, public: Overlay | None, hidden: Overlay
) -> Callable[[Overlay], list[Overlay]]:
    return lambda r: [starter, r, *([public] if public is not None else []), hidden]


def _ref_regen(
    b: BuildInputs,
    o: Overlay,
    starter: Overlay,
    public: Overlay | None,
    by_id: dict[str, AcceptedDesign],
    kw: dict[str, object],
) -> tuple[str, Callable[[str, Overlay], Awaitable[GenResult]]]:
    if o.kind == "ref_naive":
        return "gen_naive", lambda note, prev: gen_naive(
            b,
            starter,
            note=note,
            previous=prev,
            public=public,
            **kw,  # type: ignore[arg-type]
        )
    design = by_id[o.label]
    return "gen_accepted", lambda note, prev: gen_accepted(
        b,
        starter,
        design,
        note=note,
        previous=prev,
        public=public,
        **kw,  # type: ignore[arg-type]
    )


def _compile_flag(what: str, fix: ImplFix) -> str:
    e = fix.errors[0]
    return (
        f"{what} 컴파일 실패 (재요청 {len(fix.repairs)}회 뒤, 오류 {len(fix.errors)}건): "
        f"{e.path}:{e.line} {e.message} {e.detail}"
    ).strip()[:300]


async def _compile_refs(
    out: BuildOutcome,
    b: BuildInputs,
    by_id: dict[str, AcceptedDesign],
    kw: dict[str, object],
    compiler: TestCompiler,
    absorb: Callable[[ImplFix], None],
    labels: Sequence[str],
) -> None:
    """참조 구현마다 시작 코드·공개·숨은 테스트와 함께 컴파일 — 오류는 그 구현에 (개발 서버 한 대 — 차례로).
    끝까지 안 되면 표시만 하고 검증으로 넘긴다 (다른 구현은 판정할 수 있고, 배정 규칙이 컴파일 오류를 다시 다룬다)."""
    starter, public, hidden = out.overlays[STARTER], out.overlays.get(PUBLIC), out.overlays[HIDDEN]
    for label in labels:
        o = out.overlays.get(label)
        if o is None:
            continue
        generator, regen = _ref_regen(b, o, starter, public, by_id, kw)
        fix = await compile_impl_and_fix(
            generator, o, _ref_around(starter, public, hidden), regen, compiler
        )
        absorb(fix)
        out.overlays[label] = fix.overlay
        if fix.errors:
            out.review_flags.append(_compile_flag(label, fix))


@dataclass
class BuildOutcome:
    status: Literal["review", "failed"]
    overlays: dict[str, Overlay] = field(default_factory=dict)  # starter · hidden · 참조 구현 라벨
    reports: list[VerifyReport] = field(default_factory=list)  # 검증 회차마다
    repairs: list[RepairRecord] = field(default_factory=list)
    review_flags: list[str] = field(default_factory=list)
    adjustments: list[str] = field(
        default_factory=list
    )  # 생성기 출력에서 코드가 고친 것 — 승인 화면에
    regen_counts: dict[str, int] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)
    rounds: int = 0  # 다시 만든 회차 수
    snapshots: list[dict[str, Overlay]] = field(
        default_factory=list
    )  # 검증한 회차마다 그때의 오버레이 (평가·디버깅용)

    @property
    def cost_usd(self) -> float:
        return round(sum(p.cost_usd or 0.0 for p in self.provenance), 4)


def _record(a: RepairAction, result: Literal["regenerated", "failed", "skipped"]) -> RepairRecord:
    reason = f"{a.label}: {a.reason}" if a.label else a.reason
    return RepairRecord(gate=a.gate, target=a.generator, reason=reason[:300], result=result)


def _voided(r: GenResult) -> bool:
    """허용 구현 생성이 하네스가 덮어쓰는 설정 문제로만 실패 (생성기 안 정적 검사 — 재요청 뒤에도)."""
    return (
        r.task == "gen_accepted"
        and not r.ok
        and r.overlay is not None
        and bool(r.problems)
        and all(HARNESS_OVERRIDE in p for p in r.problems)
    )


def _static_actions(overlays: dict[str, Overlay], b: BuildInputs) -> RepairPlan:
    """여러 오버레이를 함께 봐야 잡히는 정적 문제 (예: 숨은 테스트 이름이 시작 코드에) — 생성기 안 검사는 자기 오버레이만 본다."""
    res = static_check(
        list(overlays.values()),
        template=b.template,
        plan=b.plan,
        spec=SpecView.from_task(b.task),
        contract=b.contract,
        seed=b.seed,
        avoid_terms=b.avoid_terms,
    )
    plan = RepairPlan()
    plan.flags = list(res.review_flags)
    for target in res.targets:
        msgs = res.for_target(target)
        note = "정적 검사 문제 — 고쳐서 다시 쓴다:\n" + "\n".join(f"- {m}" for m in msgs)
        if target in ("gen_starter", "gen_hidden", "gen_public"):
            plan.actions.append(RepairAction(target, "", "static", msgs[0][:200], note))  # type: ignore[arg-type]
            continue
        kind = "ref_naive" if target == "gen_naive" else "ref_accepted"
        labels = [k for k, o in overlays.items() if o.kind == kind]
        named = [k for k in labels if any(k in m for m in msgs)] or labels
        for label in named:
            plan.actions.append(RepairAction(target, label, "static", msgs[0][:200], note))  # type: ignore[arg-type]
    return plan


async def build_and_repair(
    b: BuildInputs,
    designs: Sequence[AcceptedDesign],
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    verifier: Verifier,
    max_rounds: int = MAX_ROUNDS,
    compiler: TestCompiler | None = None,
) -> BuildOutcome:
    kw = {"llm": llm, "prompts": prompts, "ontology_version": ontology_version}
    out = BuildOutcome(status="failed")
    by_id = {d.design_id: d for d in designs}
    missed = {"naive-all": {t.trap_id for t in b.task.traps}}

    def took(r: GenResult) -> Overlay | None:
        out.provenance += r.provenance
        out.adjustments += [f"{r.task}: {a}" for a in r.adjustments]
        if r.overlay is None or not r.ok:
            out.review_flags.append(
                f"{r.task} 생성 실패: " + "; ".join([*r.problems, *r.fail_reasons][:3])
            )
            return None
        return r.overlay

    def absorb(fix: ImplFix) -> None:
        for r in fix.results:
            out.provenance += r.provenance
            out.adjustments += [f"{r.task}: {a}" for a in r.adjustments]
        out.repairs += fix.repairs

    def drop(r: GenResult, others: int) -> bool:
        """허용 구현이 재요청에도 하네스가 덮어쓰는 설정에 기대면 그 설계를 뺀다 — 다른 허용 설계가 남을 때만 (사람 확인)."""
        if not (_voided(r) and others > 0):
            return False
        out.provenance += r.provenance
        label = r.overlay.label if r.overlay is not None else "?"
        out.review_flags.append(
            f"허용 설계 {label} 제외 — 구현이 {HARNESS_OVERRIDE}에 기댐 (재료의 허용 설계 확인)"
        )
        return True

    # ① 테스트 먼저 (2026-11-08 사용자 결정 — TDD): 공개 테스트·숨은 테스트를 계약에서 따로 (서로 보지 않음)
    use_public = b.template.manifest.public_helper is not None
    tests = await asyncio.gather(
        *([gen_public(b, **kw)] if use_public else []),  # type: ignore[arg-type]
        gen_hidden(b, **kw),  # type: ignore[arg-type]
    )
    public = took(tests[0]) if use_public else None
    hidden = took(tests[-1])
    made_tests = {k: o for k, o in ((PUBLIC, public), (HIDDEN, hidden)) if o is not None}
    if hidden is None or (use_public and public is None):
        out.overlays = made_tests  # 만든 것은 남긴다 — 평가·사람이 볼 수 있게
        return out
    if (
        compiler is not None
    ):  # 채점 코드가 컴파일되어야 구현을 만든다 (공개 테스트를 보고 맞추는 전제)
        fix = await compile_and_fix(b, hidden, public, compiler, kw)
        for r in fix.results:
            out.provenance += r.provenance
            out.adjustments += [f"{r.task}: {a}" for a in r.adjustments]
        out.repairs += fix.repairs
        hidden, public = fix.hidden, fix.public
        made_tests = {k: o for k, o in ((PUBLIC, public), (HIDDEN, hidden)) if o is not None}
        if fix.errors:
            e = fix.errors[0]
            out.review_flags.append(
                f"채점 코드 컴파일 실패 (재요청 {len(fix.repairs)}회 뒤, 오류 {len(fix.errors)}건): "
                f"{e.path}:{e.line} {e.message} {e.detail}".strip()[:300]
            )
            out.overlays = made_tests
            return out
    # ② 구현 코드 — 공개 테스트를 보고 맞춘다 (숨은 테스트는 보지 않음)
    starter = took(await gen_starter(b, public=public, **kw))  # type: ignore[arg-type]
    if starter is None:
        out.overlays = made_tests
        return out
    if (
        compiler is not None
    ):  # 시작 코드가 (공개 테스트와 함께) 컴파일되어야 그 위에 참조 구현을 쓴다
        sfix = await compile_impl_and_fix(
            "gen_starter",
            starter,
            _starter_around(public),
            lambda note, prev: gen_starter(b, note=note, previous=prev, public=public, **kw),  # type: ignore[arg-type]
            compiler,
        )
        absorb(sfix)
        starter = sfix.overlay
        if sfix.errors:
            out.review_flags.append(_compile_flag("시작 코드", sfix))
            out.overlays = {STARTER: starter, **made_tests}
            return out
    first = await asyncio.gather(
        gen_naive(b, starter, public=public, **kw),  # type: ignore[arg-type]
        *(gen_accepted(b, starter, d, public=public, **kw) for d in designs),  # type: ignore[arg-type]
    )
    accepted = list(first[1:])
    usable = sum(1 for r in accepted if not _voided(r))
    kept = [r for r in accepted if not drop(r, usable)]
    made = [took(r) for r in (first[0], *kept)]
    # 하나라도 실패해도 만든 것은 남긴다 — 평가·사람이 볼 수 있게 (2026-11-08: 숨은 테스트 생성 실패로 시작 코드 파일도 안 남음)
    out.overlays = {STARTER: starter, **made_tests, **{o.label: o for o in made if o is not None}}
    if any(o is None for o in made):
        return out
    if compiler is not None:
        await _compile_refs(out, b, by_id, kw, compiler, absorb, [o.label for o in made if o])

    for round_no in range(max_rounds + 1):
        out.rounds = round_no
        plan = _static_actions(out.overlays, b)
        for flag in plan.flags:
            if flag not in out.review_flags:
                out.review_flags.append(flag)
        if plan.passed:
            report = await verifier.verify(
                out.overlays[STARTER],
                out.overlays[HIDDEN],
                [o for k, o in out.overlays.items() if k not in (STARTER, HIDDEN, PUBLIC)],
                **({"public": out.overlays[PUBLIC]} if PUBLIC in out.overlays else {}),
            )
            out.reports.append(report)
            out.snapshots.append(dict(out.overlays))
            shared = set(b.template.files("project")) | {
                f.path for f in out.overlays[STARTER].files
            }  # 이미 있는 파일 — 모든 구현이 다시 쓰면 원인 후보
            plan = assign(report, b.plan, out.overlays, missed, out.regen_counts, shared)
            for (
                flag
            ) in plan.flags:  # 막지 않는 표시 (예: 다시 만든 시작 코드가 또 숨은 케이스 통과)
                if flag not in out.review_flags:
                    out.review_flags.append(flag)
        logger.info(
            "build round=%d actions=%d unresolved=%d",
            round_no,
            len(plan.actions),
            len(plan.unresolved),
        )
        if plan.passed:
            out.status = "review"
            return out
        blocked = [
            a
            for a in plan.actions
            if out.regen_counts.get(a.budget_key, 0)
            >= (COMPILE_BUDGET if a.compile else PER_ARTIFACT)
        ]
        if plan.unresolved or blocked or round_no == max_rounds:
            out.review_flags += plan.unresolved
            out.review_flags += [f"재생성 예산을 다 씀 — {a.key}: {a.reason}" for a in blocked]
            if round_no == max_rounds and not (plan.unresolved or blocked):
                out.review_flags.append(
                    f"빌드 전체 재생성 상한({max_rounds}회) — 남은 실패를 사람이 확인"
                )
            out.repairs += [_record(a, "skipped") for a in plan.actions]
            return out
        if not await _regenerate(plan.actions, out, b, by_id, kw, took, drop, compiler, absorb):
            return out
    return out


async def _regenerate(
    actions: list[RepairAction],
    out: BuildOutcome,
    b: BuildInputs,
    by_id: dict[str, AcceptedDesign],
    kw: dict[str, object],
    took,  # type: ignore[no-untyped-def]
    drop,  # type: ignore[no-untyped-def]
    compiler: TestCompiler | None = None,
    absorb: Callable[[ImplFix], None] | None = None,
) -> bool:
    """배정된 생성기만 다시 — 자기 이전 출력을 주고 고칠 파일만 받는다 (프롬프트가 지원하면, generate._generate).
    시작 코드가 바뀌면 참조 구현은 모두 새 시작 코드 위에 처음부터 다시 쓴다."""
    notes = {a.key: f"검증 실패 (게이트 {a.gate}) — {a.note}" for a in actions}
    for a in actions:  # 컴파일 수리는 따로 센다 (RepairAction.budget_key)
        out.regen_counts[a.budget_key] = out.regen_counts.get(a.budget_key, 0) + 1
    starter = out.overlays[STARTER]
    public = out.overlays.get(PUBLIC)
    if (
        "gen_public" in notes and public is not None
    ):  # 테스트가 바뀌면 구현은 다음 검증에서 다시 맞춰 본다
        new_public = took(
            await gen_public(b, note=notes["gen_public"], previous=public, **kw)  # type: ignore[arg-type]
        )
        out.repairs += [
            _record(a, "regenerated" if new_public is not None else "failed")
            for a in actions
            if a.generator == "gen_public"
        ]
        if new_public is None:
            return False
        out.overlays[PUBLIC] = public = new_public
    if "gen_starter" in notes:
        new = took(
            await gen_starter(b, note=notes["gen_starter"], previous=starter, public=public, **kw)  # type: ignore[arg-type]
        )
        ok = new is not None
        out.repairs += [
            _record(a, "regenerated" if ok else "failed")
            for a in actions
            if a.generator == "gen_starter"
        ]
        if new is None:
            return False
        if (
            compiler is not None and absorb is not None
        ):  # 다시 만든 시작 코드도 무거운 검증 전에 컴파일
            sfix = await compile_impl_and_fix(
                "gen_starter",
                new,
                _starter_around(public),
                lambda note, prev: gen_starter(b, note=note, previous=prev, public=public, **kw),  # type: ignore[arg-type]
                compiler,
            )
            absorb(sfix)
            new = sfix.overlay
            if sfix.errors:
                out.review_flags.append(_compile_flag("다시 만든 시작 코드", sfix))
                out.overlays[STARTER] = new
                return False
        out.overlays[STARTER] = starter = new
    refs = [(k, o) for k, o in out.overlays.items() if k not in (STARTER, HIDDEN, PUBLIC)]
    jobs: list[tuple[str, object]] = []
    if "gen_hidden" in notes:
        hidden_prev = out.overlays[HIDDEN]
        jobs.append((HIDDEN, gen_hidden(b, note=notes["gen_hidden"], previous=hidden_prev, **kw)))  # type: ignore[arg-type]
    for label, o in refs:
        if o.kind == "ref_naive":
            key = f"gen_naive:{label}"
            if key in notes or "gen_starter" in notes:
                prev = (
                    o if "gen_starter" not in notes else None
                )  # 시작 코드가 바뀌면 새 시작 코드 위에 처음부터
                jobs.append(
                    (
                        label,
                        gen_naive(
                            b, starter, note=notes.get(key), previous=prev, public=public, **kw
                        ),
                    )
                )  # type: ignore[arg-type]
        else:
            key = f"gen_accepted:{label}"
            if key in notes or "gen_starter" in notes:
                design = by_id[label]
                prev = o if "gen_starter" not in notes else None
                jobs.append(
                    (
                        label,
                        gen_accepted(
                            b,
                            starter,
                            design,
                            note=notes.get(key),
                            previous=prev,
                            public=public,
                            **kw,
                        ),
                    )  # type: ignore[arg-type]
                )
    results = await asyncio.gather(*(j for _, j in jobs))  # type: ignore[arg-type]
    ok_all = True
    remade: list[str] = []
    for (label, _), r in zip(jobs, results, strict=True):
        others = sum(1 for k, o in out.overlays.items() if o.kind == "ref_accepted" and k != label)
        if drop(r, others):
            out.overlays.pop(label, None)
            continue
        new = took(r)
        if new is None:
            ok_all = False
        else:
            out.overlays[label] = new
            if label != HIDDEN:
                remade.append(label)
    if (
        compiler is not None and absorb is not None and remade
    ):  # 다시 만든 참조 구현도 검증 전에 컴파일
        await _compile_refs(out, b, by_id, kw, compiler, absorb, remade)
    for a in actions:
        if a.generator in ("gen_starter", "gen_public"):
            continue
        label = HIDDEN if a.generator == "gen_hidden" else a.label
        ok = label in dict(jobs) and out.overlays.get(label) is not None
        out.repairs.append(_record(a, "regenerated" if ok and ok_all else "failed"))
    return ok_all
