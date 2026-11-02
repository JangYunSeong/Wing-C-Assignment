"""② 가명화 + 누락 검사.

가명화 이전 단계: LLM·임베딩 import 금지. 실패 문서가 하나라도 있으면 그래프는 END 로 간다
(부분 통과 금지, pseudonymization 스킬).

실제 가명화기는 `Pseudonymizer` 로 주입한다 (테스트에서 실패 경로를 만들기 위해).
TODO(pseudonymization): 규칙·사전·로컬 NER 기반 구현으로 교체.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from task_spec.schemas import DocumentRef, NodeError, PseudoCheck, PseudoDocRef, RunState
from task_spec.schemas.pseudo_text import _MINT, PseudonymizedText

Pseudonymizer = Callable[[str, DocumentRef], Awaitable[PseudoDocRef]]


async def stub_pseudonymizer(run_id: str, doc: DocumentRef) -> PseudoDocRef:
    """골격용: 아무것도 치환하지 않고 검사 통과로 표시한다."""
    return PseudoDocRef(
        doc_id=doc.doc_id,
        kind=doc.kind,
        path=None,
        sha256=None,
        check=PseudoCheck(passed=True, residual_count=0, detectors=["stub"]),
    )


def make_pseudonymize(
    pseudonymizer: Pseudonymizer,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def pseudonymize(state: RunState) -> dict[str, Any]:
        results = [await pseudonymizer(state.run_id, d) for d in state.documents]
        errors = [
            NodeError(
                node="pseudonymize",
                code="residual_pii",
                message=f"누락 검사 실패: 잔여 {r.check.residual_count}건",
                ref_id=r.doc_id,
            )
            for r in results
            if not r.check.passed
        ]
        return {"pseudo_docs": results, "errors": errors}

    return pseudonymize


def pseudonymization_passed(state: RunState) -> bool:
    return bool(state.pseudo_docs) and all(p.check.passed for p in state.pseudo_docs)


def mint_pseudonymized(text: str) -> PseudonymizedText:
    """가명화기가 누락 검사를 통과시킨 텍스트에만 쓴다. 이 모듈 밖에서 PseudonymizedText 를 만들지 않는다."""
    return PseudonymizedText(text, _token=_MINT)


class PseudonymizedNotAvailable(Exception):
    """가명화본이 없거나 검사를 통과하지 못한 문서."""


def load_pseudonymized(ref: PseudoDocRef) -> PseudonymizedText:
    """검사를 통과한 가명화본만 PseudonymizedText 로 읽는다 (블로킹 — 호출 측에서 to_thread)."""
    if not ref.check.passed or ref.path is None:
        raise PseudonymizedNotAvailable(ref.doc_id)
    return mint_pseudonymized(Path(ref.path).read_text(encoding="utf-8"))
