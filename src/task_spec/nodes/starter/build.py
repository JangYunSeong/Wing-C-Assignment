"""시작 코드 빌드 루프 — 생성 → 정적 검사 → 검증 → 실패 배정 → 그 생성기만 다시 (docs/design/spec-to-code.md §2 ③~⑥ · §7).

6단계 앞부분: 그래프 연결(State 는 경로·해시만, interrupt, 저장 #52)은 verifier(#55)·저장소가 생긴 뒤 이 순서를 노드로 옮긴다.
여기서는 같은 순서를 함수 하나로 돌린다 — 테스트는 가짜 verifier, 스모크는 개발 서버 Gradle verifier(eval/starter/gradle_verify.py).

예산 (§7): 산출물(생성기·라벨)마다 재생성 1회, 빌드 전체 재생성 최대 3회. 예산을 넘거나 배정이 모호하면 다시 만들지 않고 `failed`.
시작 코드를 다시 만들면 그 위에 쓴 참조 구현도 다시 만든다 (참조 구현 예산은 쓰지 않음). 숨은 테스트는 계약만 보므로 그대로.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from task_spec.llm import LLMClient
from task_spec.nodes.starter.generate import (
    BuildInputs,
    GenResult,
    gen_accepted,
    gen_hidden,
    gen_naive,
    gen_starter,
)
from task_spec.prompting import PromptRegistry
from task_spec.schemas import AcceptedDesign, Provenance, VerifyReport
from task_spec.schemas.build import RepairRecord
from task_spec.starter.contract import SpecView
from task_spec.starter.repair import STARTER, RepairAction, RepairPlan, assign
from task_spec.starter.static_check import Overlay, static_check

MAX_ROUNDS = 3  # 빌드 전체 재생성 상한
PER_ARTIFACT = 1  # 산출물마다 재생성 상한
HIDDEN = "hidden"

logger = logging.getLogger(__name__)


class Verifier(Protocol):
    """채점 경로로 대상마다 돌린 결과 — 시작 코드(①②), 참조 구현마다 시작 코드 + 구현 + 숨은 테스트(③~)."""

    async def verify(
        self, starter: Overlay, hidden: Overlay, refs: Sequence[Overlay]
    ) -> VerifyReport: ...


@dataclass
class BuildOutcome:
    status: Literal["review", "failed"]
    overlays: dict[str, Overlay] = field(default_factory=dict)  # starter · hidden · 참조 구현 라벨
    reports: list[VerifyReport] = field(default_factory=list)  # 검증 회차마다
    repairs: list[RepairRecord] = field(default_factory=list)
    review_flags: list[str] = field(default_factory=list)
    regen_counts: dict[str, int] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)
    rounds: int = 0  # 다시 만든 회차 수

    @property
    def cost_usd(self) -> float:
        return round(sum(p.cost_usd or 0.0 for p in self.provenance), 4)


def _record(a: RepairAction, result: Literal["regenerated", "failed", "skipped"]) -> RepairRecord:
    reason = f"{a.label}: {a.reason}" if a.label else a.reason
    return RepairRecord(gate=a.gate, target=a.generator, reason=reason[:300], result=result)


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
    for target in res.targets:
        msgs = res.for_target(target)
        note = "정적 검사 문제 — 고쳐서 다시 쓴다:\n" + "\n".join(f"- {m}" for m in msgs)
        if target in ("gen_starter", "gen_hidden"):
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
) -> BuildOutcome:
    kw = {"llm": llm, "prompts": prompts, "ontology_version": ontology_version}
    out = BuildOutcome(status="failed")
    by_id = {d.design_id: d for d in designs}
    missed = {"naive-all": {t.trap_id for t in b.task.traps}}

    def took(r: GenResult) -> Overlay | None:
        out.provenance += r.provenance
        if r.overlay is None or not r.ok:
            out.review_flags.append(
                f"{r.task} 생성 실패: " + "; ".join([*r.problems, *r.fail_reasons][:3])
            )
            return None
        return r.overlay

    starter = took(await gen_starter(b, **kw))  # type: ignore[arg-type]
    if starter is None:
        return out
    first = await asyncio.gather(
        gen_hidden(b, **kw),  # type: ignore[arg-type]
        gen_naive(b, starter, **kw),  # type: ignore[arg-type]
        *(gen_accepted(b, starter, d, **kw) for d in designs),  # type: ignore[arg-type]
    )
    made = [took(r) for r in first]
    if any(o is None for o in made):
        return out
    out.overlays = {STARTER: starter, **{(o.label or HIDDEN): o for o in made if o is not None}}

    for round_no in range(max_rounds + 1):
        out.rounds = round_no
        plan = _static_actions(out.overlays, b)
        if plan.passed:
            report = await verifier.verify(
                out.overlays[STARTER],
                out.overlays[HIDDEN],
                [o for k, o in out.overlays.items() if k not in (STARTER, HIDDEN)],
            )
            out.reports.append(report)
            plan = assign(report, b.plan, out.overlays, missed, out.regen_counts)
        logger.info(
            "build round=%d actions=%d unresolved=%d",
            round_no,
            len(plan.actions),
            len(plan.unresolved),
        )
        if plan.passed:
            out.status = "review"
            return out
        blocked = [a for a in plan.actions if out.regen_counts.get(a.key, 0) >= PER_ARTIFACT]
        if plan.unresolved or blocked or round_no == max_rounds:
            out.review_flags += plan.unresolved
            out.review_flags += [f"재생성 예산을 다 씀 — {a.key}: {a.reason}" for a in blocked]
            if round_no == max_rounds and not (plan.unresolved or blocked):
                out.review_flags.append(
                    f"빌드 전체 재생성 상한({max_rounds}회) — 남은 실패를 사람이 확인"
                )
            out.repairs += [_record(a, "skipped") for a in plan.actions]
            return out
        if not await _regenerate(plan.actions, out, b, by_id, kw, took):
            return out
    return out


async def _regenerate(
    actions: list[RepairAction],
    out: BuildOutcome,
    b: BuildInputs,
    by_id: dict[str, AcceptedDesign],
    kw: dict[str, object],
    took,  # type: ignore[no-untyped-def]
) -> bool:
    """배정된 생성기만 다시. 시작 코드가 바뀌면 참조 구현은 모두 새 시작 코드 위에 다시 쓴다."""
    notes = {a.key: f"검증 실패 (게이트 {a.gate}) — {a.note}" for a in actions}
    for a in actions:
        out.regen_counts[a.key] = out.regen_counts.get(a.key, 0) + 1
    starter = out.overlays[STARTER]
    if "gen_starter" in notes:
        new = took(await gen_starter(b, note=notes["gen_starter"], **kw))  # type: ignore[arg-type]
        ok = new is not None
        out.repairs += [
            _record(a, "regenerated" if ok else "failed")
            for a in actions
            if a.generator == "gen_starter"
        ]
        if new is None:
            return False
        out.overlays[STARTER] = starter = new
    refs = [(k, o) for k, o in out.overlays.items() if k not in (STARTER, HIDDEN)]
    jobs: list[tuple[str, object]] = []
    if "gen_hidden" in notes:
        jobs.append((HIDDEN, gen_hidden(b, note=notes["gen_hidden"], **kw)))  # type: ignore[arg-type]
    for label, o in refs:
        if o.kind == "ref_naive":
            key = f"gen_naive:{label}"
            if key in notes or "gen_starter" in notes:
                jobs.append((label, gen_naive(b, starter, note=notes.get(key), **kw)))  # type: ignore[arg-type]
        else:
            key = f"gen_accepted:{label}"
            if key in notes or "gen_starter" in notes:
                design = by_id[label]
                jobs.append((label, gen_accepted(b, starter, design, note=notes.get(key), **kw)))  # type: ignore[arg-type]
    results = await asyncio.gather(*(j for _, j in jobs))  # type: ignore[arg-type]
    ok_all = True
    for (label, _), r in zip(jobs, results, strict=True):
        new = took(r)
        if new is None:
            ok_all = False
        else:
            out.overlays[label] = new
    for a in actions:
        if a.generator == "gen_starter":
            continue
        label = HIDDEN if a.generator == "gen_hidden" else a.label
        ok = label in dict(jobs) and out.overlays.get(label) is not None
        out.repairs.append(_record(a, "regenerated" if ok and ok_all else "failed"))
    return ok_all
