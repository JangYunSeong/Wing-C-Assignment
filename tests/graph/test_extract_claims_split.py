"""출력 잘림(OutputTruncated) → 문서를 경계에서 나눠 다시 추출. 데이터는 전부 가상."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from task_spec.llm import Completion, FakeBackend, LLMClient, Usage
from task_spec.llm.fake import input_key
from task_spec.nodes.claims import (
    MAX_CALLS_PER_DOC,
    MAX_CONCURRENCY,
    extract_parts,
    make_extract_claims,
    split_line,
)
from task_spec.nodes.pseudonymize import mint_pseudonymized, split_pseudonymized
from task_spec.nodes.roles import field_of_lines
from task_spec.prompting import Message, PromptRegistry, assemble
from task_spec.schemas import ClaimExtractionInput, PseudoCheck, PseudoDocRef, RunState
from tests.conftest import fake_doc, run_input

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
TEMPLATE = PROMPTS.get("extract_claims")
DOC = "\n".join(
    [
        "[PERSON_1] | 물류 시스템 개발자",
        "",
        "[경력]",
        "[ORG_1] | 배송개발팀 2021.03 ~ 2023.02",
        "⟦담당업무⟧ 창고 재고 조회 API를 구현했다.",
        "⟦기여한 점⟧ 출고 대기열 구조를 설계했다.",
        "",
        "프로젝트: 반품 처리 개편 2022.01 ~ 2022.06",
        "⟦과정⟧ 반품 접수 배치를 이벤트 방식으로 바꿨다.",
        "⟦성과⟧ 반품 처리 시간 2일에서 4시간으로 단축.",
    ]
)
# 빈 줄로 나뉜 블록 8개 — 세 번 반씩 나눌 수 있다
BLOCKS = "\n\n".join(
    f"프로젝트 {k}: 가상 과제 {k}\n⟦과정⟧ 가상 작업 {k} 을 했다." for k in range(8)
)
TRUNCATED = Completion('{"claims": [{"action": "창고', Usage(900, 4096, 3500), "length")
BROKEN = Completion("깨진 응답", Usage(900, 300, 100))


def _key(text: str) -> str:
    payload = ClaimExtractionInput(doc_kind="resume", text=mint_pseudonymized(text))
    prompt = assemble(TEMPLATE, payload, ontology_version="onto-v2026.11.02")
    return input_key(list(prompt.messages))


def _body(*claims: tuple[str, str]) -> Completion:
    items = [{"action": a, "quote": q} for a, q in claims]
    return Completion(json.dumps({"claims": items}, ensure_ascii=False), Usage(400, 300, 100))


def _state(tmp_path: Path, text: str) -> RunState:
    inp = run_input(docs=[fake_doc("d1", "resume")])
    out = tmp_path / "pseudonymized" / inp["run_id"] / "d1.txt"
    out.parent.mkdir(parents=True)
    out.write_text(text, encoding="utf-8")
    ref = PseudoDocRef(
        doc_id="d1",
        kind="resume",
        path=str(out),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        check=PseudoCheck(passed=True, residual_count=0, detectors=["fake"]),
    )
    return RunState.model_validate({**inp, "pseudo_docs": [ref]})


def _halves(text: str) -> tuple[str, str]:
    cut = split_line(text)
    assert cut is not None
    left, right = split_pseudonymized(mint_pseudonymized(text), cut)
    return str(left), str(right)


# ── 경계 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [DOC, BLOCKS])
def test_split_line_cuts_outside_fields_near_half(text: str) -> None:
    cut = split_line(text)
    assert cut is not None
    assert field_of_lines(text)[cut] is None  # 칸 본문 중간에서 자르지 않는다
    left = "\n".join(text.split("\n")[:cut])
    assert abs(len(left) - len(text) / 2) < len(text) / 3


def test_split_line_skips_field_lines_even_if_nearer_to_half() -> None:
    # 반 지점 근처는 칸 줄뿐 — 칸 밖 경계(두 번째 머리말)를 고른다
    text = "\n".join(
        [
            "프로젝트: A 2021.01 ~ 2021.06",
            "⟦과정⟧ 가상 작업 하나를 했다.",
            "⟦과정⟧ 가상 작업 둘을 했다.",
            "⟦과정⟧ 가상 작업 셋을 했다.",
            "⟦성과⟧ 가상 지표를 올렸다.",
            "프로젝트: B 2022.01 ~ 2022.06",
            "⟦과정⟧ 가상 작업 넷을 했다.",
        ]
    )
    assert split_line(text) == 5


def test_split_line_none_when_only_field_lines() -> None:
    assert split_line("⟦과정⟧ 배치를 바꿨다.\n⟦성과⟧ 시간 단축.") is None


def test_split_pseudonymized_round_trip_and_type_guard() -> None:
    src = mint_pseudonymized(DOC)
    left, right = split_pseudonymized(src, 3)
    assert f"{left}\n{right}" == DOC
    with pytest.raises(ValueError):
        split_pseudonymized(DOC, 3)  # type: ignore[arg-type]  # 일반 str 은 나누지 않는다
    with pytest.raises(ValueError):
        split_pseudonymized(src, 0)


# ── 노드 ─────────────────────────────────────────────────────────────


async def test_truncated_document_is_split_and_merged_in_order(tmp_path: Path) -> None:
    left, right = _halves(DOC)
    fake = FakeBackend()
    fake.push(TRUNCATED, key=_key(DOC))
    fake.push(
        _body(
            ("창고 재고 조회 API 구현", "창고 재고 조회 API를 구현했다."),
            ("출고 대기열 구조 설계", "출고 대기열 구조를 설계했다."),
        ),
        key=_key(left),
    )
    fake.push(
        _body(
            ("반품 접수 배치를 이벤트 방식으로 변경", "반품 접수 배치를 이벤트 방식으로 바꿨다."),
            ("반품 처리 시간 단축", "반품 처리 시간 2일에서 4시간으로 단축."),
        ),
        key=_key(right),
    )
    node = make_extract_claims(LLMClient({"opencode": fake}), PROMPTS)
    out = await node(_state(tmp_path, DOC))

    claims = out["claims"]
    assert [c.claim_id for c in claims] == ["d1-c1", "d1-c2", "d1-c3", "d1-c4"]
    # 칸 위치는 문서 전체에서 찾는다 — 조각으로 나눠도 칸 규칙(불변식 6)의 근거가 그대로
    assert [c.field for c in claims] == ["담당업무", "기여한 점", "과정", "성과"]
    assert out["errors"] == []
    # 잘린 호출 1 + 조각 2 — 잘린 호출의 비용도 남는다
    assert len(out["provenance"]) == 3
    assert sum(p.tokens.out for p in out["provenance"]) == 4096 + 300 + 300
    assert {c.provenance_id for c in claims[:2]} != {c.provenance_id for c in claims[2:]}
    assert len(fake.calls) == 3  # 잘린 응답에 형식 재요청을 하지 않는다


async def test_one_part_failure_keeps_other_parts_and_marks_partial(tmp_path: Path) -> None:
    left, right = _halves(DOC)
    fake = FakeBackend()
    fake.push(TRUNCATED, key=_key(DOC))
    fake.push(_body(("창고 재고 조회 API 구현", "창고 재고 조회 API를 구현했다.")), key=_key(left))
    fake.push(BROKEN, key=_key(right))
    # 형식 재요청은 입력(메시지)이 달라 default 큐를 쓴다
    fake.push(BROKEN)
    node = make_extract_claims(LLMClient({"opencode": fake}), PROMPTS)
    out = await node(_state(tmp_path, DOC))

    assert [c.quote for c in out["claims"]] == ["창고 재고 조회 API를 구현했다."]
    assert [(e.code, e.ref_id) for e in out["errors"]] == [("claims_partial", "d1")]
    assert "1/2" in out["errors"][0].message
    assert "반품" not in out["errors"][0].message  # 에러에 원문 없음
    assert len(out["provenance"]) == 3  # 잘린 호출 + 성공 조각 + 실패 조각(SchemaViolation)


async def test_unsplittable_document_fails_at_depth_zero(tmp_path: Path) -> None:
    text = "⟦과정⟧ 가상 배치를 바꿨다.\n⟦성과⟧ 가상 시간을 줄였다."
    fake = FakeBackend().push(TRUNCATED)
    node = make_extract_claims(LLMClient({"opencode": fake}), PROMPTS)
    out = await node(_state(tmp_path, text))
    assert out["claims"] == []
    assert [(e.code, e.ref_id) for e in out["errors"]] == [("OutputTruncated", "d1")]
    assert len(fake.calls) == 1
    assert len(out["provenance"]) == 1  # 실패해도 잘린 호출 비용은 남는다


async def test_call_budget_caps_calls_exactly() -> None:
    fake = FakeBackend().push(*[TRUNCATED] * 20)
    got = await extract_parts(
        LLMClient({"opencode": fake}),
        TEMPLATE,
        "resume",
        mint_pseudonymized(BLOCKS),
        ontology_version="onto-v2026.11.02",
        doc_id="d1",
    )
    # 1 + 2 + 4 = 7 호출 뒤 깊이 3 조각 8개 중 2개만 상한 안 → 9 호출, 나머지 6개는 상한 초과
    assert got.calls == MAX_CALLS_PER_DOC == 9
    assert len(fake.calls) == 9
    assert got.parts == []
    assert Counter(got.failures) == {"OutputTruncated": 2, "call_budget": 6}
    assert len(got.spent) == 9


class _SlowTruncating:
    """잘린 응답만 주고 동시 호출 수를 센다."""

    def __init__(self) -> None:
        self.inflight = 0
        self.peak = 0

    async def complete(
        self, model: str, messages: list[Message], params: dict[str, Any]
    ) -> Completion:
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        await asyncio.sleep(0.01)
        self.inflight -= 1
        return TRUNCATED


async def test_concurrency_is_capped_per_document() -> None:
    backend = _SlowTruncating()
    got = await extract_parts(
        LLMClient({"opencode": backend}),
        TEMPLATE,
        "resume",
        mint_pseudonymized(BLOCKS),
        ontology_version="onto-v2026.11.02",
        doc_id="d1",
    )
    assert got.calls == 9
    assert backend.peak == MAX_CONCURRENCY == 3
