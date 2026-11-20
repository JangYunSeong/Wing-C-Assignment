from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from task_spec.llm import (
    Completion,
    FakeBackend,
    LLMClient,
    SchemaViolation,
    TransientLLMError,
    Usage,
)
from task_spec.llm.client import (
    MAX_RESAMPLES,
    EmptyResponse,
    OutputTruncated,
    _parse_json,
    _violation_kinds,
)
from task_spec.nodes.pseudonymize import mint_pseudonymized
from task_spec.prompting import AssembledPrompt, PromptRegistry, assemble
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


async def test_provenance_hash_covers_rendered_output_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #240: 같은 ref·같은 vN 파일이어도 출력 모델이 바뀌면 {{output_schema}} 가 바뀐다 — provenance 해시도 달라야 한다
    # (예전엔 파일 바이트 해시라 plan_contract@31 + sha 가 서로 다른 프롬프트를 가리킬 수 있었다)
    from pydantic import Field

    from task_spec import schemas

    cur = PROMPTS.get("extract_claims")
    (tmp_path / "extract_claims").mkdir()
    (tmp_path / "extract_claims" / cur.path.name).write_bytes(cur.path.read_bytes())
    (tmp_path / "extract_claims" / "CURRENT").write_text(f"v{cur.meta.version}", "utf-8")

    async def run() -> tuple[str, list[str], Any]:
        tpl = PromptRegistry(tmp_path).get("extract_claims")
        payload = ClaimExtractionInput(doc_kind="resume", text=mint_pseudonymized(TEXT))
        prompt = assemble(tpl, payload, ontology_version="onto-v2026.11.02")
        r = await _client(FakeBackend().push(Completion(GOOD))).call(prompt, ClaimExtractionOutput)
        return tpl.sha256, [m.content for m in prompt.messages], r.provenance

    file1, sent1, p1 = await run()

    class ClaimExtractionOutputChanged(ClaimExtractionOutput):
        note: str | None = Field(default=None, description="가상 설명")

    monkeypatch.setattr(schemas, "ClaimExtractionOutput", ClaimExtractionOutputChanged)
    file2, sent2, p2 = await run()
    assert (p1.prompt, file1) == (p2.prompt, file2)  # 같은 ref, 같은 파일
    assert sent1 != sent2  # 모델이 받는 프롬프트는 다르다
    assert p1.prompt_sha256 != p2.prompt_sha256
    assert p1.prompt_sha256 != file1  # 파일만의 해시가 아니다


async def test_code_fence_is_tolerated() -> None:
    fake = FakeBackend().push(Completion(f"```json\n{GOOD}\n```"))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(r.parsed.claims) == 1


async def test_stream_resets_on_schema_retry_and_preserves_validation() -> None:
    fake = FakeBackend().push(Completion('{"invalid":true}'), Completion(GOOD, Usage(10, 5)))
    received = []

    async def consume(chunk):
        received.append(chunk)

    result = await _client(fake).call(_prompt(), ClaimExtractionOutput, on_delta=consume)
    assert received.count(None) == 2
    last_reset = len(received) - 1 - received[::-1].index(None)
    assert "".join(received[last_reset + 1 :]) == GOOD
    assert result.parsed.claims[0].action == "데이터 파이프라인 설계"
    assert result.provenance.retries == 1
    assert result.provenance.tokens.out == 5


async def test_empty_response_retries_with_low_effort() -> None:
    prompt = _prompt()
    prompt = prompt.model_copy(update={"params": {"temperature": 0}})  # 기본 추론 설정 상황
    fake = FakeBackend().push(Completion("", Usage(500, 4096, 4096), "length"), Completion(GOOD))
    r = await _client(fake).call(prompt, ClaimExtractionOutput)
    assert r.provenance.retries == 1
    assert fake.calls[1][1]["reasoning_effort"] == "low"
    assert r.provenance.params["reasoning_effort"] == "low"


async def test_empty_response_resamples_up_to_the_limit_then_raises() -> None:
    fake = FakeBackend().push(*[Completion("")] * (MAX_RESAMPLES + 1))
    with pytest.raises(EmptyResponse):
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(fake.calls) == MAX_RESAMPLES + 1


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


async def test_schema_violation_log_names_where_without_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 어디가 깨졌는지 로그로 보이게 — 칸 위치·오류 종류만, 값·메시지는 남기지 않는다 (2026-11-09)
    fake = FakeBackend().push(
        Completion('{"claims": [{"quote": "가상 원문 조각"}]}'), Completion(GOOD)
    )
    with caplog.at_level("WARNING"):
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    line = next(r.getMessage() for r in caplog.records if "schema_violation" in r.getMessage())
    assert "where=claims.0." in line and ":missing" in line
    assert "가상 원문" not in line


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
    fake = FakeBackend().push(*[TRUNC_EMPTY] * (MAX_RESAMPLES + 1))  # 같은 설정 재시도 뒤에도 빔
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


async def test_default_path_empty_truncated_resamples_up_to_the_limit_then_raises() -> None:
    # 추론 폭주는 가끔이라 같은 설정으로 MAX_RESAMPLES 회까지 다시 부른다 (2026-11-09: 1회로는 rep2·rep3 이 4번 연속 빈 응답에 멈춤)
    fake = FakeBackend().push(*[TRUNC_EMPTY] * (MAX_RESAMPLES + 1))
    with pytest.raises(EmptyResponse) as ei:
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert type(ei.value) is EmptyResponse
    assert len(fake.calls) == MAX_RESAMPLES + 1
    prov = ei.value.provenance
    assert prov is not None and prov.retries == MAX_RESAMPLES
    assert prov.tokens.out == (MAX_RESAMPLES + 1) * 4096  # 버려진 호출 비용도 모두 남긴다


async def test_last_resample_turns_reasoning_off_only_when_asked() -> None:
    # 빌드 생성기만: 추론 폭주가 이어지면 마지막 한 번은 추론을 끈다 — 빈 응답으로 루프가 멈추지 않게 (2026-11-09)
    prompt = _prompt()  # opencode:qwen3.8-flash
    fake = FakeBackend().push(*[TRUNC_EMPTY] * MAX_RESAMPLES, Completion(GOOD, Usage(500, 300, 0)))
    r = await _client(fake).call(prompt, ClaimExtractionOutput, reasoning_off_last=True)
    assert len(r.parsed.claims) == 1
    efforts = [c[1].get("reasoning_effort") for c in fake.calls]
    assert efforts == ["low"] * MAX_RESAMPLES + ["none"]
    assert r.provenance.params["reasoning_effort"] == "none"  # 어느 설정으로 살았는지 남는다
    # 추론을 끈 답도 비면 다시 low 로 돌아가지 않고 끝낸다
    fake = FakeBackend().push(*[TRUNC_EMPTY] * (MAX_RESAMPLES + 1))
    with pytest.raises(EmptyResponse):
        await _client(fake).call(prompt, ClaimExtractionOutput, reasoning_off_last=True)
    assert len(fake.calls) == MAX_RESAMPLES + 1
    # 켜지 않은 호출(판정·선택·겹침 확인 등 기본값)은 끝까지 같은 설정 — 추론 없는 판단을 섞지 않는다
    fake = FakeBackend().push(*[TRUNC_EMPTY] * (MAX_RESAMPLES + 1))
    with pytest.raises(EmptyResponse):
        await _client(fake).call(prompt, ClaimExtractionOutput)
    assert [c[1].get("reasoning_effort") for c in fake.calls] == ["low"] * (MAX_RESAMPLES + 1)


async def test_default_path_last_resample_can_succeed() -> None:
    fake = FakeBackend().push(
        *[TRUNC_EMPTY] * MAX_RESAMPLES, Completion(GOOD, Usage(500, 900, 600))
    )
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(r.parsed.claims) == 1 and r.provenance.retries == MAX_RESAMPLES


async def test_empty_without_truncation_is_resampled_too() -> None:
    # 2026-11-15: 잘리지 않은 빈 응답(finish=stop)은 재시도 없이 바로 실패해 알파 둘이 생성기와 무관하게 떨어졌다 — 같은 설정으로 다시
    fake = FakeBackend().push(Completion("", Usage(500, 10, 0), "stop"), Completion(GOOD))
    r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(r.parsed.claims) == 1 and len(fake.calls) == 2 and r.provenance.retries == 1
    assert fake.calls[0][1] == fake.calls[1][1]  # 같은 설정
    # 내용 필터는 같은 입력에 같은 결정 — 다시 부르지 않는다
    fake = FakeBackend().push(Completion("", Usage(500, 0, 0), "content_filter"), Completion(GOOD))
    with pytest.raises(EmptyResponse):
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert len(fake.calls) == 1


async def test_empty_response_says_how_the_last_call_ended() -> None:
    # 2026-11-15 운영 계약 실패: 빈 응답이 잘림인지 그냥 빈 답인지 builder 로그를 봐야 알 수 있었다
    # (잘리지 않은 빈 응답도 같은 설정으로 다시 부른다 — 다 비면 마지막 끝 이유와 재시도 사유가 예외에 남는다)
    fake = FakeBackend().push(*[Completion("", Usage(500, 10, 0), "stop")] * (MAX_RESAMPLES + 1))
    with pytest.raises(EmptyResponse) as ei:
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert ei.value.finish_reason == "stop" and "finish_reason=stop" in str(ei.value)
    assert "empty_resample->resample" in str(ei.value)
    fake = FakeBackend().push(*[TRUNC_EMPTY] * (MAX_RESAMPLES + 1))
    with pytest.raises(EmptyResponse) as ei:
        await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert ei.value.finish_reason == "length" and "empty_truncated->resample" in str(ei.value)


# --- #236: 재시도에서 reasoning_effort 를 넣을 수 있는 공급자·모델만 ---

# CURRENT 머리말 그대로 — Gemini: gen_variant v3·gen_trap v2 (reasoning_effort 를 받지 않음),
# gpt-4.1-mini: segment_posting v1 (비추론 모델 — 넣으면 요청 오류)
NO_EFFORT_MODELS = [
    ("gemini:gemini-3.8-flash", {"temperature": 0.2, "max_tokens": 12000}),
    ("openai:gpt-4.1-mini", {"temperature": 0, "max_tokens": 4096}),
]
NO_EFFORT_IDS = ["gemini", "gpt-4.1-mini"]
EMPTY_STOP = Completion("", Usage(500, 10, 0), "stop")


def _prompt_as(model: str, params: dict[str, Any]) -> AssembledPrompt:
    """같은 조립 결과를 다른 모델·설정으로 (머리말 model 이 백엔드를 고른다)."""
    base = _prompt()
    meta = base.template.meta.model_copy(update={"model": model, "params": params})
    return base.model_copy(
        update={
            "template": base.template.model_copy(update={"meta": meta}),
            "params": dict(params),
        }
    )


def _client_for(model: str, fake: FakeBackend) -> LLMClient:
    return LLMClient({model.partition(":")[0]: fake}, sleep=_no_sleep)


@pytest.mark.parametrize(("model", "params"), NO_EFFORT_MODELS, ids=NO_EFFORT_IDS)
@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_empty_retry_keeps_settings_when_reasoning_effort_cannot_be_set(
    model: str, params: dict[str, Any], finish: str
) -> None:
    # 2026-11-18 #236: 예전엔 reasoning_effort=low 를 넣어 다시 불렀다 — Gemini 관문은 400 으로 빌드 실패,
    # gpt-4.1-mini(/postings/suggest)는 500. 같은 설정으로 1회 다시 부른다 (빌드 생성기 reasoning_off_last 여도)
    fake = FakeBackend().push(Completion("", Usage(500, 10, 0), finish), Completion(GOOD))
    r = await _client_for(model, fake).call(
        _prompt_as(model, params), ClaimExtractionOutput, reasoning_off_last=True
    )
    assert len(r.parsed.claims) == 1
    assert [c[1] for c in fake.calls] == [params, params]
    assert r.provenance.retries == 1 and "reasoning_effort" not in r.provenance.params


@pytest.mark.parametrize(("model", "params"), NO_EFFORT_MODELS, ids=NO_EFFORT_IDS)
async def test_empty_retry_without_reasoning_control_gives_up_after_one(
    model: str, params: dict[str, Any]
) -> None:
    fake = FakeBackend().push(*[EMPTY_STOP] * (MAX_RESAMPLES + 2))
    with pytest.raises(EmptyResponse) as ei:
        await _client_for(model, fake).call(
            _prompt_as(model, params), ClaimExtractionOutput, reasoning_off_last=True
        )
    assert type(ei.value) is EmptyResponse
    assert [c[1] for c in fake.calls] == [params, params]  # 같은 설정으로 1회만 다시
    assert "재시도 empty_resample->resample)" in str(ei.value)
    assert ei.value.provenance is not None and ei.value.provenance.retries == 1


@pytest.mark.parametrize(("model", "params"), NO_EFFORT_MODELS, ids=NO_EFFORT_IDS)
async def test_split_mode_without_reasoning_control(model: str, params: dict[str, Any]) -> None:
    # 빈+잘림: 같은 설정으로 1회 다시 → 그래도 비면 OutputTruncated (호출자가 입력을 나눈다)
    fake = FakeBackend().push(TRUNC_EMPTY, TRUNC_EMPTY)
    with pytest.raises(OutputTruncated):
        await _client_for(model, fake).call(
            _prompt_as(model, params), ClaimExtractionOutput, split_on_truncation=True
        )
    assert [c[1] for c in fake.calls] == [params, params]
    # 내용이 있는 잘림은 같은 설정이면 같은 곳에서 다시 잘린다 — 바로 OutputTruncated
    fake = FakeBackend().push(TRUNC_BROKEN, Completion(GOOD))
    with pytest.raises(OutputTruncated):
        await _client_for(model, fake).call(
            _prompt_as(model, params), ClaimExtractionOutput, split_on_truncation=True
        )
    assert [c[1] for c in fake.calls] == [params]


async def test_openai_reasoning_model_still_lowers_effort_on_empty() -> None:
    # 추론 모델(gpt-5 계열 — solve_select·solve_task 머리말 medium)은 예전처럼 low 로 1회
    params = {"max_tokens": 8000, "reasoning_effort": "medium"}
    fake = FakeBackend().push(Completion("", Usage(500, 8000, 8000), "length"), Completion(GOOD))
    r = await _client_for("openai:gpt-5.5", fake).call(
        _prompt_as("openai:gpt-5.5", params), ClaimExtractionOutput
    )
    assert [c[1] for c in fake.calls] == [params, {**params, "reasoning_effort": "low"}]
    assert r.provenance.params["reasoning_effort"] == "low"


@pytest.mark.parametrize(
    ("model", "params"),
    [("opencode:qwen3.8-flash", {"temperature": 0}), *NO_EFFORT_MODELS],
    ids=["qwen-no-effort", *NO_EFFORT_IDS],
)
async def test_content_filter_is_never_retried(model: str, params: dict[str, Any]) -> None:
    # #236: 추론 설정을 low 로 바꾸는 첫 재시도 갈래도 내용 필터를 다시 불렀다 — 같은 입력엔 같은 결정
    fake = FakeBackend().push(Completion("", Usage(500, 0, 0), "content_filter"), Completion(GOOD))
    with pytest.raises(EmptyResponse) as ei:
        await _client_for(model, fake).call(_prompt_as(model, params), ClaimExtractionOutput)
    assert len(fake.calls) == 1
    assert ei.value.finish_reason == "content_filter" and "재시도 없음" in str(ei.value)


async def test_qwen_retry_ladder_is_pinned(caplog: pytest.LogCaptureFixture) -> None:
    # 운영 주력(opencode:qwen3.8-flash)의 재시도 사다리 — params 순서·retries 문자열까지 #236 고침 전과 같다
    def seen(fake: FakeBackend) -> list[list[tuple[str, Any]]]:
        return [list(c[1].items()) for c in fake.calls]

    base = [("temperature", 0), ("max_tokens", 4096)]
    low = [*base, ("reasoning_effort", "low")]
    no_effort = _prompt().model_copy(update={"params": dict(base)})
    # 추론 설정 없음 → low 로 1회 → 같은 설정으로 MAX_RESAMPLES 회 → EmptyResponse
    fake = FakeBackend().push(*[EMPTY_STOP] * (MAX_RESAMPLES + 2))
    with pytest.raises(EmptyResponse) as ei:
        await _client(fake).call(no_effort, ClaimExtractionOutput)
    assert seen(fake) == [base, *[low] * (MAX_RESAMPLES + 1)]
    assert str(ei.value).endswith(
        "재시도 empty_response->reasoning_effort=low,"
        + ",".join(["empty_resample->resample"] * MAX_RESAMPLES)
        + ")"
    )
    # CURRENT 머리말(low) + 빌드 생성기: 마지막 한 번은 추론 끔
    fake = FakeBackend().push(*[TRUNC_EMPTY] * (MAX_RESAMPLES + 1))
    with pytest.raises(EmptyResponse) as ei:
        await _client(fake).call(_prompt(), ClaimExtractionOutput, reasoning_off_last=True)
    assert seen(fake) == [*[low] * MAX_RESAMPLES, [*base, ("reasoning_effort", "none")]]
    assert str(ei.value).endswith(
        "재시도 "
        + ",".join(["empty_truncated->resample"] * (MAX_RESAMPLES - 1))
        + ",empty_truncated->reasoning_effort=none)"
    )
    # split 모드: 잘림 → low 로 1회 → 빈+잘림 같은 설정 1회 → 성공
    fake = FakeBackend().push(TRUNC_BROKEN, TRUNC_EMPTY, Completion(GOOD))
    with caplog.at_level(logging.INFO, logger="task_spec.llm.client"):
        r = await _client(fake).call(no_effort, ClaimExtractionOutput, split_on_truncation=True)
    assert seen(fake) == [base, low, low]
    assert list(r.provenance.params.items()) == low and r.provenance.retries == 2
    assert (
        "reasons=output_truncated->reasoning_effort=low,empty_truncated->resample " in caplog.text
    )


# ================================================================ 깨진 JSON 고쳐 읽기·진단 (#280 D1·D2)


def test_valid_json_is_read_without_repairs() -> None:
    assert _parse_json(GOOD) == (json.loads(GOOD), ())


@pytest.mark.parametrize(
    ("raw", "value", "repairs"),
    [
        # 문자열 안 날 줄바꿈·탭 — 코드가 든 자료에서 흔하다
        ('{"code": "if x:\n\treturn 1"}', "if x:\n\treturn 1", ("control_char",)),
        # JSON 에 없는 이스케이프(정규식 \d) — 글자 역슬래시로. 맞는 이스케이프(\\, \u0041, \")는 그대로
        ('{"code": "re(\\d+) \\\\s \\u0041 \\"q\\""}', 're(\\d+) \\s A "q"', ("bad_escape",)),
        ('{"code": "\\u12zz"}', "\\u12zz", ("bad_escape",)),
        # 앞뒤 군더더기 — 설명 문장, 두 번째 JSON
        ('결과입니다:\n{"a": 1}\n이상입니다.', None, ("extra_text",)),
        ('{"a": 1}{"a": 2}', None, ("extra_text",)),
    ],
)
def test_common_breakage_is_repaired(raw: str, value: str | None, repairs: tuple[str, ...]) -> None:
    data, fixed = _parse_json(raw)
    assert fixed == repairs
    assert data == ({"a": 1} if value is None else {"code": value})


@pytest.mark.parametrize("raw", ['{"a": "가상 원문 조각', "not json 가상 원문", '{"a": 1, }'])
def test_truncated_or_unreadable_json_still_fails(raw: str) -> None:
    with pytest.raises(json.JSONDecodeError):  # 잘린 JSON 은 고치지 않는다 (finish_reason 은 따로)
        _parse_json(raw)


def test_json_parse_diagnosis_names_kind_and_place_without_content() -> None:
    with pytest.raises(json.JSONDecodeError) as ei:
        _parse_json('{"a": "가상 원문 조각')
    kind = _violation_kinds(ei.value)
    assert kind.startswith("json_parse:unterminated_string") and "/open" in kind and "len=" in kind
    assert "가상" not in kind
    with pytest.raises(json.JSONDecodeError) as ei2:  # 고쳐 읽기도 실패 — 원래 오류 > 마지막 오류
        _parse_json('{"a": "줄\n바꿈", "b" 1}')
    assert ">" in _violation_kinds(ei2.value) and "줄" not in _violation_kinds(ei2.value)


async def test_repaired_json_is_accepted_without_a_retry(caplog: pytest.LogCaptureFixture) -> None:
    broken = GOOD.replace("설계했다.", "설계\n했다.")  # 문자열 안 날 줄바꿈
    fake = FakeBackend().push(Completion(broken))
    with caplog.at_level("INFO"):
        r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert r.provenance.retries == 0 and len(fake.calls) == 1  # 수 분짜리 재생성을 하지 않는다
    line = next(rec.getMessage() for rec in caplog.records if "llm call" in rec.getMessage())
    assert "repairs=control_char" in line and "설계" not in line


async def test_unrepairable_json_retry_log_says_why(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeBackend().push(Completion('{"claims": [{"action": "가상 원문'), Completion(GOOD))
    with caplog.at_level("WARNING"):
        r = await _client(fake).call(_prompt(), ClaimExtractionOutput)
    assert r.provenance.retries == 1
    line = next(
        rec.getMessage() for rec in caplog.records if "schema_violation" in rec.getMessage()
    )
    assert "where=json_parse:unterminated_string" in line and "가상" not in line
