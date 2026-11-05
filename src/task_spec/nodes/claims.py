"""② 주장 추출(LLM). 역할 판정은 nodes/roles.py (규칙).

extract_claims 는 검사를 통과한 가명화본만 읽어(PseudonymizedText) prompting.assemble() + llm.call() 로
주장을 뽑는다. 인용이 원문에 글자 그대로 없는 주장은 폐기하고 errors 에 남긴다 (재요청하지 않음).
문서 하나의 실패가 다른 문서를 막지 않는다. 에러 메시지에 원문·인용을 넣지 않는다.

출력 잘림(OutputTruncated — 추론 토큰이 출력 상한을 먼저 써서 짧은 문서에서도 생긴다)이면 문서를 경계에서
둘로 나눠 조각마다 다시 뽑는다 (llm.call(split_on_truncation=True), 최대 MAX_SPLIT_DEPTH 번 나눔).
- 경계: 칸 밖 줄(roles.field_of_lines 가 None) 중 빈 줄 다음 줄·절 제목·기간 머리말 — 칸 본문 중간에서
  자르지 않는다. 프로젝트 머리말(기간)과 칸 본문이 같은 조각에 남아야 기간·칸 문맥이 산다.
- 칸 위치(field)는 늘 문서 전체에서 찾는다 (불변식 6).
- 처음부터 나누기: PRESPLIT_CHARS 보다 긴 문서는 첫 호출 전에 같은 경계에서 그 길이 이하 조각으로 나눠 병렬로
  부른다 (잘린 큰 호출에 상한·시간을 버리지 않게 — 2026-11-05 진단: 2,000자 넘는 조각은 상한 4,096 에서 8/8 잘림).
  짧은 문서는 예전처럼 한 번에 부른다.
- 문서당 호출 상한 = call_budget(글자 수) (조각 수에 비례, 최소 MIN_CALLS_PER_DOC·최대 MAX_CALLS_PER_DOC),
  동시 호출 상한 MAX_CONCURRENCY. 상한을 넘은 조각은 실패로 친다.
- 부분 성공: 성공한 조각의 주장은 살리고 문서 단위로 claims_partial 을 남긴다. 전부 실패면 문서 실패.
- 잘리거나 실패한 호출의 비용도 provenance 로 남긴다 (예외의 .provenance).
프롬프트·CURRENT 는 그대로다.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from task_spec.llm import LLMClient, verify_quote
from task_spec.llm.backend import TransientLLMError
from task_spec.llm.client import EmptyResponse, OutputTruncated, SchemaViolation
from task_spec.nodes.ingest.structure import _PERIOD, _section_kind
from task_spec.nodes.pseudonymize import load_pseudonymized, split_pseudonymized
from task_spec.nodes.roles import field_of_lines, locate_field
from task_spec.prompting import PromptRegistry, PromptTemplate, assemble
from task_spec.schemas import (
    Claim,
    ClaimExtractionInput,
    ClaimExtractionOutput,
    DocumentKind,
    NodeError,
    Provenance,
    PseudonymizedText,
    RunState,
)

TASK = "extract_claims"
MAX_SPLIT_DEPTH = 3  # 잘린 조각을 반씩 최대 3번 더 나눈다
PRESPLIT_CHARS = 1500  # 이보다 긴 문서는 첫 호출 전에 칸 밖 경계에서 이 길이 이하로 나눈다
CALLS_PER_CHUNK = 2  # 처음 조각 하나당 호출 여유 (잘림 → 반 나누기)
MIN_CALLS_PER_DOC = 9  # 짧은 문서도 잘림 → 반 나누기를 몇 번 할 수 있게 (예전 고정 상한)
MAX_CALLS_PER_DOC = 25  # 문서 하나에 쓰는 LLM 호출 상한의 상한 (잘린 호출 포함)
MAX_CONCURRENCY = 3  # 문서 하나의 조각 동시 호출 상한

logger = logging.getLogger(__name__)


def call_budget(chars: int) -> int:
    """문서당 호출 상한 — 처음 조각 수에 비례 (2 × ⌈글자 수 / PRESPLIT_CHARS⌉ + 1), 9 이상 25 이하."""
    by_size = CALLS_PER_CHUNK * math.ceil(max(chars, 1) / PRESPLIT_CHARS) + 1
    return min(MAX_CALLS_PER_DOC, max(MIN_CALLS_PER_DOC, by_size))


def _line_starts(lines: list[str]) -> list[int]:
    """줄마다 시작 글자 위치 (마지막 원소 = 끝 + 1)."""
    out = [0]
    for line in lines:
        out.append(out[-1] + len(line) + 1)
    return out


def boundary_lines(text: str) -> list[int]:
    """문서를 나눌 수 있는 줄 번호 (오름차순).

    경계 = 칸 밖(field_of_lines 가 None)의 비지 않은 줄 가운데 (빈 줄 다음 줄 | 절 제목 | 기간이 든 머리말 줄).
    ⟦칸⟧ 표시 줄과 칸 본문이 이어지는 줄은 칸 안이라 경계가 아니다. 첫 줄은 경계가 아니다.
    """
    lines = text.split("\n")
    fields = field_of_lines(text)
    out: list[int] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i > 0 and stripped and fields[i] is None:
            if not lines[i - 1].strip() or _section_kind(stripped) or _PERIOD.search(stripped):
                out.append(i)
    return out


def split_line(text: str) -> int | None:
    """문서를 나눌 줄 번호 — 경계 줄 중 앞 조각 글자 수가 전체의 반에 가장 가까운 곳. 경계가 없으면 None."""
    starts = _line_starts(text.split("\n"))
    half = len(text) / 2
    best: int | None = None
    best_gap = 0.0
    for i in boundary_lines(text):
        gap = abs(starts[i] - half)
        if best is None or gap < best_gap:
            best, best_gap = i, gap
    return best


def presplit_lines(text: str, limit: int = PRESPLIT_CHARS) -> list[int]:
    """처음부터 나눌 줄 번호들 — 경계 사이 덩어리를 앞에서부터 limit 글자 이하로 채워 묶는다 (탐욕).

    limit 이하 문서는 [] (나누지 않음). 경계 없이 limit 을 넘는 덩어리는 그대로 한 조각으로 둔다.
    """
    if len(text) <= limit:
        return []
    lines = text.split("\n")
    starts = _line_starts(lines)
    bounds = boundary_lines(text)
    cuts: list[int] = []
    begin = 0
    for k, b in enumerate(bounds):
        nxt = bounds[k + 1] if k + 1 < len(bounds) else len(lines)
        if b > begin and starts[nxt] - starts[begin] > limit + 1:
            cuts.append(b)
            begin = b
    return cuts


def presplit(text: PseudonymizedText, limit: int = PRESPLIT_CHARS) -> list[PseudonymizedText]:
    """문서를 presplit_lines 위치에서 조각들로 (조각을 줄바꿈으로 이으면 원문). 짧은 문서는 [text]."""
    parts: list[PseudonymizedText] = []
    rest = text
    taken = 0
    for cut in presplit_lines(text, limit):
        left, rest = split_pseudonymized(rest, cut - taken)
        parts.append(left)
        taken = cut
    parts.append(rest)
    return parts


@dataclass
class Extraction:
    """문서 하나의 추출 결과. parts = 성공한 조각 (문서 순서), spent = 실패·잘린 호출의 provenance (비용)."""

    parts: list[tuple[PseudonymizedText, ClaimExtractionOutput, Provenance]] = field(
        default_factory=list
    )
    spent: list[Provenance] = field(default_factory=list)
    failures: list[str] = field(
        default_factory=list
    )  # 실패한 조각마다 사유 (예외 이름·call_budget)
    calls: int = 0


@dataclass
class _Ctx:
    llm: LLMClient
    template: PromptTemplate
    doc_kind: DocumentKind
    doc_id: str
    ontology_version: str
    max_calls: int
    sem: asyncio.Semaphore
    out: Extraction


async def _run_part(ctx: _Ctx, part: PseudonymizedText, depth: int) -> list[Any]:
    """조각 하나 → [성공 (part, parsed, prov) | 실패 사유 str] 목록 (문서 순서)."""
    if ctx.out.calls >= ctx.max_calls:
        return ["call_budget"]
    ctx.out.calls += 1
    payload = ClaimExtractionInput(doc_kind=ctx.doc_kind, text=part)
    prompt = assemble(ctx.template, payload, ontology_version=ctx.ontology_version)
    truncated = False
    async with ctx.sem:  # 나눠 다시 부르기 전에 자리를 돌려준다 (중첩 대기 교착 방지)
        try:
            result = await ctx.llm.call(prompt, ClaimExtractionOutput, split_on_truncation=True)
        except OutputTruncated as exc:
            if exc.provenance is not None:
                ctx.out.spent.append(exc.provenance)
            truncated = True
        except (SchemaViolation, EmptyResponse) as exc:
            if exc.provenance is not None:
                ctx.out.spent.append(exc.provenance)
            return [type(exc).__name__]
        except TransientLLMError:
            return ["TransientLLMError"]
    if not truncated:
        return [(part, result.parsed, result.provenance)]

    cut = split_line(part) if depth < MAX_SPLIT_DEPTH else None
    if cut is None:
        return ["OutputTruncated"]
    left, right = split_pseudonymized(part, cut)
    logger.warning(
        "extract_claims split doc=%s depth=%d chars=%d->%d+%d",
        ctx.doc_id,
        depth + 1,
        len(part),
        len(left),
        len(right),
    )
    halves = await asyncio.gather(_run_part(ctx, left, depth + 1), _run_part(ctx, right, depth + 1))
    return [*halves[0], *halves[1]]


async def extract_parts(
    llm: LLMClient,
    template: PromptTemplate,
    doc_kind: DocumentKind,
    text: PseudonymizedText,
    *,
    ontology_version: str,
    doc_id: str,
    max_calls: int | None = None,
    concurrency: int = MAX_CONCURRENCY,
) -> Extraction:
    """문서에서 주장을 뽑는다. 긴 문서는 처음부터 나누고, 출력이 잘리면 경계에서 반씩 나눠 다시.

    조각 순서 = 문서 순서. max_calls 기본은 call_budget(글자 수).

    예외를 올리지 않는다 — 조각마다 성공(parts)·실패 사유(failures)·쓴 비용(spent)을 모아 돌려준다.
    평가(eval/claims)도 이 함수를 써서 운영과 같은 경로로 잰다.
    """
    out = Extraction()
    ctx = _Ctx(
        llm=llm,
        template=template,
        doc_kind=doc_kind,
        doc_id=doc_id,
        ontology_version=ontology_version,
        max_calls=max_calls if max_calls is not None else call_budget(len(text)),
        sem=asyncio.Semaphore(concurrency),
        out=out,
    )
    chunks = presplit(text)
    if len(chunks) > 1:
        logger.info(
            "extract_claims presplit doc=%s chars=%d chunks=%d budget=%d",
            doc_id,
            len(text),
            len(chunks),
            ctx.max_calls,
        )
    results = await asyncio.gather(*(_run_part(ctx, c, 0) for c in chunks))
    for res in results:
        for item in res:
            if isinstance(item, str):
                out.failures.append(item)
            else:
                out.parts.append(item)
    return out


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
            got = await extract_parts(
                llm,
                template,
                ref.kind,
                text,
                ontology_version=state.ontology_version,
                doc_id=ref.doc_id,
            )
            provenance.extend(got.spent)
            if not got.parts:
                errors.append(
                    NodeError(
                        node=TASK,
                        code=got.failures[0] if got.failures else "EmptyResponse",
                        message="주장 추출 실패",
                        ref_id=ref.doc_id,
                    )
                )
                continue
            if got.failures:
                total = len(got.parts) + len(got.failures)
                errors.append(
                    NodeError(
                        node=TASK,
                        code="claims_partial",
                        message=f"주장 추출 일부 실패 — 조각 {len(got.failures)}/{total}",
                        ref_id=ref.doc_id,
                    )
                )

            n = 0
            i = 0
            for part, parsed, prov in got.parts:
                provenance.append(prov)
                for item in parsed.claims:
                    i += 1
                    if not verify_quote(item.quote, part):
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
                            provenance_id=prov.provenance_id,
                        )
                    )
        return {"claims": claims, "provenance": provenance, "errors": errors}

    return extract_claims
