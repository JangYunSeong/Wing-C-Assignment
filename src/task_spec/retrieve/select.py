"""③ T1 선택: 주장별 검색 후보 상위 k → LLM(prompts/select_competency) 이 0~2개 선택 또는 uncovered.

- 주장 묶음(BATCH)마다 한 번 호출. 후보는 주장마다 따로 준다 (후보 전체를 한꺼번에 주면 흔들림 — #59 §8).
- 후보 목록 밖 id 는 버리고 errors 에 남긴다. 답이 빠진 주장은 선택 없음 + errors.
- 공고 역량 여부는 LLM 에 보여주지 않는다 (가산점은 검색에서만 — 불변식 5).
- 에러 메시지에 주장 원문을 넣지 않는다 (id 만).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from task_spec.llm import LLMClient
from task_spec.llm.backend import TransientLLMError
from task_spec.llm.client import EmptyResponse, SchemaViolation
from task_spec.nodes.pseudonymize import join_pseudonymized
from task_spec.prompting import PromptRegistry, assemble
from task_spec.retrieve.cards import CardSet
from task_spec.retrieve.t1 import Hit
from task_spec.schemas import (
    CandidateRef,
    ClaimCandidates,
    NodeError,
    Provenance,
    PseudonymizedText,
    SelectCompetencyInput,
    SelectCompetencyOutput,
)

TASK = "select_competency"
BATCH = 8


@dataclass
class SelectInputClaim:
    claim_id: str
    text: PseudonymizedText  # "행동: … / 문장: …" (가명화본에서 나온 것만)
    hits: list[Hit]


@dataclass
class SelectResult:
    items: list[ClaimCandidates] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)
    errors: list[NodeError] = field(default_factory=list)


def candidate_refs(cards: CardSet, hits: Sequence[Hit]) -> list[CandidateRef]:
    return [
        CandidateRef(
            competency_id=h.competency_id,
            label=cards.cards[h.competency_id].label,
            score=round(h.score, 5),
            ranks=h.ranks,
            in_scope=h.in_scope,
            posting=h.posting,
        )
        for h in hits
    ]


def _describe(cards: CardSet, h: Hit) -> str:
    c = cards.cards[h.competency_id]
    extra = ", ".join(dict.fromkeys(c.esco[:2]))
    return (
        f"- {c.id} {c.label}"
        + (f" — {extra}" if extra else "")
        + ("" if h.in_scope else " (범위 밖)")
    )


async def select(
    claims: Sequence[SelectInputClaim],
    *,
    sub: str,
    cards: CardSet,
    llm: LLMClient,
    prompts: PromptRegistry,
    batch: int = BATCH,
) -> SelectResult:
    template = prompts.get(TASK)
    out = SelectResult()
    # 후보가 하나도 없는 주장은 LLM 에 보내지 않는다 (고를 게 없음)
    out.items.extend(ClaimCandidates(claim_id=x.claim_id) for x in claims if not x.hits)
    claims = [x for x in claims if x.hits]
    for start in range(0, len(claims), batch):
        chunk = claims[start : start + batch]
        refs = {f"c{i}": x for i, x in enumerate(chunk, 1)}
        claims_block = join_pseudonymized([x.text for x in chunk], list(refs))
        cand_block = "\n\n".join(
            f"[{r}]\n" + "\n".join(_describe(cards, h) for h in x.hits) for r, x in refs.items()
        )
        payload = SelectCompetencyInput(sub=sub, claims=claims_block, candidates=cand_block)
        prompt = assemble(template, payload, ontology_version=cards.version)
        try:
            res = await llm.call(prompt, SelectCompetencyOutput)
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            for x in chunk:
                out.items.append(
                    ClaimCandidates(claim_id=x.claim_id, candidates=candidate_refs(cards, x.hits))
                )
                out.errors.append(
                    NodeError(
                        node=TASK,
                        code=type(exc).__name__,
                        message="역량 선택 실패",
                        ref_id=x.claim_id,
                    )
                )
            continue
        out.provenance.append(res.provenance)
        answers = {a.ref: a for a in res.parsed.items}
        for r, x in refs.items():
            allowed = {h.competency_id for h in x.hits}
            a = answers.get(r)
            ids: list[str] = []
            if a is None:
                out.errors.append(
                    NodeError(
                        node=TASK, code="missing_answer", message="선택 답 없음", ref_id=x.claim_id
                    )
                )
            else:
                ids = [c for c in dict.fromkeys(a.competency_ids) if c in allowed][:2]
                if len(ids) < len([c for c in a.competency_ids if c]):
                    out.errors.append(
                        NodeError(
                            node=TASK,
                            code="candidate_out_of_list",
                            message="후보 밖 역량 선택을 버림",
                            ref_id=x.claim_id,
                        )
                    )
            out.items.append(
                ClaimCandidates(
                    claim_id=x.claim_id,
                    candidates=candidate_refs(cards, x.hits),
                    competency_ids=ids,
                    uncovered=bool(a and a.uncovered and not ids),
                    reason=a.reason if a else None,
                    provenance_id=res.provenance.provenance_id,
                )
            )
    return out
