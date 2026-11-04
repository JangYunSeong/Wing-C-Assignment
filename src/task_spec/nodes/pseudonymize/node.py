"""② 가명화 + 누락 검사.

가명화 이전 단계: LLM·임베딩 import 금지. 실패 문서가 하나라도 있으면 그래프는 END 로 간다
(부분 통과 금지, pseudonymization 스킬).

실제 가명화기는 `Pseudonymizer` 로 주입한다. 운영은 `RulePseudonymizer`(rule_pseudonymizer.py),
테스트는 가짜를 끼운다. 기본값 stub 는 텍스트를 내보내지 않으므로(path=None) 주장 추출까지 가지 않는다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from task_spec.llm.quotes import quote_in
from task_spec.nodes.pseudonymize.detectors import TOKEN_RE, detect_patterns
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


def derive_pseudonymized(
    source: PseudonymizedText, *, quote: str, summary: str = ""
) -> PseudonymizedText:
    """가명화본(source)에서 나온 주장(인용 + LLM 요약)을 다음 LLM 입력으로 넘길 때 쓴다.

    - quote: source 에 글자 그대로 있어야 한다 (공백·줄바꿈은 무시 — llm/quotes.quote_in 과 같은 규칙).
    - summary(행동 요약 등 LLM 이 쓴 자유 텍스트): 패턴 탐지기(이메일·전화·URL·주민번호)에 걸리면 거부.
    - 둘 다 가명화 토큰([PERSON_1] 등)은 source 에 있는 것만 허용 (위조 토큰 거부).
    결과 = "행동: {summary} / 문장: {quote}". 어기면 ValueError.
    """

    if not quote.strip() or not quote_in(quote, source):
        raise ValueError("인용이 가명화본에 없음")
    if summary and detect_patterns(summary):
        raise ValueError("요약에 개인정보 패턴")
    text = f"행동: {summary} / 문장: {quote}" if summary else quote
    known = {m.group(0) for m in TOKEN_RE.finditer(source)}
    if any(m.group(0) not in known for m in TOKEN_RE.finditer(text)):
        raise ValueError("가명화본에 없는 가명화 토큰")
    return PseudonymizedText(text, _token=_MINT)


def join_pseudonymized(
    parts: Sequence[PseudonymizedText], labels: Sequence[str], sep: str = "\n"
) -> PseudonymizedText:
    """가명화 텍스트 여러 개를 '[라벨] 텍스트' 줄로 잇는다. 라벨은 c1·c2 같은 짧은 표시만 (영숫자)."""
    if len(parts) != len(labels):
        raise ValueError("parts 와 labels 길이가 다릅니다")
    if not all(isinstance(p, PseudonymizedText) for p in parts):
        raise ValueError("가명화되지 않은 텍스트는 이을 수 없습니다")
    if not all(lb.isascii() and lb.isalnum() for lb in labels):
        raise ValueError("라벨은 영숫자만")
    return PseudonymizedText(
        sep.join(f"[{lb}] {p}" for lb, p in zip(labels, parts, strict=True)), _token=_MINT
    )


def split_pseudonymized(
    source: PseudonymizedText, line: int
) -> tuple[PseudonymizedText, PseudonymizedText]:
    """가명화본을 줄 경계에서 둘로 나눈다 — 긴 문서를 조각내 LLM 에 보낼 때 (주장 추출 출력 잘림).

    누락 검사를 통과한 가명화본(load_pseudonymized 결과)이나 그 조각만 받는다 — PseudonymizedText 가
    아닌 텍스트는 거부한다.

    내용을 바꾸지 않고 줄 단위로만 자르므로 가명화 토큰이 끊기지 않고 가명화 상태가 유지된다.
    두 조각을 줄바꿈으로 다시 이으면 source 와 같다.
    """
    if not isinstance(source, PseudonymizedText):
        raise ValueError("가명화되지 않은 텍스트는 나눌 수 없습니다")
    lines = source.split("\n")
    if not 0 < line < len(lines):
        raise ValueError("나눌 줄 위치가 범위 밖")
    return (
        PseudonymizedText("\n".join(lines[:line]), _token=_MINT),
        PseudonymizedText("\n".join(lines[line:]), _token=_MINT),
    )


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
