"""LLM 호출 1건 = 조립된 프롬프트 → 검증된 출력 + provenance (llm-client 스킬).

재시도 정책 (이 한 곳에만 있다)
| 증상                               | 처리                                         |
| 빈 응답 (추론에 토큰 소진 등)        | reasoning_effort=low 로 1회                   |
| TransientLLMError (타임아웃·429·5xx) | 지수 백오프 최대 3회                          |
| JSON 파싱·스키마 검증 실패           | 검증 에러를 붙여 1회 재요청, 그래도 실패면 예외 |

메시지 본문·키 값은 로그·provenance·예외 메시지에 넣지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from task_spec.llm.backend import ChatBackend, Completion, TransientLLMError, Usage
from task_spec.llm.pricing import cost_usd
from task_spec.prompting import AssembledPrompt, Message
from task_spec.schemas import Provenance, TokenUsage

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# 프로바이더별로 백엔드에 넘길 수 있는 파라미터. 나머지는 걸러내고 provenance 에 기록한다.
# OpenAI 추론 모델(gpt-5 계열·o 계열)은 temperature·top_p 기본값만 받는다 → 보내지 않는다 (provenance 의 dropped 에 남음).
# max_tokens 는 langchain-openai 가 max_completion_tokens 로 바꿔 보낸다.
REASONING_ONLY_DEFAULT = frozenset({"temperature", "top_p"})
_REASONING_MODEL = re.compile(r"^(gpt-5|o\d)")


def is_reasoning_model(provider: str, model: str) -> bool:
    return provider == "openai" and bool(_REASONING_MODEL.match(model))


ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "openai": frozenset({"temperature", "max_tokens", "reasoning_effort", "top_p", "seed"}),
    "opencode": frozenset({"temperature", "max_tokens", "reasoning_effort", "top_p", "seed"}),
}

MAX_TRANSIENT_RETRIES = 3
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class SchemaViolation(Exception):
    """재요청 후에도 출력이 스키마를 만족하지 못함."""


class EmptyResponse(Exception):
    """재시도 후에도 빈 응답."""


class LLMResult(BaseModel, Generic[T]):
    parsed: T
    provenance: Provenance


def _parse_json(content: str) -> Any:
    return json.loads(_FENCE.sub("", content.strip()))


def _validation_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()[:10]
        )
    return "JSON 파싱 실패"


class LLMClient:
    def __init__(
        self,
        backends: Mapping[str, ChatBackend],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff: float = 0.5,
    ) -> None:
        self._backends = dict(backends)
        self._sleep = sleep
        self._backoff = backoff

    def has_provider(self, provider: str) -> bool:
        return provider in self._backends

    async def call(self, prompt: AssembledPrompt, output: type[T] | None = None) -> LLMResult[T]:
        out_model: type[T] = output or prompt.output_model  # type: ignore[assignment]
        provider, _, model = prompt.model.partition(":")
        backend = self._backends.get(provider)
        if backend is None:
            raise ValueError(f"설정되지 않은 프로바이더: {provider}")

        allowed = ALLOWED_PARAMS.get(provider, frozenset())
        params = {
            k: v
            for k, v in prompt.params.items()
            if k in allowed
            and not (k in REASONING_ONLY_DEFAULT and is_reasoning_model(provider, model))
        }
        dropped = sorted(set(prompt.params) - set(params))

        messages = list(prompt.messages)
        retries: list[str] = []
        total = Usage()
        started = time.monotonic()
        schema_retry_used = False
        empty_retry_used = False

        while True:
            completion = await self._complete_with_backoff(
                backend, model, messages, params, retries
            )
            total = Usage(
                total.input + completion.usage.input,
                total.output + completion.usage.output,
                total.reasoning + completion.usage.reasoning,
            )

            if not completion.content.strip():
                if empty_retry_used or params.get("reasoning_effort") == "low":
                    raise EmptyResponse(f"{prompt.template.ref}: 빈 응답")
                empty_retry_used = True
                retries.append("empty_response->reasoning_effort=low")
                params = {**params, "reasoning_effort": "low"}
                continue

            try:
                parsed = out_model.model_validate(_parse_json(completion.content))
                break
            except (ValueError, ValidationError) as exc:  # JSONDecodeError 는 ValueError
                summary = _validation_summary(exc)
                if schema_retry_used:
                    raise SchemaViolation(f"{prompt.template.ref}: {summary}") from None
                schema_retry_used = True
                retries.append("schema_violation")
                messages = [
                    *prompt.messages,
                    Message(
                        role="user",
                        content=(
                            "직전 응답이 출력 형식을 만족하지 않았습니다. 오류: "
                            f"{summary}\n형식에 맞는 JSON 만 다시 출력하세요."
                        ),
                    ),
                ]

        latency_ms = int((time.monotonic() - started) * 1000)
        prov = Provenance(
            provenance_id=f"prov-{uuid.uuid4().hex[:12]}",
            prompt=prompt.template.ref,
            prompt_sha256=prompt.template.sha256,
            model=prompt.model,
            params={**params, **({"dropped": dropped} if dropped else {})},
            fewshot_ids=prompt.fewshot_ids,
            criteria_ids=prompt.criteria_ids,
            ontology_version=prompt.ontology_version,
            retries=len(retries),
            tokens=TokenUsage(**{"in": total.input}, out=total.output, reasoning=total.reasoning),
            latency_ms=latency_ms,
            cost_usd=cost_usd(
                prompt.model, total.input, total.output
            ),  # output 에 추론 토큰 포함 (OpenAI 호환 usage)
        )
        logger.info(
            "llm call prompt=%s model=%s retries=%s reasons=%s tokens_in=%d tokens_out=%d latency_ms=%d",
            prov.prompt,
            prov.model,
            prov.retries,
            ",".join(retries) or "-",
            total.input,
            total.output,
            latency_ms,
        )
        return LLMResult[out_model](parsed=parsed, provenance=prov)  # type: ignore[valid-type]

    async def _complete_with_backoff(
        self,
        backend: ChatBackend,
        model: str,
        messages: list[Message],
        params: dict[str, Any],
        retries: list[str],
    ) -> Completion:
        attempt = 0
        while True:
            try:
                return await backend.complete(model, messages, params)
            except TransientLLMError as exc:
                if attempt >= MAX_TRANSIENT_RETRIES:
                    raise
                retries.append(f"transient:{exc}")
                await self._sleep(self._backoff * (2**attempt))
                attempt += 1
