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

import os
import re
from collections.abc import Sequence
from typing import Any, NamedTuple

from task_spec.interview.material_analysis import DESIGN_CHOICES, material_block, reinforce_lines
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
from task_spec.starter.feasibility import names as condition_names
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


def _key(text: str) -> str:
    """글자 대조용 — 영숫자·한글만 남긴다 (띄어쓰기·문장 부호·대소문자 차이는 같게)."""
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())


_MARK = re.compile(r"^\s*\[[^\]]*\]\s*")  # 허용 설계 앞의 '[설계]' 표시


def _design_key(text: str) -> str:
    return _key(_MARK.sub("", text))


def match_design(text: str, designs: Sequence[str]) -> str | None:
    """틀이 옮겨 적은 설계(또는 사람이 뺀 설계 원문)가 재료의 어느 허용 설계인지 — 앞부분 글자가 가장 길게 겹치는 것.
    띄어쓰기·문장 부호·'[설계]' 표시는 무시한다. 겹친 글자가 8자(text 가 더 짧으면 그 길이) 미만이면 None.
    다시 만든 재료는 설계 문구가 조금 바뀔 수 있어 글자 그대로가 아니라 앞부분으로 맞춘다 (2026-11-25 평가: 틀이 띄어쓰기만 바꿔 옮겨 적음)."""
    key = _design_key(text)
    if not key:
        return None
    best, hit = 0, None
    for d in designs:
        n = len(os.path.commonprefix([key, _design_key(d)]))
        if n > best:
            best, hit = n, d
    return hit if best >= min(8, len(key)) else None


def _summary(design: str) -> str:
    """'[설계] 요약 — 조건: … — 함정: …' 의 요약 (질문 글에 넣는다)."""
    return _MARK.sub("", design).split(" — ", 1)[0].strip()[:100]


def design_questions(frame: TrapFrame, seed: FeatureSeed) -> list[tuple[str, str]]:
    """검사 2 설계 호환 (#318 §5.2) — 업무 요구를 못 지킬 수 있는 허용 설계(design_matrix 의 no·partial)를 정답에서 뺄지 묻는
    (질문 글, 재료 허용 설계 원문). 사람이 고른다 — '뺀다' 면 다음 과제 만들기에서 틀 적용 재료가 그 설계를 뺀다 (apply_frame).

    - yes 설계가 하나도 없으면 묻지 않는다 — 다 빼면 과제가 성립하지 않는다 (검토 표시로만).
    - 허용 설계는 2개 이상이어야 한다 (스키마·명세 원칙 — 한 가지 정답으로 감점하지 않게). 둘뿐인 재료는 대신 인정할 설계를 함께
      받는다 — 그 글은 문답으로 다음 재료 만들기(interview_feature_seed) 입력에 들어가 재료가 설계를 바꿀 수 있다.
    질문은 틀의 글(가명화 검사를 거친 것)과 재료 원문으로만 만든다 — 부르는 쪽이 저장 전에 다시 검사한다."""
    if not any(d.holds == "yes" for d in frame.design_matrix):
        return []
    remove, keep = DESIGN_CHOICES
    out: list[tuple[str, str]] = []
    for d in frame.design_matrix:
        original = None if d.holds == "yes" else match_design(d.design, seed.accepted_designs)
        if original is None or any(original == o for _, o in out):
            continue
        need = f" '{d.requirement}'" if d.requirement else ""
        why = d.why.strip().rstrip(".")
        tail = (
            f" 빼면 허용 설계가 하나만 남아 그대로는 뺄 수 없습니다 — 대신 인정할 설계가 있으면 '{remove} — <설계>' 로 적어 주세요."
            if len(seed.accepted_designs) <= 2
            else ""
        )
        out.append(
            (
                f"허용 설계 '{_summary(original)}'는 업무 요구{need}를 지키지 못할 수 있습니다 — {why}. "
                f"정답 설계에서 뺄까요? ({remove}/{keep}){tail}",
                original,
            )
        )
    return out


class EvidenceGap(NamedTuple):
    question: str  # ⑧ 보강 질문 글 (담당자에게 운영 값을 묻는다)
    flag: str  # 과제 검토 표시


def _param_label(template: Template, name: str) -> str:
    p = template.manifest.param(name)
    return re.split(r" — |\(", p.description)[0].strip() if p is not None else name


def evidence_gaps(
    reason: str, template: Template, seed: FeatureSeed, analysis: MaterialAnalysis | None
) -> list[EvidenceGap]:
    """검사 1 재현 근거 (#318 §5.1) — 틀 사유가 성립하려면 필요한 운영 조건(템플릿 failure_reasons 의 feasible.when 에 나오는
    조절값)의 근거가 재료에 있는가. 근거 = 재료가 숨은 요구를 재는 데 필요하다고 적은 조절값(harness) ∪ 재료 분석이 짚은 갈림
    조절값(split_params). 재료가 harness 를 적지 않았으면(None — 템플릿 필수값을 쓴다) 분석만 근거로 본다.
    수치는 빌드 보정이 실험으로 정한다 [확정: 2026-11-25] — 여기서는 근거가 있는지만. 템플릿 tunes·빌드 성립 검사(#313)는 보지 않는다.
    조절값이 아닌 이름(예: rows — 시드 행 수)은 보지 않는다. 빠진 것은 검토 표시와 ⑧ 보강 질문 후보 (인터뷰 사례만 묻는다)."""
    m = template.manifest
    fr = next((r for r in m.failure_reasons if r.code == reason), None)
    if fr is None:
        return []
    present = set(seed.harness or ()) | set(analysis.split_params if analysis is not None else ())
    out: list[EvidenceGap] = []
    for cond in fr.feasible:
        missing = sorted(
            n for n in condition_names(cond.when) if m.param(n) is not None and n not in present
        )
        if not missing:
            continue
        labels = ", ".join(_param_label(template, n) for n in missing)
        out.append(
            EvidenceGap(
                question=f"실패 사유 '{fr.description}'가 성립하는지 볼 운영 값이 재료에 없습니다 — {cond.why}. "
                f"실제 운영에서 {labels}은(는) 어느 정도였나요? (대략이라도)",
                flag=f"{FLAG}재현 근거 빈칸 — 사유 {reason} 의 성립 조건 '{cond.when}' 중 {', '.join(missing)} 근거 없음",
            )
        )
    return out


def keeps_hidden(seed: FeatureSeed, frame: TrapFrame) -> bool:
    """design 틀의 숨은 요구가 재료의 숨은 요구를 글자 그대로 담는가 (덧붙이기만 했는가).
    2026-11-25 trap_frame v1 평가: 평범한 재료의 숨은 요구를 다시 쓰며 조건을 뺐다(풀 교착 '롤백된 변경은 성공으로 기록되지 않는다')
    거나 허용 설계 하나를 배제하는 조건을 더했다(갱신 손실 '하나의 원자 작업으로') — 숨은 케이스가 재료와 다른 것을 재게 된다."""
    return _key(seed.hidden_requirement) in _key(frame.hidden_requirement)


def apply_frame(
    seed: FeatureSeed,
    frame: TrapFrame,
    removed_designs: Sequence[str] = (),
) -> tuple[FeatureSeed, list[str]]:
    """틀 적용 재료 — 틀이 바꾸는 칸만 덮어쓴 재료 사본과 바꾼 것 (문서 §3.2). 나머지 칸(business_need·stage2_* 등)은 원 재료 그대로.

    - hidden_requirement ← 틀. behavior 면 trigger·why_naive_breaks 도 틀 것으로 (빈 글이면 원 재료).
      design 틀이 재료의 숨은 요구를 글자 그대로 담지 않으면(keeps_hidden) 재료 것을 그대로 두고 적는다 — 재료 조건이 빠지지 않게.
    - accepted_designs ← 사람이 뺀 설계(removed_designs — ⑧ 설계 질문에 '뺀다', 재료 원문)를 제외. 다시 만든 재료는 문구가 바뀔 수
      있어 앞부분으로 맞춘다(match_design). 둘 미만이 되면 원 재료 그대로 두고 적는다 (스키마 하한).
    - reasons ← 틀 사유가 재료 사유와 다르면 [틀 사유]·harness 는 템플릿 필수값(None) — 조절값은 사유마다 다르다.
    - contract_needs ← 틀이 상태 대상을 흔한 구현의 저장 방식이라 하면 stateful_target·transition_rules 를 뺀다 (D4).
      전이 규칙은 그 대상의 상태 칸에 적는 것이라 대상이 빠지면 계약 검사가 다시 요구하게 된다.
    - leak_terms ← 공개 쪽 금지 낱말을 더한다 — 명세 검사·계약 입력(forbidden_text)·계약 검사(_leaks)가 같은 목록을 받는다.
    """
    notes: list[str] = []
    update: dict[str, Any] = {}
    if frame.kind == "design" and not keeps_hidden(seed, frame):
        notes.append("숨은 요구를 재료 것으로 유지 — 틀의 숨은 요구에 재료 조건 누락")
    else:
        update["hidden_requirement"] = frame.hidden_requirement
    if frame.kind == "behavior":
        if frame.trigger:
            update["trigger"] = frame.trigger
        if frame.why_naive_breaks:
            update["why_naive_breaks"] = frame.why_naive_breaks
    if removed_designs:
        matched = [match_design(r, seed.accepted_designs) for r in removed_designs]
        gone = {d for d in matched if d is not None}
        if lost := sum(1 for d in matched if d is None):
            notes.append(
                f"사람이 뺀 설계 {lost}개가 다시 만든 재료에 없음 (이미 빠졌을 수 있음) — 사람 확인"
            )
        kept = [d for d in seed.accepted_designs if d not in gone]
        if gone and len(kept) >= 2:
            update["accepted_designs"] = kept
            notes.append(f"허용 설계 {len(gone)}개를 뺌 (사람 결정)")
        elif gone:
            notes.append(
                "허용 설계를 빼면 둘 미만이라 그대로 둠 — 대신 인정할 설계 필요, 사람 확인"
            )
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
