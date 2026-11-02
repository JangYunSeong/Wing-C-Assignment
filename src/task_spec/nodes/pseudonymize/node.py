"""② 가명화 + 누락 검사.

가명화 이전 단계: LLM·임베딩 import 금지. 실패 문서가 하나라도 있으면 그래프는 END 로 간다
(부분 통과 금지, pseudonymization 스킬).

실제 가명화기는 `Pseudonymizer` 로 주입한다. 운영은 `RulePseudonymizer`(rule_pseudonymizer.py),
테스트는 가짜를 끼운다. 기본값 stub 는 텍스트를 내보내지 않으므로(path=None) 주장 추출까지 가지 않는다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from task_spec.schemas import ExtractedRef, NodeError, PseudoCheck, PseudoDocRef, RunState
from task_spec.schemas.pseudo_text import _MINT, PseudonymizedText

Pseudonymizer = Callable[[str, ExtractedRef], Awaitable[PseudoDocRef]]


async def stub_pseudonymizer(run_id: str, doc: ExtractedRef) -> PseudoDocRef:
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
        # 추출에 실패한 문서는 가명화하지 않고 같은 사유로 실패시킨다 (→ END)
        results = [
            await pseudonymizer(state.run_id, r)
            if r.ok
            else PseudoDocRef(
                doc_id=r.doc_id,
                kind=r.kind,
                check=PseudoCheck(passed=False, residual_count=0, reason=r.reason),
            )
            for r in state.extracted
        ]
        errors = [
            NodeError(
                node="pseudonymize",
                code=r.check.reason or "residual_pii",
                message=f"가명화 실패: {r.check.reason or 'residual_pii'} (잔여 {r.check.residual_count}건)",
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
    """검사를 통과한 가명화본만 PseudonymizedText 로 읽는다 (블로킹 — 호출 측에서 to_thread).

    State 가 오염돼도 임의 파일이 LLM 으로 가지 않게 (privacy-auditor W3):
    경로 모양이 `.../pseudonymized/<run_id>/<doc_id>.txt` 이고, 심볼릭 링크가 아니고, sha256 이 맞아야 한다.
    """
    if not ref.check.passed or ref.path is None or ref.sha256 is None:
        raise PseudonymizedNotAvailable(ref.doc_id)
    path = Path(ref.path)
    if (
        path.is_symlink()
        or path.name != f"{ref.doc_id}.txt"
        or path.parent.parent.name != "pseudonymized"
    ):
        raise PseudonymizedNotAvailable(ref.doc_id)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref.sha256:
        raise PseudonymizedNotAvailable(ref.doc_id)
    return mint_pseudonymized(raw.decode("utf-8"))
