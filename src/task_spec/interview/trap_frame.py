"""함정 틀 (#318, docs/design/trap-frame.md) — ⑧ seed 단계에서 재료 분석 다음에 이 공고의 틀을 정하고, 틀 적용 재료를 만든다.

2026-11-24 세션 유지 과제(ct-f6087a7ba679): 공개 계약이 흔한 구현의 저장 방식(Session 테이블)을 요구했고(재료 stateful_target →
계약 재요청), 허용 설계 JWT 는 '접근 때 연장'을 지킬 수 없었다. 명세를 쓰기 전에 숨길 것·공개할 것·공개 쪽 금지·허용 설계 대조를 정하고,
코드가 그 결정을 재료 사본(틀 적용 재료)에 덮어써 명세 초안·계약 단계가 그대로 따르게 한다.

- 프롬프트에 CURRENT 가 없으면(평가 전) 부르는 쪽이 건너뛴다 — 지금 동작 그대로 (재료 분석과 같은 방식).
- 틀은 하나 [확정: 2026-11-25 사용자 — D3]: 호출 한 번에 틀 종류도 정하고, 코드가 템플릿을 보고 허용 종류를 넘긴다 (allowed_kinds).
- 재료의 상태 대상 요구는 틀이 '흔한 구현의 저장 방식'이라 하면 뺀다 [확정: 2026-11-25 사용자 — D4] — 계약 검사의 '만들어라'(_needs)와
  금지 저장 엔티티 검사(검사 4)의 '만들지 마라'가 부딪히지 않게.
- 틀은 막지 않는다 — 결과는 과제 검토 표시와 틀 적용 재료로만 (최종 판단은 사람). 틀이 템플릿과 맞지 않으면 원 재료로 만든다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from task_spec.interview.material_analysis import material_block, reinforce_lines
from task_spec.interview.task import template_json
from task_spec.llm import LLMClient
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    FeatureSeed,
    MaterialAnalysis,
    Provenance,
    PublicIncident,
    TrapFrame,
    TrapFrameInput,
)
from task_spec.schemas.interview_state import ReinforceAnswer
from task_spec.schemas.pseudo_text import PseudonymizedText
from task_spec.schemas.trap_frame import TrapKind
from task_spec.starter.templates import Template

TASK = "trap_frame"
FLAG = "함정 틀: "  # 과제 검토 표시 머리 (막지 않음)
FAILED = FLAG + "자동 생성 실패 — 원 재료로 만듦"
# B(동작 함정)를 잴 수 있는 템플릿 실패 사유 — 템플릿 v5 에는 없다 (injected_failure_ignored 는 401 과 503 을 가르지 못한다).
# 템플릿 v6 에 생기면 여기에 더한다 (#318 D2). 그 전에는 틀 종류가 design 하나다.
BEHAVIOR_REASONS: frozenset[str] = frozenset()


def allowed_kinds(template: Template) -> list[TrapKind]:
    """이 템플릿으로 잴 수 있는 틀 종류 — design 은 늘, behavior 는 템플릿에 B 사유가 있을 때만."""
    codes = set(template.manifest.reason_codes)
    return ["design", *(["behavior"] if codes & BEHAVIOR_REASONS else [])]  # type: ignore[list-item]


def frame_block(
    coding: PublicIncident,
    analysis: MaterialAnalysis | None,
    reinforce: Sequence[ReinforceAnswer] = (),
    *,
    shared: bool = False,
) -> dict[str, Any]:
    """틀 입력의 재료 블록 — 재료 분석 입력(material_block)에 업무 요구·계약 필요 요소·재료 분석 결과·보강 문답을 더한다.
    shared=True(공유 회사 사례)면 등록 규칙의 LLM 입력 칸만 (notes 제외) · 보강 문답 없음. 부르는 쪽이 가명화 검사로 감싼다."""
    block = material_block(coding, shared=shared)
    f = coding.feature
    if f is not None:
        block["business_need"] = f.business_need
        block["contract_needs"] = [{"kind": n.kind, "note": n.note} for n in f.contract_needs]
    if analysis is not None:
        block["analysis"] = {
            "mechanism": analysis.mechanism,
            "check": analysis.check,
            "reason": analysis.reason,
            "reproducible": analysis.reproducible,
            "gaps": list(analysis.gaps),
        }
    if reinforce and not shared:
        block["reinforcement"] = reinforce_lines(reinforce)
    return block


async def make_frame(
    material: PseudonymizedText,
    template: Template,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
) -> tuple[TrapFrame, Provenance]:
    """material 은 가명화한 틀 입력 블록 (frame_block 을 저장본 검사·공유 재료 검사로 감싼 것 — 불변식 2)."""
    result = await llm.call(
        assemble(
            prompts.get(TASK),
            TrapFrameInput(
                material=material,
                template_json=template_json(template),
                kinds=", ".join(allowed_kinds(template)),
            ),
            ontology_version=ontology_version,
        ),
        TrapFrame,
    )
    return result.parsed, result.provenance


def frame_texts(frame: TrapFrame) -> list[str]:
    """틀 안의 글 전부 — LLM 이 쓴 글이라 저장·다음 LLM 입력 전에 부르는 쪽이 하나씩 가명화 검사(derived)를 한다."""

    def walk(v: Any) -> list[str]:
        if isinstance(v, str):
            return [v] if v else []
        if isinstance(v, dict):
            return [s for x in v.values() for s in walk(x)]
        if isinstance(v, list):
            return [s for x in v for s in walk(x)]
        return []

    return walk(frame.model_dump())


def frame_problems(frame: TrapFrame, template: Template) -> list[str]:
    """틀을 쓸 수 없는 까닭 — 템플릿이 허용하지 않는 종류, 템플릿에 없는 사유. 있으면 원 재료로 만들고 표시만 남긴다."""
    out: list[str] = []
    if frame.kind not in allowed_kinds(template):
        out.append(f"{FLAG}이 템플릿이 잴 수 없는 틀 종류 {frame.kind} — 원 재료로 만듦")
    if frame.reason not in template.manifest.reason_codes:
        out.append(f"{FLAG}템플릿에 없는 실패 사유 {frame.reason} — 원 재료로 만듦")
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def apply_frame(
    seed: FeatureSeed,
    frame: TrapFrame,
    removed_designs: Sequence[str] = (),
) -> tuple[FeatureSeed, list[str]]:
    """틀 적용 재료 — 틀이 바꾸는 칸만 덮어쓴 재료 사본과 바꾼 것 (문서 §3.2). 나머지 칸(business_need·stage2_* 등)은 원 재료 그대로.

    - hidden_requirement ← 틀. behavior 면 trigger·why_naive_breaks 도 틀 것으로 (빈 글이면 원 재료).
    - accepted_designs ← 사람이 뺀 설계(removed_designs)를 제외. 둘 미만이 되면 원 재료 그대로 두고 적는다 (스키마 하한).
    - reasons ← 틀 사유가 재료 사유와 다르면 [틀 사유]·harness 는 템플릿 필수값(None) — 조절값은 사유마다 다르다.
    - contract_needs ← 틀이 상태 대상을 흔한 구현의 저장 방식이라 하면 stateful_target·transition_rules 를 뺀다 (D4).
      전이 규칙은 그 대상의 상태 칸에 적는 것이라 대상이 빠지면 계약 검사가 다시 요구하게 된다.
    - leak_terms ← 공개 쪽 금지 낱말을 더한다 — 명세 검사·계약 입력(forbidden_text)·계약 검사(_leaks)가 같은 목록을 받는다.
    """
    notes: list[str] = []
    update: dict[str, Any] = {"hidden_requirement": frame.hidden_requirement}
    if frame.kind == "behavior":
        if frame.trigger:
            update["trigger"] = frame.trigger
        if frame.why_naive_breaks:
            update["why_naive_breaks"] = frame.why_naive_breaks
    if removed_designs:
        gone = [_norm(r) for r in removed_designs if _norm(r)]
        kept = [d for d in seed.accepted_designs if not any(_norm(d).startswith(g) for g in gone)]
        if len(kept) >= 2:
            if len(kept) < len(seed.accepted_designs):
                update["accepted_designs"] = kept
                notes.append(
                    f"허용 설계 {len(seed.accepted_designs) - len(kept)}개를 뺌 (사람 결정)"
                )
        else:
            notes.append("허용 설계를 빼면 둘 미만이라 그대로 둠 — 사람 확인")
    if frame.reason not in seed.reasons:
        update["reasons"] = [frame.reason]
        update["harness"] = None
        notes.append(
            f"실패 사유를 틀 것({frame.reason})으로 — 재료 사유 {', '.join(seed.reasons) or '없음'}"
        )
    if frame.stateful_target_forbidden:
        dropped = {"stateful_target", "transition_rules"}
        needs = [n for n in seed.contract_needs if n.kind not in dropped]
        if len(needs) < len(seed.contract_needs):
            update["contract_needs"] = needs
            notes.append(
                "재료의 상태 대상 요구를 뺌 — 흔한 구현의 저장 방식"
                + (f" ({frame.stateful_target_why})" if frame.stateful_target_why else "")
            )
    terms = list(dict.fromkeys([*seed.leak_terms, *(t for t in frame.forbidden_terms if t)]))
    if len(terms) > len(seed.leak_terms):
        update["leak_terms"] = terms
    return seed.model_copy(update=update), notes


def frame_flags(frame: TrapFrame, notes: Sequence[str]) -> list[str]:
    """과제 검토 표시 — 틀이 바꾼 것, 허용 설계가 업무 요구를 못 지킬 수 있는 곳, 공개 계약에 두지 않을 엔티티, 근거 빈칸.
    LLM 이 쓴 글이 섞이므로 부르는 쪽이 가명화 검사(derived)를 거친다."""
    out = [f"{FLAG}{n}" for n in notes]
    for d in frame.design_matrix:
        if d.holds != "yes":
            what = f" '{d.requirement}'" if d.requirement else ""
            out.append(
                f"{FLAG}허용 설계가 업무 요구{what} 를 못 지킬 수 있음 ({d.holds}) — {d.design[:80]}: {d.why} — 사람 확인"
            )
    if frame.forbidden_storage:
        names = ", ".join(f"{s.entity}({s.why})" for s in frame.forbidden_storage)
        out.append(f"{FLAG}공개 계약에 두지 않을 엔티티 — {names}")
    if frame.gaps:
        out.append(f"{FLAG}근거 빈칸 — {'; '.join(frame.gaps)}")
    return out
