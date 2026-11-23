"""지원자별 경험 검증 = 슬롯 배정 + 면접 질문 [확정: 2026-11-03 사용자].

LLM 은 주장마다 연결 문구(지원자에게 보임) + 면접 확인 질문 2~3개(면접관용)만 쓴다 (prompts/slot_questions).
입력의 지원자 텍스트는 가명화본에서 다시 뽑은 주장만 (derive_pseudonymized, 불변식 2).
검사: 연결 문구·질문에 원문 베끼기 없음(가명화 토큰·인용 수치·긴 겹침), 연결 문구에 수치·과제 정답 개념 없음. 1회 재요청.
슬롯이 배정된 주장만 확장 문항(TaskItem, track=claim_verification, slot_id)이 되고, 슬롯이 없으면 배정 기록에 면접 질문만.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.nodes.pseudonymize import load_claim_texts
from task_spec.ontology.relatedness import Relatedness
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    NodeError,
    Provenance,
    PseudonymizedText,
    RunState,
    SlotAssignment,
    SlotQuestionsInput,
    SlotQuestionsOutput,
    TaskItem,
)
from task_spec.spec.assign import assign_slots
from task_spec.spec.draft import _norm
from task_spec.spec.verify import copied, pick_targets

logger = logging.getLogger(__name__)

TASK = "slot_questions"
NO_SLOT = "(슬롯 없음 — 면접 질문만)"
FLAG_Q_FAILED = "면접 질문 생성 실패 — 면접관이 주장을 직접 확인하고 질문해야 함"
FLAG_Q_INVALID = "면접 질문 자동 검사 실패 (재요청 후에도): "
FLAG_NO_SLOT = "적합한 확장 슬롯 없음 — 응시 과제 없이 면접 질문만"
FLAG_UNAPPROVED_SLOT = (
    "승인 전 슬롯을 배정함 — 이 명세를 승인하면 공통 과제·슬롯도 공고 단위로 승인함"
)
_DIGIT = re.compile(r"\d")


def check_questions(out: SlotQuestionsOutput, quote: str, forbidden: list[str]) -> list[str]:
    problems: list[str] = []
    cp = copied("\n".join([out.link_text, *out.follow_up_questions]), quote)
    if cp:
        problems.append("지원자 원문을 그대로 씀: " + ", ".join(cp))
    if _DIGIT.search(out.link_text):
        problems.append("연결 문구에 수치가 있음 (행동만 짧게)")
    shown = [w for w in forbidden if len(_norm(w)) >= 2 and _norm(w) in _norm(out.link_text)]
    if shown:
        problems.append(f"연결 문구에 과제 정답 개념이 있음: {', '.join(shown)}")
    return problems


@dataclass
class QuestionsResult:
    output: SlotQuestionsOutput | None = None
    provenance: list[Provenance] = field(default_factory=list)
    errors: list[NodeError] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


async def make_questions(
    claim_id: str,
    claim_text: PseudonymizedText,
    quote: str,
    competencies: str,
    slot: TaskItem | None,
    forbidden: list[str],
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
) -> QuestionsResult:
    template = prompts.get(TASK)
    slot_text = f"{slot.title}\n{slot.scenario}" if slot is not None and slot.title else NO_SLOT
    res = QuestionsResult()
    best: tuple[SlotQuestionsOutput, list[str]] | None = None
    note = ""
    for attempt in range(2):
        payload = SlotQuestionsInput(
            claim=claim_text,
            competencies=competencies,
            slot=slot_text,
            forbidden=", ".join(forbidden) or "(없음)",
            revision_note=f"이전 답의 문제 (고쳐서 다시 쓰세요):\n{note}" if note else "(없음)",
        )
        prompt = assemble(template, payload, ontology_version=ontology_version)
        try:
            r = await llm.call(prompt, SlotQuestionsOutput)
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(
                    node=TASK,
                    code=type(exc).__name__,
                    message="면접 질문 생성 실패",
                    ref_id=claim_id,
                )
            )
            continue
        res.provenance.append(r.provenance)
        problems = check_questions(r.parsed, quote, forbidden)
        logger.info("slot_questions attempt=%d problems=%d", attempt + 1, len(problems))
        if best is None or len(problems) < len(best[1]):
            best = (r.parsed, problems)
        if not problems:
            break
        note = "\n".join(f"- {p}" for p in problems)
    if best is None:
        res.flags.append(FLAG_Q_FAILED)
        return res
    out, problems = best
    if problems:
        res.flags.append(FLAG_Q_INVALID + "; ".join(problems))
        out = out.model_copy(
            update={"link_text": "지원서에 적은 관련 경험"}
        )  # 문구만 안전한 기본값으로
    res.output = out
    return res


async def _claim_texts(
    state: RunState, claim_ids: list[str]
) -> tuple[dict[str, PseudonymizedText], list[NodeError]]:
    # 가명화본 로딩·실패 처리는 세 곳이 같은 규칙 (load_claim_texts, #246)
    claims = {c.claim_id: c for c in state.claims}
    return await load_claim_texts(
        state, [claims[cid] for cid in claim_ids if cid in claims], node=TASK
    )


async def build_slot_verifications(
    state: RunState,
    common: TaskItem,
    *,
    spec_id: str,
    llm: LLMClient,
    prompts: PromptRegistry,
    rel: Relatedness | None = None,
    reassign: dict[str, str | None] | None = None,
    slots_approved: bool = True,
) -> tuple[list[SlotAssignment], list[TaskItem], list[Provenance], list[NodeError]]:
    """검증할 주장 고르기 → 슬롯 배정(규칙) → 면접 질문(LLM, 동시에) → 배정 기록 + 확장 문항."""
    c = state.comparison
    if c is None or not c.claims_to_verify:
        return [], [], [], []
    common_ids = {t.competency_id for t in common.target_competencies}
    targets = pick_targets(c.claims_to_verify, common_ids)
    assignments = assign_slots(targets, common.slots, rel, reassign)
    slots = {s.task_id: s for s in common.slots}
    claims = {x.claim_id: x for x in state.claims}
    labels = {r.competency_id: r.label for x in state.candidates for r in x.candidates}
    if state.posting_profile is not None:
        labels.update({x.competency_id: x.label for x in state.posting_profile.competencies})
    texts, errors = await _claim_texts(state, [a.claim_id for a in assignments])

    common_secret = [o.value for o in common.observables if o.kind == "first_mention_of"]

    async def one(a: SlotAssignment) -> QuestionsResult | None:
        if a.claim_id not in texts:
            return None
        slot = slots.get(a.slot_id) if a.slot_id else None
        secret = common_secret + (
            [o.value for o in slot.observables if o.kind == "first_mention_of"] if slot else []
        )
        return await make_questions(
            a.claim_id,
            texts[a.claim_id],
            claims[a.claim_id].quote,
            ", ".join(labels.get(x, x) for x in a.competency_ids) or "(미상)",
            slot,
            list(dict.fromkeys(secret)),
            llm=llm,
            prompts=prompts,
            ontology_version=state.ontology_version,
        )

    results = await asyncio.gather(*(one(a) for a in assignments))
    prov: list[Provenance] = []
    final: list[SlotAssignment] = []
    items: list[TaskItem] = []
    for i, (a, res) in enumerate(zip(assignments, results, strict=True), 1):
        flags: list[str] = []
        update: dict[str, object] = {}
        if res is not None:
            prov += res.provenance
            errors += res.errors
            flags += res.flags
            if res.output is not None:
                update = {
                    "link_text": res.output.link_text,
                    "follow_up_questions": list(res.output.follow_up_questions),
                    "provenance_id": res.provenance[-1].provenance_id if res.provenance else None,
                }
        else:
            flags.append(FLAG_Q_FAILED)
        slot = slots.get(a.slot_id) if a.slot_id else None
        if slot is None:
            flags.append(FLAG_NO_SLOT)
        elif not slots_approved:
            flags.append(FLAG_UNAPPROVED_SLOT)
        a = a.model_copy(update={**update, "review_flags": flags})
        final.append(a)
        if slot is not None:
            items.append(
                TaskItem(
                    task_id=f"{spec_id}-verify-{i}",
                    track="claim_verification",
                    target_competencies=slot.target_competencies,
                    claims_to_verify=[a.claim_id],
                    parent_task_id=common.task_id,
                    slot_id=slot.task_id,
                    title=slot.title,
                    link_text=a.link_text,
                    follow_up_questions=a.follow_up_questions,
                    tech_env=slot.tech_env,
                    difficulty_target=slot.difficulty_target,
                    time_limit_min=slot.time_limit_min,
                    provenance_id=a.provenance_id,
                    review_flags=[
                        "확장 문항 — 비교 점수에 포함하지 않음 (확인/의심 + 면접 질문)",
                        f"배정 근거: {a.basis}",
                        *flags,
                    ],
                )
            )
    return final, items, prov, errors
