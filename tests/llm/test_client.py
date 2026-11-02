from __future__ import annotations

import json
from pathlib import Path

import pytest

from task_spec.llm import (
    Completion,
    FakeBackend,
    LLMClient,
    SchemaViolation,
    TransientLLMError,
    Usage,
)
from task_spec.llm.client import EmptyResponse
from task_spec.nodes.pseudonymize import mint_pseudonymized
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import ClaimExtractionInput, ClaimExtractionOutput

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
TEXT = "가상 데이터: [ORG_1] 에서 데이터 파이프라인을 설계했다."
GOOD = json.dumps(
    {"claims": [{"action": "데이터 파이프라인 설계", "quote": "데이터 파이프라인을 설계했다."}]},
    ensure_ascii=False,
)


async def _no_sleep(_: float) -> None:
    return None


def _prompt():
    payload = ClaimExtractionInput(doc_kind="resume", text=mint_pseudonymized(TEXT))
    return assemble(PROMPTS.get("extract_claims"), payload, ontology_version="onto-v2026.11.02")


def _client(fake: FakeBackend) -> LLMClient:
    return LLMClient({"opencode": fake}, sleep=_no_sleep)


async def test_success_with_provenance() -> None:
    fake = FakeBackend().push(Completion(GOOD, Usage(100, 20, 5)))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert r.parsed.claims[0].action == "데이터 파이프라인 설계"
    p = r.provenance
    assert p.prompt == "extract_claims@1"
    assert p.model == "opencode:qwen3.8-flash"
    assert p.ontology_version == "onto-v2026.11.02"
    assert p.retries == 0
    assert (p.tokens.in_, p.tokens.out, p.tokens.reasoning) == (100, 20, 5)
    assert p.cost_usd is None  # 단가 모르는 모델은 추측하지 않음
    assert fake.calls == [
        ("qwen3.8-flash", {"temperature": 0, "max_tokens": 4096, "reasoning_effort": "low"})
    ]


async def test_code_fence_is_tolerated() -> None:
    fake = FakeBackend().push(Completion(f"```json\n{GOOD}\n```"))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(r.parsed.claims) == 1


async def test_empty_response_retries_with_low_effort() -> None:
    prompt = _prompt()
    prompt = prompt.model_copy(update={"params": {"temperature": 0}})  # 기본 추론 설정 상황
    fake = FakeBackend().push(Completion("", Usage(500, 4096, 4096), "length"), Completion(GOOD))
    r = await _client(fake).call(prompt, ClaimExtractionOutput)
    assert r.provenance.retries == 1
    assert fake.calls[1][1]["reasoning_effort"] == "low"
    assert r.provenance.params["reasoning_effort"] == "low"


async def test_empty_response_twice_raises() -> None:
    fake = FakeBackend().push(Completion(""), Completion(""))
    with pytest.raises(EmptyResponse):
        await _client(fake).call(_prompt(), ClaimExtractionOutput)


async def test_transient_errors_backoff_then_succeed() -> None:
    fake = FakeBackend().push(
        TransientLLMError("RateLimitError"), TransientLLMError("APITimeoutError"), Completion(GOOD)
    )
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert r.provenance.retries == 2


async def test_transient_errors_exhausted() -> None:
    fake = FakeBackend().push(*[TransientLLMError("InternalServerError")] * 4)
    with pytest.raises(TransientLLMError):
        await _client(fake).call(_prompt(), ClaimExtractionOutput)


async def test_schema_violation_reasks_once() -> None:
    fake = FakeBackend().push(Completion('{"claims": [{"quote": "x"}]}'), Completion(GOOD))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert r.provenance.retries == 1
    assert len(fake.calls) == 2


async def test_schema_violation_twice_raises_without_leaking_content() -> None:
    fake = FakeBackend().push(Completion("not json 가상 원문"), Completion('{"claims": "x"}'))
    with pytest.raises(SchemaViolation) as ei:
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert "가상 원문" not in str(ei.value)


async def test_unknown_params_dropped_and_recorded() -> None:
    prompt = _prompt().model_copy(update={"params": {"temperature": 0, "frequency_penalty": 1}})
    fake = FakeBackend().push(Completion(GOOD))
    r = await _client(fake).call(prompt, ClaimExtractionOutput)
    assert "frequency_penalty" not in fake.calls[0][1]
    assert r.provenance.params["dropped"] == ["frequency_penalty"]


async def test_unconfigured_provider() -> None:
    with pytest.raises(ValueError, match="프로바이더"):
        await LLMClient({}).call(_prompt(), ClaimExtractionOutput)
