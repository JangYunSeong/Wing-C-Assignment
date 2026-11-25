"""시작 코드 빌드 루프 — 생성 → 정적 검사 → 검증 → 실패 배정 → 그 생성기만 다시 (docs/design/spec-to-code.md §2 ③~⑥ · §7).

6단계 앞부분: 그래프 연결(State 는 경로·해시만, interrupt, 저장 #52)은 verifier(#55)·저장소가 생긴 뒤 이 순서를 노드로 옮긴다.
여기서는 같은 순서를 함수 하나로 돌린다 — 테스트는 가짜 verifier, 스모크·builder 는 스택별 verifier(starter/verify/ — `verifier_for`).

예산 (§7): 산출물(생성기·라벨)마다 재생성 1회(컴파일 오류만이면 2회), 빌드 전체 재생성 최대 3회. 예산을 넘거나 배정이 모호하면 다시 만들지 않고 `failed`.
시작 코드를 다시 만들면 그 위에 쓴 참조 구현도 다시 만든다 (참조 구현 예산은 쓰지 않음). 숨은 테스트는 계약만 보므로 그대로.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import PurePosixPath
from typing import Literal, Protocol

from task_spec.llm import LLMClient
from task_spec.nodes.starter.generate import (
    BuildInputs,
    GenResult,
    TrapGen,
    case_owner,
    gen_accepted,
    gen_hidden,
    gen_naive,
    gen_public,
    gen_starter,
    gen_traps,
    gen_variants,
)
from task_spec.prompting import PromptRegistry
from task_spec.schemas import (
    AcceptedDesign,
    CompileError,
    GeneratedFile,
    Provenance,
    VerifyReport,
)
from task_spec.schemas.build import HiddenPlan, RepairRecord, TaskContract
from task_spec.starter import calibrate as calib
from task_spec.starter.conformance import render_conformance
from task_spec.starter.contract import SpecView
from task_spec.starter.hand_refs import GATE as HAND_GATE
from task_spec.starter.hand_refs import NAME as HAND_NAME
from task_spec.starter.hand_refs import (
    HandGate,
    finding_flags,
    hidden_note,
    judge_hands,
    public_note,
)
from task_spec.starter.hidden_helpers import code_written_paths, time_settings
from task_spec.starter.material_guide import public_rules as guide_public_rules
from task_spec.starter.repair import (
    COMPILE_RETRIES,
    MATERIAL_VERDICT,
    STARTER,
    WEAK_FLAG,
    ContractSignal,
    RegenNote,
    RepairAction,
    RepairPlan,
    assign,
    contract_signals,
    gates_of,
)
from task_spec.starter.repair import HAND as HAND_POOL
from task_spec.starter.repair import TRAP_GATE as TRAP_POOL
from task_spec.starter.repair import VARIANT as VARIANT_POOL
from task_spec.starter.static_check import HARNESS_OVERRIDE, Overlay, static_check
from task_spec.starter.templates import is_java
from task_spec.starter.traps import (
    GATE as TRAP_GATE,
)
from task_spec.starter.traps import (
    TrapGate,
    TrapResult,
    TrapSpec,
    dropped_note,
    invalid_note,
    judge_traps,
    trap_note,
    trap_targets,
    unread_traps,
    untested_designs,
)
from task_spec.starter.traps import (
    survivors as trap_survivors,
)
from task_spec.starter.variants import (
    OP_NAME,
    VariantGate,
    VariantResult,
    judge,
    pts_by_test,
    repair_note,
    survivors,
    targets,
)

MAX_ROUNDS = 3  # 빌드 전체 재생성 상한
# 재생성 예산은 산출물·풀마다 (starter/repair.py POOLS — 상한은 Pool.limit). 컴파일 재요청 상한은 그 표의 COMPILE_RETRIES 하나에서.
COMPILE_TEST_ATTEMPTS = (
    COMPILE_RETRIES  # 채점 코드 컴파일 오류 재요청 상한 (구현 만들기 전, compile_and_fix)
)
COMPILE_IMPL_ATTEMPTS = (
    COMPILE_RETRIES  # 구현 컴파일 오류 재요청 상한 (무거운 검증 전, compile_impl_and_fix)
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
        conform: Overlay | None = None,
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
    """코드가 쓴 파일 — 생성기를 다시 불러도 바뀌지 않는다 (조절값 파일·도우미·저장 확인·시드). 크기 상한과 같은 집합 (#178)."""
    return code_written_paths(b.template.manifest)


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
        stale = out.compile_flags.pop(
            label, None
        )  # 같은 구현을 다시 컴파일했다 — 앞 표시는 옛 판의 것
        if stale in out.review_flags:
            out.review_flags.remove(stale)
        if fix.errors:
            flag = _compile_flag(label, fix)
            out.compile_flags[label] = flag
            out.review_flags.append(flag)


def _note_passed(out: BuildOutcome, report: VerifyReport) -> None:
    """참조 구현마다 지금 숨은 테스트로 통과한 케이스를 모은다 — 다시 만든 판이 그 케이스에서 떨어지면 테스트가 아니라 퇴행 (#183)."""
    for run in report.runs:
        if run.kind in ("ref_naive", "ref_accepted") and run.setup == "ok":
            out.passed_before.setdefault(run.target, set()).update(
                t.name for t in run.results("hidden") if t.result == "PASS"
            )


def _withdraw_compile_flags(out: BuildOutcome, report: VerifyReport) -> None:
    """참조 구현의 컴파일 실패 표시는 그 구현의 그때 상태다 — 뒤 검증에서 그 구현이 컴파일·기동되면(setup ok) 거둔다.
    2026-11-14 kakaobank003: d3 컴파일 수리가 끝까지 안 되어 표시가 붙었고, 같은 회차의 기동 수리로 다시 만든 d3 가 검증을 모두
    통과했는데도 '컴파일 실패' 표시가 승인자 화면에 남았다."""
    for run in report.runs:
        flag = out.compile_flags.get(run.target)
        if flag is not None and run.setup == "ok":
            del out.compile_flags[run.target]
            if flag in out.review_flags:
                out.review_flags.remove(flag)


@dataclass
class BuildOutcome:
    status: Literal["review", "failed"]
    overlays: dict[str, Overlay] = field(default_factory=dict)  # starter · hidden · 참조 구현 라벨
    reports: list[VerifyReport] = field(default_factory=list)  # 검증 회차마다
    repairs: list[RepairRecord] = field(default_factory=list)
    review_flags: list[str] = field(default_factory=list)
    # 참조 구현 라벨 → 그 구현의 컴파일 실패 표시 (review_flags 에도 있음) — 뒤 검증에서 기동되면 거둔다 (_withdraw_compile_flags)
    compile_flags: dict[str, str] = field(default_factory=dict)
    # 산출물 키 → 마지막으로 다시 만든 이유 (#183 — 수리 배정이 퇴행과 '이 케이스 때문에 다시 만들었는데 또 떨어짐'을 가른다)
    regen_log: dict[str, RegenNote] = field(default_factory=dict)
    # 참조 구현 라벨 → 그 설계의 어느 판이든 지금 숨은 테스트로 통과한 케이스 — 숨은 테스트·시작 코드를 다시 만들면 비운다
    passed_before: dict[str, set[str]] = field(default_factory=dict)
    adjustments: list[str] = field(
        default_factory=list
    )  # 생성기 출력에서 코드가 고친 것 — 승인 화면에
    regen_counts: dict[str, int] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)
    rounds: int = 0  # 다시 만든 회차 수
    # 생성기 첫 답에 정적 검사가 잡은 문제 (재요청으로 고쳤어도) — 검사가 실제로 일했는지 사후에 확인하려고.
    # 2026-11-09 데이터 알파 재검증: 보고서에 남지 않아 8회 동안 검사가 무엇을 잡았는지 알 수 없었다.
    static_retries: list[str] = field(default_factory=list)
    rejected: list[Overlay] = field(
        default_factory=list
    )  # 채택하지 않은 생성 출력 (산출물로만, git 밖)
    snapshots: list[dict[str, Overlay]] = field(
        default_factory=list
    )  # 검증한 회차마다 그때의 오버레이 (평가·디버깅용)
    # 채점에서 뺀 숨은 케이스와 그 뒤의 숨은 계획 — 변별하지 못하는 케이스 (RepairPlan.weak) [확정: 2026-11-11 사용자].
    # 저장·이미지는 hidden_plan 을 쓴다 (없으면 입력의 계획 그대로).
    dropped_cases: list[str] = field(default_factory=list)
    hidden_plan: HiddenPlan | None = None
    # 공개 테스트 변별 관문 (#164) — 마지막으로 돌린 변형 결과·버린 변형·변형 실행 보고서 (검증 회차 보고서와 따로)
    variants: list[VariantResult] = field(default_factory=list)
    variant_dropped: list[str] = field(default_factory=list)
    variant_reports: list[VerifyReport] = field(default_factory=list)
    # 숨은 테스트 함정 관문 (#171) — 허용 설계의 함정에 빠진 변형 결과·버린 변형·실행 보고서
    traps: list[TrapResult] = field(default_factory=list)
    trap_dropped: list[str] = field(default_factory=list)
    trap_reports: list[VerifyReport] = field(default_factory=list)
    # 손 구현 관문 (⑫, #256 3단계) — 손 구현마다 마지막 결과 한 줄·채점에 남긴 케이스(구하기)·실행 보고서
    hand: list[str] = field(default_factory=list)
    hand_kept: list[str] = field(default_factory=list)
    hand_reports: list[VerifyReport] = field(default_factory=list)
    # 실패한 빌드의 계약 탓 꼴에 붙인 손 허용 구현 실행 (#256 4단계) — 손 관문 보고서와 따로
    blame_reports: list[VerifyReport] = field(default_factory=list)
    # 조절값 보정 (#301 PR 2) — 빌드마다 한 번 (결과가 있으면 다시 하지 않는다)·조합마다 실행 보고서
    calibration: calib.Calibration | None = None
    calibration_reports: list[VerifyReport] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return round(sum(p.cost_usd or 0.0 for p in self.provenance), 4)


VARIANT_GATE = "⑩"  # 공개 테스트 변별 (#164)


def new_api_tests(contract: TaskContract) -> set[str]:
    """새 API 에만 걸린 공개 테스트 id — 시작 코드로 통과하면 새 기능이 들어 있다 (수리 ②', 2026-11-15 손 시범 coupons).
    이미 있는 API(existing)가 하나도 없으면 공개 테스트 전부. 있으면 그 공개 수용 기준(ac_id)을 refs 에 둔 엔드포인트가 모두 새 API 인 것만
    (엔드포인트 refs 가 r* 만이면 어느 API 의 테스트인지 몰라 세지 않는다 — 지금처럼 표시)."""
    if not any(e.existing for e in contract.endpoints):
        return {t.test_id for t in contract.public_tests}
    out: set[str] = set()
    for t in contract.public_tests:
        eps = [e for e in contract.endpoints if t.ac_id in e.refs]
        if eps and not any(e.existing for e in eps):
            out.add(t.test_id)
    return out


def _pt_ac(b: BuildInputs) -> dict[str, str]:
    return {t.test_id: t.ac_id for t in b.contract.public_tests}


def _overlay_digest(*overlays: Overlay) -> str:
    h = hashlib.sha256()
    for o in overlays:
        for f in sorted(o.files, key=lambda x: x.path):
            h.update(f.path.encode("utf-8") + b"\0" + f.content.encode("utf-8") + b"\0")
    return h.hexdigest()


async def _variant_gate(
    out: BuildOutcome,
    b: BuildInputs,
    kw: dict[str, object],
    verifier: Verifier,
    gate: VariantGate,
) -> RepairPlan:
    """공개 테스트 변별 관문 (#164) — 다른 게이트를 다 통과한 회차에서. 공개 수용 기준마다 정답 구현(공개 테스트를 모두 통과한
    허용 구현)에서 그 기준 하나만 어긴 변형을 만들어(gen_variant — 공개 테스트를 보지 않음) 공개 테스트로 돌린다.
    살아남은 변형이 있으면 공개 테스트 수리 행동 하나. 변형은 허용 구현이 바뀔 때만 다시 만든다."""
    plan = RepairPlan()
    run = getattr(verifier, "verify_variants", None)
    public = out.overlays.get(PUBLIC)
    if public is None or not out.reports:
        return plan  # 공개 테스트가 없는 템플릿
    if run is None:
        plan.flags.append(
            f"공개 테스트 변별 관문({VARIANT_GATE}) 생략함 — 검증기가 변형 실행(verify_variants)을 지원하지 않음"
        )
        return plan
    last = {r.target: r for r in out.reports[-1].runs}
    base_label = next(
        (
            k
            for k, o in out.overlays.items()
            if o.kind == "ref_accepted"
            and (r := last.get(k)) is not None
            and r.setup == "ok"
            and r.results("public")
            and all(t.result == "PASS" for t in r.results("public"))
        ),
        None,
    )
    if base_label is None:
        plan.flags.append(
            f"공개 테스트 변별 관문({VARIANT_GATE}) 생략함 — 공개 테스트를 모두 통과한 허용 구현이 없음"
        )
        return plan
    starter, base = out.overlays[STARTER], out.overlays[base_label]
    digest = _overlay_digest(starter, base)
    if gate.base_digest != digest:
        made = await gen_variants(b.task, b.contract, b.template, starter, base, **kw)  # type: ignore[arg-type]
        out.provenance += made.provenance
        gate.base_digest, gate.variants = digest, made.variants
        gate.dropped = [*made.dropped, *made.fail_reasons]
        gate.missing_boundary = list(made.missing_boundary)
        out.variant_dropped = list(gate.dropped)
    if not gate.variants:
        plan.flags.append(
            f"공개 테스트 변별 관문({VARIANT_GATE}): 사용할 수 있는 변형이 없음 — 공개 테스트가 규칙을 위반한 구현을 구별하는지 사람 확인"
            + (f" ({'; '.join(gate.dropped[:3])})" if gate.dropped else "")
        )
        return plan
    report = await run(starter, [o for o, _ in gate.variants], public)
    out.variant_reports.append(report)
    gate.results = judge(report, [s for _, s in gate.variants], pts_by_test(public), _pt_ac(b))
    judged = {r.rule_id for r in gate.results if r.verdict in ("caught", "survived")}
    plan.flags += [
        f"공개 테스트 변별 관문({VARIANT_GATE}): 요구 {t.rule_id} 는 사용할 수 있는 변형이 없어 변별 검사 못 함 — 사람 확인"
        for t in targets(b.task, b.contract)
        if t.rule_id not in judged
    ]
    plan.flags += [
        f"공개 테스트 변별 관문({VARIANT_GATE}): 요구 {r} 에 비교 규칙이 있음. 경계 변형을 생성하지 못함 — 경계를 구별하는지 사람 확인"
        for r in gate.missing_boundary
    ]
    gate.rounds += 1
    out.variants = list(gate.results)
    alive = survivors(gate.results)
    logger.info(
        "variant gate round=%d variants=%d caught=%d survived=%d invalid=%d",
        gate.rounds,
        len(gate.results),
        sum(r.verdict == "caught" for r in gate.results),
        len(alive),
        sum(r.verdict == "invalid" for r in gate.results),
    )
    if alive:
        test_ids: dict[str, list[str]] = {}
        for pt in b.contract.public_tests:
            test_ids.setdefault(pt.ac_id, []).append(pt.test_id)
        rule_acs = {t.rule_id: t.acs for t in targets(b.task, b.contract)}
        plan.actions.append(
            RepairAction(
                generator="gen_public",
                label="",
                gate=VARIANT_GATE,
                reason="규칙을 위반한 변형이 공개 테스트 통과: "
                + ", ".join(f"{r.rule_id} {OP_NAME[r.op]}" for r in alive),
                note=repair_note(
                    alive,
                    test_ids,
                    rule_acs,
                    timed=is_java(b.template.manifest) and bool(time_settings(b.contract)),
                ),
                pool=VARIANT_POOL,
            )
        )
    return plan


async def _trap_gate(
    out: BuildOutcome,
    b: BuildInputs,
    kw: dict[str, object],
    verifier: Verifier,
    gate: TrapGate,
    weak: Sequence[str] = (),
) -> RepairPlan:
    """숨은 테스트 함정 관문 (⑪, #171) — 다른 게이트를 다 통과한 회차에서. 허용 설계마다(함정이 적힌 것만) 그 설계 구현에서 함정에
    빠진 변형을 만들어(gen_trap — 숨은 테스트를 보지 않음) 공개·숨은 테스트로 돌린다. 숨은 케이스가 하나도 안 떨어지면 그 함정을 붙여
    그 숨은 요구의 케이스를 다시 만드는 행동 하나. 변형은 그 설계 구현이 바뀔 때만 다시 만든다."""
    plan = RepairPlan()
    run = getattr(verifier, "verify_variants", None)
    public, hidden = out.overlays.get(PUBLIC), out.overlays.get(HIDDEN)
    if hidden is None or not out.reports:
        return plan
    designs = list(b.task.accepted_designs)
    targets = {t.design_id: t for t in trap_targets(designs, b.plan)}
    plan.flags += [
        f"숨은 테스트 함정 관문({TRAP_GATE}): 허용 설계 {d} 조건에 '함정'이 있음. 함정 문장을 해석하지 못함 — 사람 확인"
        for d in unread_traps(designs)
    ]
    built = {k: o for k, o in out.overlays.items() if o.kind == "ref_accepted" and k in targets}
    if not built:
        return plan
    if run is None or public is None:
        plan.flags.append(
            f"숨은 테스트 함정 관문({TRAP_GATE}) 생략함 — 검증기가 변형 실행(verify_variants)을 지원하지 않음"
        )
        return plan
    starter = out.overlays[STARTER]
    taken = {s.label for vs in gate.variants.values() for _, s in vs}

    async def make(label: str, note: str | None) -> TrapGen:
        for _, s in gate.variants.get(label, []):
            taken.discard(s.label)
        made = await gen_traps(
            b.task,
            b.contract,
            b.template,
            starter,
            built[label],
            targets[label],
            taken=taken,
            note=note,
            **kw,  # type: ignore[arg-type]
        )
        out.provenance += made.provenance
        return made

    for label, design in built.items():
        digest = _overlay_digest(starter, design)
        if gate.digests.get(label) == digest:
            continue
        gate.retried.discard(label)
        made = await make(label, None)
        dropped = [*made.dropped, *made.fail_reasons]
        if not made.variants and made.dropped and not made.fail_reasons:
            # 쓸 변형이 하나도 없으면 버린 이유(줄 상한·시드 파일 등)를 붙여 한 번 더 — 상한은 그대로 (traps.dropped_note)
            made = await make(label, dropped_note(made.dropped))
            dropped += [*made.dropped, *made.fail_reasons]
        gate.variants[label] = made.variants
        if not made.fail_reasons:  # 호출이 실패했으면 다음 회차에 다시 (리뷰 2026-11-13: 일시 오류 한 번에 끝까지 시험 못 함)
            gate.digests[label] = digest
        gate.dropped = [x for x in gate.dropped if not x.startswith(f"{label} ")] + dropped
    plan.flags += [
        f"숨은 테스트 함정 관문({TRAP_GATE}): 허용 설계 {d} 의 함정 '{targets[d].trap}' 에 해당하는 변형을 생성하지 못함 — 숨은 테스트가 그 함정을 구별하는지 사람 확인"
        for d in built
        if not gate.variants.get(d)
    ]
    pairs = [x for vs in gate.variants.values() for x in vs]
    if not pairs:
        out.trap_dropped = list(gate.dropped)
        return plan

    async def judged(ps: list[tuple[Overlay, TrapSpec]]) -> list[TrapResult]:
        report = await run(starter, [o for o, _ in ps], public, hidden)
        out.trap_reports.append(report)
        results = judge_traps(report, [s for _, s in ps], weak)
        # 경합형 함정(조회 뒤 저장·읽은 뒤 갱신)은 한 번에 안 드러날 수 있다 — 살아남은 변형은 한 번 더 돌려 두 번 다 살아남아야 살아남음
        # (리뷰 2026-11-13: 한 번 돌리면 멀쩡한 숨은 테스트를 더 엄격하게 고쳐 다른 허용 설계를 떨어뜨리기 쉽다)
        first_alive = {r.label for r in trap_survivors(results)}
        if not first_alive:
            return results
        again = await run(starter, [o for o, s in ps if s.label in first_alive], public, hidden)
        out.trap_reports.append(again)
        rerun = {
            r.label: r
            for r in judge_traps(again, [s for _, s in ps if s.label in first_alive], weak)
        }
        return [
            rerun[r.label] if r.label in rerun and rerun[r.label].verdict == "caught" else r
            for r in results
        ]

    results = await judged(pairs)
    # 변형이 모두 무효인 설계(공개 테스트까지 깸 등)는 무효 이유를 붙여 한 번 다시 만들어 돌린다 — 그 구현이 바뀔 때까지 한 번 (traps.invalid_note)
    fresh: list[tuple[Overlay, TrapSpec]] = []
    for d in untested_designs(results, [d for d in built if gate.variants.get(d)]):
        if d in gate.retried:
            continue
        gate.retried.add(d)
        made = await make(d, invalid_note([r for r in results if r.design_id == d]))
        gate.dropped += [*made.dropped, *made.fail_reasons]
        if made.variants:
            gate.variants[d] = made.variants
            fresh += made.variants
    if fresh:
        redone = {s.design_id for _, s in fresh}
        results = [r for r in results if r.design_id not in redone] + await judged(fresh)
        pairs = [x for vs in gate.variants.values() for x in vs]
    out.trap_dropped = list(gate.dropped)
    gate.results = results
    gate.rounds += 1
    out.traps = list(gate.results)
    plan.flags += [
        f"숨은 테스트 함정 관문({TRAP_GATE}): 허용 설계 {d} 의 함정 변형이 모두 무효라 시험하지 못함 — 숨은 테스트가 그 함정을 구별하는지 사람 확인 "
        f"({'; '.join(r.detail for r in results if r.design_id == d)[:200]})"
        for d in untested_designs(results, [d for d in built if gate.variants.get(d)])
    ]
    specs = {s.label: s for _, s in pairs}
    alive_all = trap_survivors(gate.results)
    # 그 숨은 요구의 케이스를 찾지 못한 설계는 고치지 않는다 — 케이스 id 가 없으면 숨은 테스트 전체를 다시 만든다 (리뷰 2026-11-13)
    alive = [r for r in alive_all if specs[r.label].cases]
    plan.flags += _trap_flags(
        [r for r in alive_all if not specs[r.label].cases],
        "그 숨은 요구의 케이스를 발견하지 못해 수정하지 않음",
    )
    logger.info(
        "trap gate round=%d variants=%d caught=%d survived=%d invalid=%d",
        gate.rounds,
        len(gate.results),
        sum(r.verdict == "caught" for r in gate.results),
        len(alive),
        sum(r.verdict == "invalid" for r in gate.results),
    )
    if alive:
        plan.actions.append(
            RepairAction(
                generator="gen_hidden",
                label="",
                gate=TRAP_GATE,
                reason="허용 설계의 함정에 해당하는 변형이 숨은 테스트 통과: "
                + ", ".join(f"{r.design_id}" for r in alive),
                note=trap_note(alive, [s for _, s in pairs]),
                pool=TRAP_POOL,
            )
        )
    return plan


def _trap_flags(alive: Sequence[TrapResult], why: str) -> list[str]:
    return [
        f"숨은 테스트가 허용 설계의 함정을 구별하지 못함 ({why}) — {r.line()} — 행동 기준의 그 함정을 숨은 테스트로 측정하지 못함. "
        "변형이 함정에 실제로 해당하지 않았을 수도 있다 — 사람 확인"
        for r in alive
    ]


@dataclass(frozen=True)
class _Passed:
    """관문(함정 ⑪·손 구현 ⑫)이 테스트를 다시 만들기 직전 — 모든 게이트를 통과한 오버레이·뺄 숨은 케이스·그때의 함정 결과.
    숨은 테스트는 공개 테스트처럼 '더하기만'으로 고칠 수 없어(케이스 파일을 바꾼다) 수리가 실패하면 이 상태로 되돌린다 — 관문은 막지 않는다."""

    overlays: dict[str, Overlay]
    weak: tuple[str, ...]
    traps: list[TrapResult]
    gate: str = TRAP_GATE  # 다시 만든 관문 — 되돌릴 때 표시에


def _restore_passed(out: BuildOutcome, p: _Passed, why: str) -> None:
    """관문 수리 전 통과 상태로 — 변별 못 하는 숨은 케이스는 부르는 쪽이 손 구현 관문 뒤에 뺀다 (_settle_weak)."""
    out.overlays = dict(p.overlays)
    out.traps = list(p.traps)
    out.snapshots.append(dict(out.overlays))  # 마지막 스냅샷이 끝난 상태와 같게 (리뷰 2026-11-13)
    if p.gate == HAND_GATE:
        out.review_flags.append(
            f"{HAND_NAME} 수리 실패 — {why}. 관문 전(모든 게이트 통과) 상태로 복원함"
        )
        return
    out.review_flags.append(
        f"숨은 테스트 함정 관문({TRAP_GATE}) 수리 실패 — {why}. 관문 전(모든 게이트 통과) 숨은 테스트로 복원함"
    )
    out.review_flags += _trap_flags(trap_survivors(p.traps), "숨은 테스트 수리 실패")


async def _finish_restored(
    out: BuildOutcome,
    b: BuildInputs,
    kw: dict[str, object],
    verifier: Verifier,
    gate: VariantGate,
    hgate: HandGate,
    passed: _Passed,
    why: str,
    compiler: TestCompiler | None,
) -> BuildOutcome:
    """관문 수리 전 통과 상태로 되돌리고 공개 테스트 관문(⑩)·손 구현 관문(⑫, 수리 없이)까지 마쳐 review 로 끝낸다."""
    _restore_passed(out, passed, why)
    vplan = await _variant_gate(out, b, kw, verifier, gate)
    out.review_flags += [f for f in vplan.flags if f not in out.review_flags]
    if vplan.actions:
        out.review_flags += await _add_public_cases(
            out, b, kw, verifier, gate, vplan.actions[0], compiler
        )
    await _hand_only(out, b, verifier, hgate, "수리 뒤 다른 게이트 실패로 되돌린 상태")
    _settle_weak(out, b, passed.weak, hgate)
    out.status = "review"
    return out


async def _hand_gate(
    out: BuildOutcome, b: BuildInputs, verifier: Verifier, gate: HandGate
) -> RepairPlan:
    """손 구현 관문 (⑫, #256 3단계) — 다른 게이트를 다 통과한 회차의 마지막 (함정 ⑪·변별 ⑩ 뒤라 변별 관문이 더한 공개 테스트
    클래스까지 본다). 재료 손 구현을 지금 공개·숨은 테스트에 돌려 starter/hand_refs.judge_hands 로 판정한다 (LLM 호출 없음).
    수리는 공개 테스트 하나·숨은 테스트 하나로 합친다 (_regenerate 는 생성기마다 요청 하나). 손 구현이 없는 재료는 그대로 지나간다."""
    plan = RepairPlan()
    refs = b.references
    if refs is None:
        return plan
    run = getattr(verifier, "verify_variants", None)
    public, hidden = out.overlays.get(PUBLIC), out.overlays.get(HIDDEN)
    off = (
        refs.stale
        or ("검증기가 변형 실행(verify_variants)을 지원하지 않음" if run is None else "")
        or ("공개 테스트가 없음" if public is None else "")
    )
    if off or hidden is None or run is None or public is None:
        if off and not gate.off_noted:
            gate.off_noted = True
            plan.flags.append(f"{HAND_NAME} 생략 — {off}")
        return plan
    report = await run(out.overlays[STARTER], refs.overlays(), public, hidden)
    out.hand_reports.append(report)
    j = judge_hands(report, refs, b.plan)
    gate.findings, gate.keep = list(j.findings), j.keep
    gate.rounds += 1
    out.hand = list(j.lines)
    plan.flags += j.flags
    logger.info(
        "hand gate round=%d findings=%d keep=%s",
        gate.rounds,
        len(j.findings),
        ",".join(sorted(j.keep)),
    )
    pub = [f for f in j.findings if f.generator == "gen_public"]
    hid = [f for f in j.findings if f.generator == "gen_hidden"]
    if pub:
        plan.actions.append(
            RepairAction(
                "gen_public",
                "",
                HAND_GATE,
                "재료 손 구현이 짚은 공개 테스트: " + ", ".join(dict.fromkeys(f.test for f in pub)),
                public_note(
                    pub, guide_public_rules(b.template)
                ),  # 재료 안내의 공개 테스트 입력 규칙
                pool=HAND_POOL,
            )
        )
    if hid:
        cases = tuple(dict.fromkeys(c for f in hid for c in f.cases))
        plan.actions.append(
            RepairAction(
                "gen_hidden",
                "",
                HAND_GATE,
                "재료 손 구현이 짚은 숨은 케이스: " + (", ".join(cases) or "숨은 테스트 전체"),
                hidden_note(hid),
                pool=HAND_POOL,
                cases=cases,
            )
        )
    return plan


def _hand_skipped(
    out: BuildOutcome,
    gate: HandGate,
    actions: Sequence[RepairAction],
    why: str,
    *,
    record: bool = True,
) -> None:
    """고치지 못한 손 구현 관문 행동 — 수리 기록(skipped)과 그 생성기의 발견을 승인 화면 표시로 (막지 않는다)."""
    gens = {a.generator for a in actions}
    if record:
        out.repairs += [_record(a, "skipped") for a in actions]
    out.review_flags += [
        f
        for f in finding_flags([x for x in gate.findings if x.generator in gens], why)
        if f not in out.review_flags
    ]


async def _hand_only(
    out: BuildOutcome, b: BuildInputs, verifier: Verifier, gate: HandGate, why: str
) -> None:
    """수리 없이 손 구현 관문 — 되돌린 상태로 끝낼 때 표시·구하기 판정만 (수리 기록은 _regenerate 가 이미 남겼다)."""
    hplan = await _hand_gate(out, b, verifier, gate)
    out.review_flags += [f for f in hplan.flags if f not in out.review_flags]
    if hplan.actions:
        _hand_skipped(out, gate, hplan.actions, why, record=False)


def _settle_weak(out: BuildOutcome, b: BuildInputs, weak: Sequence[str], gate: HandGate) -> None:
    """변별 못 하는 숨은 케이스를 채점에서 뺀다 — 손 구현이 의도한 사유로 떨어뜨리는 케이스는 남기고 '채점에서 제외' 표시를 거둔다
    (#256 3단계: kb002 r1 hc1 처럼 생성 흔한 구현만 보고 사유 하나를 통째로 빼지 않게)."""
    kept = [c for c in weak if c in gate.keep]
    out.hand_kept = kept
    if kept:
        out.review_flags = [
            f
            for f in out.review_flags
            if not (WEAK_FLAG in f and any(f.startswith(f"숨은 케이스 {c} 는 ") for c in kept))
        ]
        out.review_flags.append(
            f"{HAND_NAME}: 숨은 케이스 {', '.join(kept)} 채점 유지 — 생성 흔한 구현은 통과, 재료 손 구현이 의도한 사유로 실패 (변별함)"
        )
    dropped = [c for c in weak if c not in gate.keep]
    if dropped:
        _drop_cases(out, b, dropped)


def _survivor_flags(alive: Sequence[VariantResult], why: str) -> list[str]:
    return [
        f"공개 테스트가 규칙을 위반한 구현을 구별하지 못함 ({why}) — {r.line()} — 그 기준은 공개 테스트로 측정하지 못함. "
        "변형이 규칙을 실제로 위반하지 않았을(동치) 수도 있고, 시드에 구별할 행이 없으면 계약 rows 부터, "
        "시간 규칙이면 경계 ±1초 안에서 재지 않았는지(정수 초 구현은 최대 1초 늦게 만료) — 사람 확인"
        for r in alive
    ]


def _class_names(f: GeneratedFile) -> set[str]:
    return {PurePosixPath(f.path).stem, *re.findall(r"\bclass\s+(\w+)", f.content)}


async def _add_public_cases(
    out: BuildOutcome,
    b: BuildInputs,
    kw: dict[str, object],
    verifier: Verifier,
    gate: VariantGate,
    action: RepairAction,
    compiler: TestCompiler | None,
) -> list[str]:
    """관문(⑩) 수리 — 기존 공개 테스트는 그대로 두고, 살아남은 변형이 있는 요구마다 **새 테스트 클래스만** 더한다 [2026-11-13 사용자].
    공개 테스트 전체를 다시 만들면 맞던 테스트가 깨질 수 있었다 (N+1 알파: 정답 구현도 떨어지는 테스트). 더한 클래스는
    (1) 기존 파일을 고친 답은 버리고 새 파일만, (2) 채점 코드와 함께 컴파일, (3) 참조 구현(흔한·허용)이 모두 통과하는 클래스만 남기고,
    (4) 변형을 다시 돌려 판정한다. 못 더하면 그대로 두고 표시 — 이미 통과한 빌드를 깨지 않는다. 돌려주는 값 = 승인자 표시."""
    run = getattr(verifier, "verify_variants", None)
    if run is None:  # _variant_gate 가 있음을 확인한 뒤에만 불린다
        return []
    starter, old = out.overlays[STARTER], out.overlays[PUBLIC]
    have = {f.path: f.content for f in old.files}
    alive = survivors(gate.results)
    test_compile = getattr(compiler, "compile_tests", None) if compiler is not None else None
    note = f"검증 실패 (게이트 {VARIANT_GATE}) — {action.note}"
    previous = old
    for attempt in range(COMPILE_TEST_ATTEMPTS + 1):
        r = await gen_public(
            b,
            note=note,
            previous=previous,
            **kw,  # type: ignore[arg-type]
        )
        out.provenance += r.provenance
        added = (
            [f for f in r.overlay.files if f.path not in have and f.content.strip()]
            if r.overlay is not None
            else []
        )
        touched = (
            [f.path for f in r.overlay.files if f.path in have and f.content != have[f.path]]
            if r.overlay is not None
            else []
        )
        if touched:
            out.adjustments.append(
                f"gen_public({VARIANT_GATE}): 기존 공개 테스트 {len(touched)}개를 고친 답은 버림 — 새 클래스만 더함"
            )
        if not added:
            out.repairs.append(_record(action, "failed"))
            return _survivor_flags(alive, "추가할 테스트를 생성하지 못함")
        candidate = replace(old, files=(*old.files, *added))
        errors = await test_compile(out.overlays[HIDDEN], candidate) if test_compile else []
        if not errors:
            break
        if attempt == COMPILE_TEST_ATTEMPTS:
            out.repairs.append(_record(action, "failed"))
            e = errors[0]
            return [
                f"공개 테스트 변별 관문({VARIANT_GATE}): 추가한 테스트를 컴파일하지 못해 제외함 — {e.path}:{e.line} {e.message}"[
                    :300
                ],
                *_survivor_flags(alive, "추가한 테스트를 컴파일하지 못함"),
            ]
        # 더한 테스트의 컴파일 오류를 붙여 다시 — 채점 코드 컴파일 수리(compile_and_fix)와 같은 횟수.
        # 2026-11-13 재시도 중복 알파(naver_2026#002): 경계 변형(0 이하 → 0 미만)을 가를 테스트가 `cannot find symbol` 하나로 버려졌다
        note = (
            compile_note(errors, candidate)
            + f"\n\n더하려던 이유 (게이트 {VARIANT_GATE}) — {action.note}"
        )
        previous = candidate
    refs = [o for o in out.overlays.values() if o.kind in ("ref_naive", "ref_accepted")]
    check = await run(starter, refs, candidate)
    out.variant_reports.append(check)
    if any(x.setup != "ok" for x in check.runs):
        out.repairs.append(_record(action, "failed"))
        return _survivor_flags(alive, "추가한 테스트로 참조 구현이 기동하지 못함")
    failing = {t.name for x in check.runs for t in x.results("public") if t.result != "PASS"}
    keep = [f for f in added if not (_class_names(f) & failing)]
    flags: list[str] = []
    if len(keep) < len(added) or (failing - {n for f in added for n in _class_names(f)}):
        dropped = [f.path for f in added if f not in keep]
        flags.append(
            f"공개 테스트 변별 관문({VARIANT_GATE}): 추가한 테스트 중 참조 구현이 실패하는 것은 제외함 — {', '.join(dropped) or '(기존 테스트가 실패함 — 추가한 것 모두 제외함)'}"
        )
        if failing - {n for f in added for n in _class_names(f)}:
            keep = []  # 더한 파일로 기존 테스트가 깨짐 (도우미 공유 등) — 모두 버린다
    if not keep:
        out.repairs.append(_record(action, "failed"))
        return [*flags, *_survivor_flags(alive, "추가한 테스트를 사용할 수 없음")]
    final = replace(old, files=(*old.files, *keep))
    again = await run(starter, [o for o, _ in gate.variants], final)
    out.variant_reports.append(again)
    gate.results = judge(again, [s for _, s in gate.variants], pts_by_test(final), _pt_ac(b))
    out.variants = list(gate.results)
    out.overlays[PUBLIC] = final
    out.repairs.append(_record(action, "regenerated"))
    out.adjustments.append(
        f"gen_public({VARIANT_GATE}): 공개 테스트 {len(keep)}개 클래스 더함 — {', '.join(f.path.rsplit('/', 1)[-1] for f in keep)}"
    )
    return [*flags, *_survivor_flags(survivors(gate.results), "테스트를 추가한 뒤에도")]


def _record(
    a: RepairAction, result: Literal["regenerated", "failed", "skipped", "kept-previous"]
) -> RepairRecord:
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


SIGNAL_FLAG = "다른 관문 신호: "  # 실패한 빌드의 마지막 검증에서 본 계약 탓 꼴 (#256 4단계)
MAX_SIGNALS = 4  # 승인·실패 화면 표시 상한 — 넘으면 개수만


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
    """빌드 루프 (_build_and_repair) — 실패로 끝나면 어느 관문에서 멈췄든 마지막 검증의 계약 탓 꼴(흔한 구현만 통과하는 숨은 케이스,
    모든 구현이 같은 이유로 떨어지는 테스트)을 표시로 남긴다. 손 구현이 있는 재료는 손 허용 구현 결과를 근거로 붙인다 (#256 4단계)."""
    out = await _build_and_repair(
        b,
        designs,
        llm=llm,
        prompts=prompts,
        ontology_version=ontology_version,
        verifier=verifier,
        max_rounds=max_rounds,
        compiler=compiler,
    )
    if out.status == "failed" and out.reports:
        signals = contract_signals(out.reports[-1], b.plan)
        shown = signals[:MAX_SIGNALS]
        evidence = await _hand_evidence(out, b, verifier, shown) if shown else {}
        flags = [f"{SIGNAL_FLAG}{s.line()}{evidence.get((s.group, s.name), '')}" for s in shown]
        if len(signals) > MAX_SIGNALS:
            flags.append(
                f"{SIGNAL_FLAG}외 {len(signals) - MAX_SIGNALS}건 (보고서의 마지막 검증 참고)"
            )
        out.review_flags += [f for f in flags if f not in out.review_flags]
    return out


async def _hand_evidence(
    out: BuildOutcome, b: BuildInputs, verifier: Verifier, signals: Sequence[ContractSignal]
) -> dict[tuple[str, str], str]:
    """계약 탓 꼴마다 손 허용 구현 결과 — 손 허용이 통과하면 테스트는 올바른 구현으로 통과할 수 있어 생성 구현 결함, 같이 떨어지면
    테스트·계약 결함. 신호와 같은 판으로 — 마지막 검증 때의 오버레이 스냅샷 (그 뒤 다시 만든 산출물이 아니라). 실패해도 빌드 결과는 그대로."""
    refs = b.references
    run = getattr(verifier, "verify_variants", None)
    snap = out.snapshots[-1] if out.snapshots else out.overlays
    starter, hidden, public = (snap.get(k) for k in (STARTER, HIDDEN, PUBLIC))
    if refs is None or refs.stale or run is None or starter is None or hidden is None:
        return {}
    if public is None:
        return {}
    accepted = [o for o in refs.overlays() if o.kind == "ref_accepted"]
    if not accepted:
        return {}
    try:
        report = await run(starter, accepted, public, hidden)
    except Exception as exc:  # 근거 모으기 실패는 빌드 결과를 바꾸지 않는다
        logger.warning("hand evidence failed %s", type(exc).__name__)
        return {}
    out.blame_reports.append(report)
    runs = [r for r in report.runs if r.setup == "ok"]
    evidence: dict[tuple[str, str], str] = {}
    for s in signals:
        got = [
            t
            for r in runs
            if (t := next((x for x in r.tests if x.group == s.group and x.name == s.name), None))
            is not None
        ]
        if not got:
            continue
        if all(t.result == "PASS" for t in got):
            evidence[(s.group, s.name)] = (
                f" · 손 허용 구현 {len(got)}개 통과 — 테스트는 올바른 구현으로 통과 가능, 생성 구현 결함 의심"
            )
        elif all(t.result != "PASS" for t in got):
            evidence[(s.group, s.name)] = (
                f" · 손 허용 구현 {len(got)}개도 실패 — 테스트·계약 결함 의심"
            )
        else:
            evidence[(s.group, s.name)] = " · 손 허용 구현 일부만 실패"
    return evidence


async def _build_and_repair(
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
    gate = VariantGate()  # 공개 테스트 변별 관문 상태 (#164)
    tgate = TrapGate()  # 숨은 테스트 함정 관문 상태 (#171)
    hgate = HandGate()  # 손 구현 관문 상태 (#256 3단계)
    passed: _Passed | None = None  # 함정 관문이 숨은 테스트를 다시 만들기 직전의 통과 상태

    def note_static(r: GenResult) -> None:
        if r.first_problems:
            label = f"({r.overlay.label})" if r.overlay is not None and r.overlay.label else ""
            out.static_retries.append(
                f"{r.task}{label}: " + " | ".join(p[:160] for p in r.first_problems[:4])
            )

    def took(r: GenResult) -> Overlay | None:
        note_static(r)
        out.provenance += r.provenance
        out.adjustments += [f"{r.task}: {a}" for a in r.adjustments]
        out.rejected += r.rejected
        for cid, probs in r.kept.items():
            out.review_flags.append(
                f"숨은 케이스 {cid} 를 다시 생성하지 못해 이전 파일을 그대로 유지함 (재요청 뒤에도): "
                + "; ".join(p[:160] for p in probs[:2])
            )
        if r.overlay is not None and not r.ok:
            out.rejected.append(r.overlay)
        if r.overlay is None or not r.ok:
            out.review_flags.append(
                f"{r.task} 생성 실패: " + "; ".join([*r.problems, *r.fail_reasons][:3])
            )
            return None
        return r.overlay

    def absorb(fix: ImplFix | CompileFix) -> None:
        for r in fix.results:
            note_static(r)
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
            f"허용 설계 {label} 제외 — 구현이 {HARNESS_OVERRIDE}에 의존함 (재료의 허용 설계 확인)"
        )
        return True

    # 계약 적합성 스모크 (게이트 ⓪) — 계약에서 코드가 쓴다. 시작 코드·참조 구현을 계약 모양 응답으로 두드려 탓을 증거로 정한다
    conform_files = render_conformance(b.contract, b.template.manifest)
    conform = (
        Overlay(kind="public_test", files=tuple(conform_files), label="conform")
        if conform_files
        else None
    )

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
                **({"conform": conform} if conform is not None else {}),
            )
            out.reports.append(report)
            _withdraw_compile_flags(out, report)
            _note_passed(out, report)
            out.snapshots.append(dict(out.overlays))
            shared = set(b.template.files("project")) | {
                f.path for f in out.overlays[STARTER].files
            }  # 이미 있는 파일 — 모든 구현이 다시 쓰면 원인 후보
            assign_now = partial(
                assign,
                report,
                b.plan,
                out.overlays,
                missed,
                out.regen_counts,
                shared,
                terms=b.template.manifest.repair_terms,
                fix_markers=b.seed.fix_markers if b.seed is not None else (),
                feature_reasons=b.seed.reasons if b.seed is not None else (),
                regen_log=out.regen_log,
                passed_before=out.passed_before,
                new_tests=new_api_tests(b.contract),
            )
            # 조절값 보정 (#301 PR 2) — 다시 검증할 회차가 남아 있을 때만 (보정한 값은 다음 회차의 전체 검증이 판정한다, ③)
            tunable = _calibratable(out, b, verifier, report) if round_no < max_rounds else ()
            plan = assign_now(calibrate=tunable)
            if plan.calibrate:
                cal = out.calibration = await _calibrate(out, b, verifier, plan.calibrate)
                if cal.status == "calibrated":
                    b = _apply_calibration(out, b, cal)
                    continue
                # 못 맞춤 — 보정 없이 다시 배정한다 (흔한 구현이 안 떨어짐·돌릴 수 없음은 기존 재생성 경로, 창 없음은 재료 판정으로 멈춤)
                plan = assign_now()
                if cal.status == "no_window":
                    plan.unresolved.append(MATERIAL_VERDICT + cal.verdict())
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
            tplan = await _trap_gate(out, b, kw, verifier, tgate)
            out.review_flags += [f for f in tplan.flags if f not in out.review_flags]
            if tplan.actions:
                a = tplan.actions[0]
                tried = out.regen_counts.get(a.budget_key, 0) >= a.pool.limit
                if not tried and round_no < max_rounds:
                    passed = _Passed(dict(out.overlays), tuple(plan.weak), list(out.traps))
                    if not await _regenerate(
                        tplan.actions, out, b, by_id, kw, took, drop, compiler, absorb
                    ):
                        _restore_passed(out, passed, "숨은 테스트를 다시 생성하지 못함")
                        passed = None
                    else:
                        continue
                else:
                    out.repairs.append(_record(a, "skipped"))
                    out.review_flags += _trap_flags(
                        trap_survivors(out.traps),
                        "숨은 테스트를 다시 생성한 뒤에도"
                        if tried
                        else "빌드 회차 상한으로 숨은 테스트를 다시 생성하지 못함",
                    )
            passed = None
            vplan = await _variant_gate(out, b, kw, verifier, gate)
            out.review_flags += [f for f in vplan.flags if f not in out.review_flags]
            if vplan.actions:  # 살아남은 변형 — 기존 공개 테스트는 두고 새 테스트 클래스만 더한다 (회차를 쓰지 않음)
                out.review_flags += await _add_public_cases(
                    out, b, kw, verifier, gate, vplan.actions[0], compiler
                )
            # 손 구현 관문 (⑫, #256 3단계) — 마지막 공개 테스트(변별 관문이 더한 클래스 포함)·숨은 테스트로. 수리는 생성기마다 1회,
            # 다시 만든 테스트가 다른 게이트를 깨고 못 고치면 이 상태(함정·변별 관문을 거친)로 되돌린다
            hplan = await _hand_gate(out, b, verifier, hgate)
            out.review_flags += [f for f in hplan.flags if f not in out.review_flags]
            fresh = [
                a for a in hplan.actions if out.regen_counts.get(a.budget_key, 0) < a.pool.limit
            ]
            spent = [a for a in hplan.actions if a not in fresh]
            if spent:
                _hand_skipped(out, hgate, spent, "다시 생성 1회 뒤에도")
            if fresh and round_no < max_rounds:
                passed = _Passed(dict(out.overlays), tuple(plan.weak), list(out.traps), HAND_GATE)
                if await _regenerate(fresh, out, b, by_id, kw, took, drop, compiler, absorb):
                    continue
                _restore_passed(out, passed, "테스트를 다시 생성하지 못함")
                passed = None
                _hand_skipped(out, hgate, fresh, "다시 생성 실패", record=False)
            elif fresh:
                _hand_skipped(out, hgate, fresh, "빌드 회차 상한으로 다시 생성하지 못함")
            _settle_weak(out, b, plan.weak, hgate)
            out.status = "review"
            return out
        blocked = [a for a in plan.actions if out.regen_counts.get(a.budget_key, 0) >= a.pool.limit]
        if plan.unresolved or blocked or round_no == max_rounds:
            if (
                passed is not None
            ):  # 관문(함정·손 구현) 수리 뒤 고치지 못하고 끝남 — 관문 전 통과 상태로 (관문은 막지 않는다)
                out.repairs += [_record(a, "skipped") for a in plan.actions]
                return await _finish_restored(
                    out,
                    b,
                    kw,
                    verifier,
                    gate,
                    hgate,
                    passed,
                    "다시 생성한 테스트가 다른 게이트를 실패시켜 수정하지 못함",
                    compiler,
                )
            out.review_flags += plan.unresolved
            out.review_flags += [f"재생성 예산을 소진함 — {a.key}: {a.reason}" for a in blocked]
            if round_no == max_rounds and not (plan.unresolved or blocked):
                out.review_flags.append(
                    f"빌드 전체 재생성 상한({max_rounds}회) — 잔여 실패를 사람이 확인"
                )
            out.repairs += [_record(a, "skipped") for a in plan.actions]
            return out
        if not await _regenerate(plan.actions, out, b, by_id, kw, took, drop, compiler, absorb):
            if passed is not None:
                return await _finish_restored(
                    out,
                    b,
                    kw,
                    verifier,
                    gate,
                    hgate,
                    passed,
                    ("손 구현" if passed.gate == HAND_GATE else "함정")
                    + " 관문 수리 뒤 다른 게이트 수리가 실패",
                    compiler,
                )
            return out
    return out


def _calibratable(
    out: BuildOutcome, b: BuildInputs, verifier: Verifier, report: VerifyReport
) -> tuple[str, ...]:
    """조절값을 보정해 볼 수 있는 숨은 케이스 (#301 PR 2) — 빌드마다 한 번, 검증기가 변형 실행(verify_variants)을 지원하고 공개
    테스트가 있고, 허용 구현이 지금 모두 통과할 때만 (허용 구현이 이미 떨어지면 창을 잴 수 없다). 그 사유에 보정할 숨긴 조절값이
    있는 케이스 (calibrate.knobs — 재료가 나열한 값은 빼고)."""
    if (
        out.calibration is not None
        or getattr(verifier, "verify_variants", None) is None
        or PUBLIC not in out.overlays
        or not calib.accepted_clean(report)
    ):
        return ()
    listed = (b.seed.harness or ()) if b.seed is not None else ()
    m = b.template.manifest
    return tuple(
        c.case_id
        for c in b.plan.cases
        if calib.knobs(c.intended_reason, m, b.plan, listed) is not None
    )


async def _calibrate(
    out: BuildOutcome, b: BuildInputs, verifier: Verifier, request: Sequence[tuple[str, str]]
) -> calib.Calibration:
    """조절값 보정 (#301 PR 2) — 요청한 케이스의 사유 조절값만 바꿔 흔한 구현·허용 구현을 시작 코드·공개·숨은 테스트와 다시 돌린다
    (verify_variants — 시작 코드 단독 실행 없이, 조합마다 숨은 오버레이의 조절값 파일만 바꿈). 한 번에 한 사유 (조절값이 사유마다 다르다)."""
    run = getattr(verifier, "verify_variants", None)
    starter, hidden, public = (out.overlays.get(k) for k in (STARTER, HIDDEN, PUBLIC))
    intended = {c.case_id: c.intended_reason for c in b.plan.cases}
    if run is None or starter is None or hidden is None or public is None or not request:
        return calib.Calibration(
            "skipped", why="검증기 변형 실행·시작 코드·숨은·공개 테스트 중 없는 것이 있음"
        )
    reason = intended.get(request[0][1], "")
    request = [(t, c) for t, c in request if intended.get(c) == reason]
    listed = (b.seed.harness or ()) if b.seed is not None else ()
    k = calib.knobs(reason, b.template.manifest, b.plan, listed)
    if k is None:
        return calib.Calibration("skipped", why=f"{reason} 에 보정할 조절값이 없음")
    cal = await calib.calibrate_refs(
        run,
        starter,
        [o for key, o in out.overlays.items() if key not in (STARTER, HIDDEN, PUBLIC)],
        public,
        hidden,
        b.plan,
        b.template.manifest,
        k,
        request,
        b.seed.reasons if b.seed is not None else (),
        out.calibration_reports,
    )
    logger.info(
        "build calibrate reason=%s status=%s trials=%d values=%s",
        reason,
        cal.status,
        len(cal.trials),
        dict(cal.values),
    )
    return cal


def _apply_calibration(out: BuildOutcome, b: BuildInputs, cal: calib.Calibration) -> BuildInputs:
    """보정한 값을 빌드의 숨은 계획에 넣은 BuildInputs — b.plan 을 바꿔야 뒤의 숨은 테스트 재생성(generate 의 조절값 파일)·정적 검사
    (조절값 파일 대조)·케이스 빼기(_drop_cases)가 같은 값을 본다. 저장·이미지는 out.hidden_plan 을 쓰므로(steps: out.hidden_plan or
    계약 계획) 함께 바꾸고, 숨은 오버레이의 조절값 파일도 다시 쓴다. 승인 화면에 보정한 값을 남긴다."""
    harness = {**b.plan.harness, **cal.values}
    nb = replace(b, plan=b.plan.model_copy(update={"harness": harness}))
    out.hidden_plan = (out.hidden_plan or b.plan).model_copy(update={"harness": harness})
    out.overlays[HIDDEN] = calib.with_params(out.overlays[HIDDEN], nb.plan, nb.template.manifest)
    # 명세 글이 같은 조절값을 다른 수치로 적었으면 함께 알린다 (2026-11-25 #308: 보정 350ms ↔ 2단계 로그 3000ms)
    texts = calib.spec_texts(b.task)
    said = "".join(
        calib.spec_note(name, v, calib.spec_values(texts, name, nb.template.manifest))
        for name, v in cal.values.items()
    )
    if (flag := cal.flag() + said) not in out.review_flags:
        out.review_flags.append(flag)
    return nb


def _drop_cases(out: BuildOutcome, b: BuildInputs, ids: Sequence[str]) -> None:
    """변별하지 못하는 숨은 케이스를 채점에서 뺀다 — 숨은 계획에서 지우고 그 케이스만의 숨은 테스트 파일도 지운다 (공용 파일은 둔다).
    같은 숨은 요구는 다른 케이스가 의도한 사유로 잡으므로(repair._gate3) 숨은 요구마다 케이스가 하나 이상 남는다."""
    drop = set(ids)
    rx = re.compile(b.template.manifest.hidden_rules.case_id_pattern)
    all_ids = [c.case_id for c in b.plan.cases]
    hidden = out.overlays[HIDDEN]
    out.overlays[HIDDEN] = replace(
        hidden, files=tuple(f for f in hidden.files if case_owner(f, rx, all_ids) not in drop)
    )
    out.hidden_plan = b.plan.model_copy(
        update={"cases": [c for c in b.plan.cases if c.case_id not in drop]}
    )
    out.dropped_cases = sorted(drop)
    logger.info("build dropped weak cases=%s", ",".join(sorted(drop)))


# 숨은 테스트 수리 중 이전 파일이 올바른 구현에 맞는 사유 게이트 (흔한 구현을 못 가름) — 게이트 문자열 집합. 예전에는 글자 집합
# frozenset("③'") 이라 글자 단위 비교로 우연히 맞았다 (#183). 합친 수리의 이어 붙은 게이트(예: "③④")는 gates_of 로 나눠 비교한다.
# ③' 은 하나만 놓친 흔한 구현과 함께 지웠다 (#243).
KEEP_GATES = frozenset({"③"})


def _keepable_cases(actions: Sequence[RepairAction], out: BuildOutcome) -> frozenset[str]:
    """숨은 테스트 수리에서 재요청 뒤에도 문제가 남으면 이전 파일을 둘 케이스 — 숨은 테스트 사유 게이트가 모두 ③ 이고, 직전 검증에서
    허용 구현이 모두 그 케이스를 통과(PASS)한 것만. 이전 파일이 올바른 구현에 맞고 흔한 구현을 못 가를 뿐이면, 남겨도 다음 회차에
    '변별 못 하는 케이스 → 사람 확인'으로 넘어간다 (2026-11-11 재측정 gen-item).
    허용 구현을 떨어뜨린 케이스(④)·setup·static·컴파일 수리·테스트 예외는 남기지 않는다 — 2026-11-11 재검증 오프라인: 허용 구현이 모두
    실패하는 hc1 을 남겨 4회차에 같은 실패가 되풀이됐다."""
    gates = [a.gate for a in actions if a.generator == "gen_hidden"]
    if not gates or not all(set(gates_of(g)) <= KEEP_GATES for g in gates) or not out.reports:
        return frozenset()
    accepted = [r for r in out.reports[-1].runs if r.kind == "ref_accepted"]
    ids = {t.name for r in accepted for t in r.tests if t.group == "hidden"}
    return frozenset(
        cid
        for cid in ids
        if accepted and all((t := r.case(cid)) is not None and t.result == "PASS" for r in accepted)
    )


async def _regenerate(
    actions: list[RepairAction],
    out: BuildOutcome,
    b: BuildInputs,
    by_id: dict[str, AcceptedDesign],
    kw: dict[str, object],
    took,  # type: ignore[no-untyped-def]
    drop,  # type: ignore[no-untyped-def]
    compiler: TestCompiler | None = None,
    absorb: Callable[[ImplFix | CompileFix], None] | None = None,
) -> bool:
    """배정된 생성기만 다시 — 자기 이전 출력을 주고 고칠 파일만 받는다 (프롬프트가 지원하면, generate._generate).
    시작 코드가 바뀌면 참조 구현은 모두 새 시작 코드 위에 처음부터 다시 쓴다."""
    notes = {a.key: f"검증 실패 (게이트 {a.gate}) — {a.note}" for a in actions}
    for a in actions:  # 산출물·풀마다 센다 (RepairAction.budget_key — repair.POOLS)
        out.regen_counts[a.budget_key] = out.regen_counts.get(a.budget_key, 0) + 1
        out.regen_log[a.key] = RegenNote(a.gate, a.cases, a.reason, a.blame)
    if any(a.generator in ("gen_hidden", "gen_starter") for a in actions):
        out.passed_before.clear()  # 테스트·시작 코드가 바뀌면 이전 판의 통과는 근거가 아니다

    async def tests_compile() -> bool:
        """다시 만든 채점 코드(숨은·공개 테스트)도 구현에 쓰기 전에 컴파일 — 처음 만들 때(compile_and_fix)와 같은 루프.
        2026-11-10 프론트 알파 fe-v3-opt: ⑤ 로 다시 만든 공개 테스트가 컴파일되지 않은 채 검증에 들어가, 모든 대상이 compile_error →
        ① 이 시작 코드를 다시 만들었고 그 시작 코드도 같은 공개 테스트 오류로 '컴파일 실패'가 됐다."""
        test_compile = getattr(compiler, "compile_tests", None)
        if compiler is None or test_compile is None:
            return True
        fix = await compile_and_fix(b, out.overlays[HIDDEN], out.overlays.get(PUBLIC), compiler, kw)
        if absorb is not None:  # 재요청 결과의 provenance·수리 이력 (ImplFix 와 같은 칸)
            absorb(fix)
        else:
            for r in fix.results:
                out.provenance += r.provenance
                out.adjustments += [f"{r.task}: {a}" for a in r.adjustments]
            out.repairs += fix.repairs
        out.overlays[HIDDEN] = fix.hidden
        if fix.public is not None:
            out.overlays[PUBLIC] = fix.public
        if fix.errors:
            e = fix.errors[0]
            out.review_flags.append(
                f"다시 생성한 채점 코드 컴파일 실패 (재요청 {len(fix.repairs)}회 뒤, 오류 {len(fix.errors)}건): "
                f"{e.path}:{e.line} {e.message} {e.detail}".strip()[:300]
            )
            return False
        return True

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
        out.overlays[PUBLIC] = new_public
        if not await tests_compile():
            return False
        public = out.overlays[PUBLIC]
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
        hidden_job = gen_hidden(
            b,
            note=notes["gen_hidden"],
            previous=hidden_prev,
            keep_previous=_keepable_cases(actions, out),
            **kw,  # type: ignore[arg-type]
        )
        jobs.append((HIDDEN, hidden_job))
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
    if "gen_hidden" in notes and out.overlays.get(HIDDEN) is not None and not await tests_compile():
        return False
    if (
        compiler is not None and absorb is not None and remade
    ):  # 다시 만든 참조 구현도 검증 전에 컴파일
        await _compile_refs(out, b, by_id, kw, compiler, absorb, remade)
    kept = any(isinstance(r, GenResult) and r.task == "gen_hidden" and r.kept for r in results)
    for a in actions:
        if a.generator in ("gen_starter", "gen_public"):
            continue
        label = HIDDEN if a.generator == "gen_hidden" else a.label
        ok = label in dict(jobs) and out.overlays.get(label) is not None
        result = "regenerated" if ok and ok_all else "failed"
        if result == "regenerated" and a.generator == "gen_hidden" and kept:
            result = "kept-previous"
        out.repairs.append(_record(a, result))  # type: ignore[arg-type]
    return ok_all
