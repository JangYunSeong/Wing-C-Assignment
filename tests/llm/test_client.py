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
from task_spec.llm.client import EmptyResponse, OutputTruncated
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
    assert p.prompt == PROMPTS.get("extract_claims").ref  # CURRENT
    assert p.model == "opencode:qwen3.8-flash"
    assert p.ontology_version == "onto-v2026.11.02"
    assert p.retries == 0
    assert (p.tokens.in_, p.tokens.out, p.tokens.reasoning) == (100, 20, 5)
    assert p.cost_usd == 0.000024  # qwen3.8-flash 단가 × (입력 100, 출력 20 — 추론 포함)
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


async def test_gpt5_drops_temperature_and_is_priced() -> None:
    from task_spec.llm import LLMClient

    base = _prompt()
    meta = base.template.meta.model_copy(update={"model": "openai:gpt-5.4-mini"})
    prompt = base.model_copy(
        update={
            "template": base.template.model_copy(update={"meta": meta}),
            "params": {"temperature": 0, "max_tokens": 100},
        }
    )
    fake = FakeBackend().push(Completion(GOOD))
    r = await LLMClient({"openai": fake}).call(prompt, ClaimExtractionOutput)
    assert fake.calls[0] == ("gpt-5.4-mini", {"max_tokens": 100})
    assert r.provenance.params["dropped"] == ["temperature"]
    assert r.provenance.cost_usd is not None


async def test_unconfigured_provider() -> None:
    with pytest.raises(ValueError, match="프로바이더"):
        await LLMClient({}).call(_prompt(), ClaimExtractionOutput)


def test_unknown_model_has_no_cost_and_reasoning_model_drops_temperature() -> None:
    from task_spec.llm.client import is_reasoning_model
    from task_spec.llm.pricing import cost_usd

    assert cost_usd("openai:없는모델", 100, 100) is None  # 단가 모르는 모델은 추측하지 않음
    assert is_reasoning_model("openai", "gpt-5.5") and is_reasoning_model("openai", "gpt-5.4-mini")
    assert not is_reasoning_model("openai", "gpt-4.1") and not is_reasoning_model(
        "opencode", "gpt-5.5"
    )


async def test_timeout_is_retried_only_once_and_logged(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    fake = FakeBackend().push(
        TransientLLMError("APITimeoutError"),
        TransientLLMError("APITimeoutError"),
        Completion(GOOD),
    )
    with caplog.at_level(logging.WARNING, logger="task_spec.llm.client"):
        with pytest.raises(TransientLLMError):
            await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(fake.calls) == 2  # 처음 + 시간 초과 재시도 1번 (예전엔 4번)
    text = caplog.text
    assert "llm retry" in text and "llm give up" in text and "APITimeoutError" in text
    assert "가상 데이터" not in text  # 원문은 로그에 없음


async def test_rate_limit_still_retried_three_times() -> None:
    fake = FakeBackend().push(*[TransientLLMError("RateLimitError")] * 3, Completion(GOOD))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert r.provenance.retries == 3


TRUNC_BROKEN = Completion('{"claims": [{"action": "가상 원문', Usage(500, 4096, 3000), "length")
TRUNC_EMPTY = Completion("", Usage(500, 4096, 4096), "length")


async def test_default_path_keeps_schema_reask_on_truncation() -> None:
    # 기본(split_on_truncation=False): 잘림도 예전처럼 형식 재요청 1회 → 성공하면 받는다
    fake = FakeBackend().push(TRUNC_BROKEN, Completion(GOOD))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(fake.calls) == 2
    assert r.provenance.retries == 1


async def test_default_path_raises_same_exception_types_as_before() -> None:
    fake = FakeBackend().push(TRUNC_BROKEN, TRUNC_BROKEN)
    with pytest.raises(SchemaViolation) as ei:
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert type(ei.value) is SchemaViolation
    fake = FakeBackend().push(TRUNC_EMPTY, TRUNC_EMPTY)  # 같은 설정 재시도 뒤에도 빔
    with pytest.raises(EmptyResponse) as ee:
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert type(ee.value) is EmptyResponse  # OutputTruncated 가 아니다


async def test_split_mode_truncated_raises_without_reask_and_without_content() -> None:
    fake = FakeBackend().push(TRUNC_BROKEN)
    with pytest.raises(OutputTruncated) as ei:
        await _client(fake).call(_prompt(), ClaimExtractionOutput, split_on_truncation=True)
    assert len(fake.calls) == 1  # 형식 오류를 알려 다시 물어도 같은 곳에서 잘린다
    assert "가상 원문" not in str(ei.value)
    assert isinstance(ei.value, EmptyResponse)  # 호출자의 실패 처리는 그대로 받는다
    prov = ei.value.provenance
    assert prov is not None and prov.tokens.out == 4096  # 잘린 호출의 비용도 남는다


async def test_split_mode_truncated_empty_with_low_effort_resamples_then_raises() -> None:
    fake = FakeBackend().push(TRUNC_EMPTY, TRUNC_EMPTY)
    with pytest.raises(OutputTruncated) as ei:
        await _client(fake).call(_prompt(), ClaimExtractionOutput, split_on_truncation=True)
    assert len(fake.calls) == 2  # 같은 설정으로 1회 다시 부른 뒤에야 나누라고 돌려준다
    assert fake.calls[0][1] == fake.calls[1][1]
    prov = ei.value.provenance
    assert prov is not None and prov.retries == 1 and prov.tokens.out == 2 * 4096


async def test_split_mode_truncated_empty_resample_can_succeed() -> None:
    fake = FakeBackend().push(TRUNC_EMPTY, Completion(GOOD, Usage(500, 900, 600)))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput, split_on_truncation=True)
    assert len(r.parsed.claims) == 1
    assert r.provenance.retries == 1
    assert r.provenance.tokens.out == 4096 + 900


async def test_split_mode_tries_low_effort_first() -> None:
    prompt = _prompt().model_copy(update={"params": {"temperature": 0}})
    fake = FakeBackend().push(TRUNC_BROKEN, TRUNC_EMPTY, TRUNC_EMPTY)
    with pytest.raises(OutputTruncated) as ei:
        await _client(fake).call(prompt, ClaimExtractionOutput, split_on_truncation=True)
    assert len(fake.calls) == 3  # low 로 1회 → 빈+잘림이라 같은 설정으로 1회 → 포기
    assert fake.calls[1][1]["reasoning_effort"] == "low"
    assert fake.calls[2][1]["reasoning_effort"] == "low"
    assert ei.value.provenance is not None and ei.value.provenance.retries == 2


async def test_split_mode_complete_json_at_length_is_accepted() -> None:
    fake = FakeBackend().push(Completion(GOOD, Usage(100, 4096, 3000), "length"))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput, split_on_truncation=True)
    assert len(r.parsed.claims) == 1


async def test_default_path_empty_truncated_resamples_and_succeeds() -> None:
    fake = FakeBackend().push(TRUNC_EMPTY, Completion(GOOD, Usage(500, 900, 600)))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(r.parsed.claims) == 1
    assert len(fake.calls) == 2
    assert fake.calls[0][1] == fake.calls[1][1]  # 같은 설정 (이미 low)
    assert r.provenance.retries == 1
    # 두 호출 비용을 모두 남긴다
    assert (r.provenance.tokens.in_, r.provenance.tokens.out) == (1000, 4096 + 900)


async def test_default_path_empty_truncated_twice_raises_with_both_costs() -> None:
    fake = FakeBackend().push(TRUNC_EMPTY, TRUNC_EMPTY)
    with pytest.raises(EmptyResponse) as ei:
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert type(ei.value) is EmptyResponse
    assert len(fake.calls) == 2
    prov = ei.value.provenance
    assert prov is not None and prov.retries == 1 and prov.tokens.out == 2 * 4096


async def test_empty_without_truncation_keeps_old_behavior() -> None:
    # 잘리지 않은 빈 응답(이미 low) → 다시 부르지 않고 바로 EmptyResponse (예전 그대로)
    fake = FakeBackend().push(Completion("", Usage(500, 10, 0), "stop"), Completion(GOOD))
    with pytest.raises(EmptyResponse):
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(fake.calls) == 1
