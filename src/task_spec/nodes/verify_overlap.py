"""④ 공고 역량 → 주장 확인 (top-down, prompts/verify_overlap). [확정: 2026-11-04 사용자]

선택 경로(retrieve_t1: 주장 → 검색 상위 10 → 주장별 0~2개)는 정답 역량이 어느 주장의 상위 10 에도 없으면 못 찾는다.
여기서는 반대 방향으로, 공고 역량마다 그 이력서의 주장 전체를 보고 action/related/mention/none 과 근거 주장을 받는다.
대조(compare)가 두 경로를 규칙으로 합친다 — 이 노드는 판정만 기록한다.

- LLM 입력 = 가명화본에서 다시 만든 주장 텍스트(derive_pseudonymized)와 온톨로지 역량 설명만 (불변식 2·3).
  목록이 공고 역량이라는 사실은 알리지 않는다 ('예' 쏠림 방지). retrieve 는 건드리지 않는다 (불변식 5).
- 공고 역량을 BATCH(40)개씩 묶어 묻는다 (보통 공고 1건 = 호출 1번).
- 답은 느슨한 모델(VerifyParsedOutput)로 받고 normalize 가 정리한다 — 프롬프트에 그리는 스키마(VerifyOverlapOutput)는
  평가 때와 같게 두고, 한도 하나 때문에 호출 전체가 실패하지 않게. 목록 밖 id·네 값 밖 label 은 버림, 중복은 앞의 것,
  근거 주장은 아는 것만 MAX_EVIDENCE 개까지, 근거 없는 판정은 none. 판정 이유(reason)는 읽지 않는다.
- 다시 묻기 (묶음 하나당 호출 상한 = 첫 호출 + 다시 묻기 MAX_REASK(2)번):
  · 답했는데 빠진 역량 → 그 역량만 한 번에 다시 (호출 1번)
  · 묶음 호출이 예외로 실패(빈 응답·추론 토큰 소진·잘림·스키마 등) → 그 묶음 전부를 반으로 나눠 다시 (호출 2번)
    — 긴 이력서(주장 50개+)에서 추론 토큰이 상한을 다 쓰면 첫 묶음이 통째로 실패한다 (2026-11-05 v2 e2e, G안)
  다시 묻기가 실패하거나 그래도 빠진 역량은 status=partial ('확인 안 됨' — 그 역량은 선택 경로만).
  모든 호출이 실패했을 때만 status=failed — 대조는 선택 경로만으로 (예전 동작).
  어떤 예외든 노드 밖으로 내보내지 않는다 (실행 전체가 죽지 않게). errors 에는 예외 이름만.
  실패한 호출도 쓴 토큰·비용은 provenance 로 남긴다 (llm.client 가 예외에 싣는 것).
- 판정 이유(LLM 자유 텍스트)는 State 에 남기지 않는다. 에러 메시지에는 id·고정 문구만.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from task_spec.llm import LLMClient
from task_spec.nodes.pseudonymize import (
    derive_pseudonymized,
    join_pseudonymized,
    load_claim_texts,
)
from task_spec.prompting import PromptError, PromptRegistry, assemble
from task_spec.retrieve.cards import CardSet, CompetencyCard
from task_spec.schemas import (
    Claim,
    NodeError,
    OverlapCheck,
    OverlapCheckResult,
    Provenance,
    PseudonymizedText,
    RunState,
    VerifyOverlapInput,
    VerifyParsedOutput,
)
from task_spec.schemas.domain import OverlapLabel

TASK = "verify_overlap"
NODE = "verify_overlap"
NOTE = "verify_answer_invalid"  # 정상 흐름의 재요청·보정 기록 (UI 는 *invalid 를 접어 둔다)
BATCH = 40  # 한 번에 묻는 공고 역량 수
MAX_REASK = 2  # 묶음 하나당 다시 묻기 호출 상한 (실패한 묶음은 반씩 2번, 빠진 역량은 1번)
REASK = "빠진 역량을 다시 물음"
MAX_EVIDENCE = 6  # 역량 하나에 남기는 근거 주장 수
LABELS: frozenset[str] = frozenset({"action", "related", "mention", "none"})
logger = logging.getLogger(__name__)


def describe(cid: str, card: CompetencyCard | None, fallback: str) -> str:
    """역량 한 줄: id 라벨 — ESCO 2개, NCS 능력단위 1개 (온톨로지 텍스트만)."""
    if card is None:
        return f"- {cid} {fallback}"
    extra = [*dict.fromkeys(card.esco[:2]), *card.ncs[:1]]
    return f"- {cid} {card.label}" + (f" — {', '.join(extra)}" if extra else "")


def claim_text(source: PseudonymizedText, claim: Claim) -> PseudonymizedText:
    """주장 1건 → "행동: … (칸: …) / 문장: …". 인용이 가명화본에 없으면 ValueError."""
    summary = claim.action + (f" (칸: {claim.field})" if claim.field else "")
    return derive_pseudonymized(source, quote=claim.quote, summary=summary)


def claim_block(
    texts: Sequence[tuple[str, PseudonymizedText]],
) -> tuple[PseudonymizedText, dict[str, str]]:
    """(claim_id, 텍스트) → "[c1] …" 묶음과 표시 → claim_id."""
    refs = [f"c{i}" for i in range(1, len(texts) + 1)]
    block = join_pseudonymized([t for _, t in texts], refs)
    return block, {r: cid for r, (cid, _) in zip(refs, texts, strict=True)}


@dataclass
class Normalized:
    checks: dict[str, OverlapCheck] = field(default_factory=dict)
    notes: list[tuple[str, str]] = field(default_factory=list)  # (사유, 역량 id)
    missing: list[str] = field(default_factory=list)


def normalize(
    items: Iterable[Any], asked: Sequence[str], refmap: dict[str, str], provenance_id: str | None
) -> Normalized:
    """LLM 답 → 검증된 판정. 목록 밖 id·네 값 밖 label 은 버림 · 중복은 앞의 것 ·
    근거는 아는 주장만 MAX_EVIDENCE 개까지 · 근거 없는 판정은 none. reason 은 읽지 않는다."""
    out = Normalized()
    asked_set = set(asked)
    for it in items:
        cid = it.competency_id
        if cid not in asked_set:
            out.notes.append(("목록 밖 역량 id 를 버림", ""))  # LLM 이 만든 문자열은 남기지 않는다
            continue
        if cid in out.checks:
            out.notes.append(("같은 역량 두 번 — 앞의 답을 씀", cid))
            continue
        if it.label not in LABELS:
            out.notes.append(("알 수 없는 판정 값 — 그 역량을 버림", cid))
            continue
        refs = list(dict.fromkeys(it.evidence_claims))
        known = [refmap[r] for r in refs if r in refmap]
        if len(known) < len(refs):
            out.notes.append(("없는 주장 표시를 버림", cid))
        if len(known) > MAX_EVIDENCE:
            out.notes.append((f"근거 주장을 {MAX_EVIDENCE}개로 줄임", cid))
            known = known[:MAX_EVIDENCE]
        label: OverlapLabel = it.label
        if label != "none" and not known:
            out.notes.append(("근거 주장 없는 판정을 none 으로", cid))
            label = "none"
        out.checks[cid] = OverlapCheck(
            competency_id=cid,
            label=label,
            claim_ids=known if label != "none" else [],
            provenance_id=provenance_id,
        )
    out.missing = [c for c in asked if c not in out.checks]
    return out


def batches(ids: Sequence[str], size: int = BATCH) -> list[list[str]]:
    return [list(ids[i : i + size]) for i in range(0, len(ids), size)]


def halves(ids: Sequence[str]) -> list[list[str]]:
    """실패한 묶음을 다시 물을 단위로 — 반씩 (한 개면 그대로). 다시 묻기 호출 수 ≤ MAX_REASK."""
    if len(ids) <= 1:
        return [list(ids)]
    mid = (len(ids) + 1) // 2
    return [list(ids[:mid]), list(ids[mid:])]


def _note(reason: str, ref: str) -> NodeError:
    return NodeError(node=NODE, code=NOTE, message=reason, ref_id=ref or None)


async def _claim_texts(
    state: RunState,
) -> tuple[list[tuple[str, PseudonymizedText]], list[NodeError]]:
    # 가명화본 로딩·실패 처리는 세 곳이 같은 규칙 (load_claim_texts, #246)
    texts, errors = await load_claim_texts(state, state.claims, node=NODE, derive=claim_text)
    return list(texts.items()), errors


def _failure(exc: BaseException, message: str) -> NodeError:
    return NodeError(node=NODE, code=type(exc).__name__, message=message)


def make_verify_overlap(
    llm: LLMClient | None, prompts: PromptRegistry | None, cards: CardSet | None = None
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def verify_overlap(state: RunState) -> dict[str, Any]:
        p = state.posting_profile
        # 주장·공고 역량이 없거나 LLM·프롬프트 미연결이면 하지 않는다 (overlap_check=None → 선택 경로만)
        if llm is None or prompts is None or p is None or not p.competencies or not state.claims:
            return {"overlap_check": None}
        try:
            template = prompts.get(TASK)
        except PromptError:
            return {"overlap_check": None}

        texts, errors = await _claim_texts(state)
        if not texts:
            return {"overlap_check": None, "errors": errors}
        block, refmap = claim_block(texts)
        labels = {c.competency_id: c.label for c in p.competencies}
        asked = list(labels)
        card_of = cards.cards if cards is not None else {}
        no_card = [c for c in asked if c not in card_of]
        if no_card:
            # 역량 설명(ESCO·NCS) 없이 라벨만 보낸다 — 판정이 약해질 수 있어 남긴다 (id·개수만)
            logger.warning("verify_overlap labels_only n=%d of %d", len(no_card), len(asked))
            errors.append(
                NodeError(
                    node=NODE,
                    code="verify_labels_only",
                    message=f"역량 카드 없음 — 라벨만으로 확인 ({len(no_card)}/{len(asked)}개)",
                )
            )

        provenance: list[Provenance] = []

        async def ask(ids: Sequence[str]) -> Normalized:
            try:
                payload = VerifyOverlapInput(
                    competencies="\n".join(describe(c, card_of.get(c), labels[c]) for c in ids),
                    claims=block,
                )
                prompt = assemble(template, payload, ontology_version=state.ontology_version)
                res = await llm.call(prompt, VerifyParsedOutput)
            except Exception as exc:
                spent = getattr(exc, "provenance", None)
                if isinstance(spent, Provenance):
                    provenance.append(spent)  # 실패한 호출도 쓴 토큰·비용은 남긴다
                raise
            provenance.append(res.provenance)
            return normalize(res.parsed.items, ids, refmap, res.provenance.provenance_id)

        checks: dict[str, OverlapCheck] = {}
        notes: list[tuple[str, str]] = []
        retry: list[list[str]] = []  # 다시 물을 묶음 (묶음마다 MAX_REASK 개까지)
        missing: list[str] = []
        succeeded = False
        for chunk in batches(asked):
            try:
                got = await ask(chunk)
            except Exception as exc:  # 어떤 예외든 실행을 죽이지 않는다 — 이 묶음 전부를 반씩 다시
                errors.append(_failure(exc, "공고 역량 확인 실패"))
                retry += halves(chunk)
                continue
            succeeded = True
            checks.update(got.checks)
            notes += got.notes
            if got.missing:
                retry.append(got.missing)  # 답에서 빠진 역량은 한 번에

        for group in retry:  # 차례로 (실패 때만 생기는 호출 — 동시 요청을 늘리지 않는다)
            notes += [(REASK, c) for c in group]
            try:
                got = await ask(group)
            except Exception as exc:  # 다시 묻기 실패 — 이미 받은 답은 지키고 이 묶음은 확인 안 됨
                errors.append(_failure(exc, "빠진 역량 다시 묻기 실패"))
                missing += group
                continue
            succeeded = True
            checks.update(got.checks)
            notes += got.notes
            missing += got.missing

        if not succeeded:
            errors += [_note(reason, ref) for reason, ref in notes]
            errors.append(
                NodeError(
                    node=NODE, code="verify_failed", message="공고 역량 확인 실패 — 선택 경로만"
                )
            )
            return {
                "overlap_check": OverlapCheckResult(status="failed"),
                "provenance": provenance,
                "errors": errors,
            }

        errors += [_note(reason, ref) for reason, ref in notes]
        errors += [
            NodeError(
                node=NODE,
                code="verify_missing_after_retry",
                message="다시 물어도 답이 없는 공고 역량 — 확인 안 됨",
                ref_id=c,
            )
            for c in missing
        ]
        result = OverlapCheckResult(
            status="partial" if missing else "ok",
            items=[checks[c] for c in asked if c in checks],
        )
        return {"overlap_check": result, "provenance": provenance, "errors": errors}

    return verify_overlap
