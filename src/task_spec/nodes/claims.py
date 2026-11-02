"""② 주장 추출(LLM). 역할 판정은 nodes/roles.py (규칙).

extract_claims 는 검사를 통과한 가명화본만 읽어(PseudonymizedText) prompting.assemble() + llm.call() 로
주장을 뽑는다. 인용이 원문에 글자 그대로 없는 주장은 폐기하고 errors 에 남긴다 (재요청하지 않음).
문서 하나의 실패가 다른 문서를 막지 않는다. 에러 메시지에 원문·인용을 넣지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from task_spec.llm import LLMClient, verify_quote
from task_spec.llm.backend import TransientLLMError
from task_spec.llm.client import EmptyResponse, SchemaViolation
from task_spec.nodes.pseudonymize import load_pseudonymized
from task_spec.nodes.roles import locate_field
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    Claim,
    ClaimExtractionInput,
    ClaimExtractionOutput,
    NodeError,
    Provenance,
    RunState,
)

TASK = "extract_claims"


def make_extract_claims(
    llm: LLMClient | None, prompts: PromptRegistry | None
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def extract_claims(state: RunState) -> dict[str, Any]:
        targets = [p for p in state.pseudo_docs if p.check.passed and p.path is not None]
        if not targets:
            return {"claims": []}
        if llm is None or prompts is None:
            return {
                "claims": [],
                "errors": [
                    NodeError(node=TASK, code="llm_not_configured", message="LLM 클라이언트 없음")
                ],
            }

        template = prompts.get(TASK)
        claims: list[Claim] = []
        provenance: list[Provenance] = []
        errors: list[NodeError] = []

        for ref in targets:
            text = await asyncio.to_thread(load_pseudonymized, ref)
            payload = ClaimExtractionInput(doc_kind=ref.kind, text=text)
            prompt = assemble(template, payload, ontology_version=state.ontology_version)
            try:
                result = await llm.call(prompt, ClaimExtractionOutput)
            except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
                errors.append(
                    NodeError(
                        node=TASK,
                        code=type(exc).__name__,
                        message="주장 추출 실패",
                        ref_id=ref.doc_id,
                    )
                )
                continue

            provenance.append(result.provenance)
            n = 0
            for i, item in enumerate(result.parsed.claims, start=1):
                if not verify_quote(item.quote, text):
                    errors.append(
                        NodeError(
                            node=TASK,
                            code="quote_mismatch",
                            message=f"인용 불일치로 주장 {i} 폐기",
                            ref_id=ref.doc_id,
                        )
                    )
                    continue
                n += 1
                claims.append(
                    Claim(
                        claim_id=f"{ref.doc_id}-c{n}",
                        doc_id=ref.doc_id,
                        action=item.action,
                        target=item.target,
                        result=item.result,
                        period=item.period,
                        quote=item.quote,
                        field=locate_field(text, item.quote),
                        provenance_id=result.provenance.provenance_id,
                    )
                )
        return {"claims": claims, "provenance": provenance, "errors": errors}

    return extract_claims
