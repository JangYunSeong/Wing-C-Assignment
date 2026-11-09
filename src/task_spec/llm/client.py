"""LLM 호출 1건 = 조립된 프롬프트 → 검증된 출력 + provenance (llm-client 스킬).

재시도 정책 (이 한 곳에만 있다)
| 증상                               | 처리                                         |
| 빈 응답 (추론에 토큰 소진 등)        | reasoning_effort=low 로 1회                   |
| 빈 응답 + 잘림, 추론 설정이 이미 low | 최대 MAX_RESAMPLES 회 다시 — 같은 설정, reasoning_off_last 호출(빌드 생성기)만 마지막은 추론 끔(NO_REASONING, 확인한 프로바이더만) — 잘리지 않은 빈 응답은 예외 그대로 |
| TransientLLMError (429·5xx·연결)     | 지수 백오프 최대 3회                          |
| 시간 초과 (APITimeoutError)          | 1회만 다시 (요청 제한 시간 LLM_TIMEOUT_SECONDS, 기본 300초) |
| JSON 파싱·스키마 검증 실패           | 검증 에러를 붙여 1회 재요청, 그래도 실패면 예외 |
| 출력 잘림 (finish_reason=length)     | 호출자가 split_on_truncation=True 일 때만: (추론 설정이 low 가 아니면 low 로 1회 · 빈 응답이면 같은 설정으로 1회 — MAX_SPLIT_RESAMPLES) → OutputTruncated, 형식 재요청 없음 — 호출자가 입력을 나눈다. 기본(False)은 위 규칙 그대로 |

재시도·포기는 WARNING/ERROR 로그(프롬프트 ref·모델·사유·경과 ms)로 남긴다.
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
# 빈 응답 + 잘림(추론이 상한을 다 씀)을 같은 설정으로 다시 부르는 횟수. 2026-11-09 측정: qwen3.8-flash 는 low 에서 추론이
# 정확히 4096 토큰에서 끊기고 본문은 ~2k 토큰 — 빈 응답은 그 예산이 듣지 않는 가끔의 폭주라, 상한을 낮춰 빨리 실패시키고 여러 번 다시 부른다
MAX_RESAMPLES = 3
# 입력을 나눌 수 있는 호출(split_on_truncation — 주장 추출)은 1회 뒤 바로 돌려준다 (나눠 다시 부르는 편이 빠르다)
# 마지막 재시도에서 추론을 끄는 값 (프로바이더별 — 확인한 곳만). 2026-11-09 확인: OpenCode qwen3.8-flash 는
# reasoning_effort=none 이면 추론 0 으로 바로 답한다 (minimal 은 low 와 같음). 모르는 프로바이더는 low 로 다시 부른다
NO_REASONING: dict[str, str] = {"opencode": "none"}
MAX_SPLIT_RESAMPLES = 1
# 시간 초과는 1번만 다시 시도한다 — 출력이 길어 생긴 초과는 같은 요청을 반복해도 대개 다시 넘는다
# (2026-11-03 운영: 120초 × 4회 × 호출 여러 번으로 draft_spec 이 수십 분 멈춤)
MAX_TIMEOUT_RETRIES = 1
_TIMEOUT_ERRORS = frozenset({"APITimeoutError", "TimeoutError", "ReadTimeout"})
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class SchemaViolation(Exception):
    """재요청 후에도 출력이 스키마를 만족하지 못함. provenance = 실패까지 쓴 토큰·비용 (본문 없음)."""

    provenance: Provenance | None = None


class EmptyResponse(Exception):
    """재시도 후에도 빈 응답. provenance = 실패까지 쓴 토큰·비용 (본문 없음)."""

    provenance: Provenance | None = None


class OutputTruncated(EmptyResponse):
    """출력 상한(max_tokens)에 걸려 응답이 잘림 (finish_reason=length) — split_on_truncation=True 호출에서만.

    같은 입력을 다시 보내도 같은 곳에서 잘리므로 형식 재요청을 하지 않는다 — 호출자가 입력을 나눠 다시 부른다.
    추론 모델은 추론 토큰이 상한을 먼저 써서 짧은 입력에서도 잘린다 (2026-11-04 진단: 입력 1.8k 토큰,
    추론 2.9k~4.1k 토큰 / 상한 4096). EmptyResponse 의 하위 클래스라 호출자의 실패 처리는 그대로 받는다.
    provenance = 잘린 호출까지 쓴 토큰·비용 (본문 없음).
    """


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

    async def call(
        self,
        prompt: AssembledPrompt,
        output: type[T] | None = None,
        *,
        split_on_truncation: bool = False,
        reasoning_off_last: bool = False,
    ) -> LLMResult[T]:
        """split_on_truncation=True: 잘린 응답(finish_reason=length)을 형식 재요청 없이 OutputTruncated 로
        돌려준다 (추론 설정이 low 가 아니면 low 로 1회 먼저). 입력을 나눠 다시 부를 수 있는 호출자만 켠다.
        reasoning_off_last=True: 빈 응답+잘림 재시도의 마지막 한 번을 추론 없이 (NO_REASONING). 출력을 정적 검사·수리 루프가
        다시 보는 빌드 생성기(계약·시작 코드·테스트·참조 구현)만 켠다 — 판정·선택·겹침 확인 같은 판단 호출은 끄고 둔다
        (추론 없는 판단이 지원자 결과에 섞이지 않게, 2026-11-09 사용자 결정)."""
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
        resamples = 0  # 빈 응답 + 잘림 (이미 low) → 같은 설정으로 MAX_RESAMPLES 회까지

        def record() -> Provenance:
            latency_ms = int((time.monotonic() - started) * 1000)
            return Provenance(
                provenance_id=f"prov-{uuid.uuid4().hex[:12]}",
                prompt=prompt.template.ref,
                prompt_sha256=prompt.template.sha256,
                model=prompt.model,
                params={**params, **({"dropped": dropped} if dropped else {})},
                fewshot_ids=prompt.fewshot_ids,
                criteria_ids=prompt.criteria_ids,
                ontology_version=prompt.ontology_version,
                retries=len(retries),
                tokens=TokenUsage(
                    **{"in": total.input}, out=total.output, reasoning=total.reasoning
                ),
                latency_ms=latency_ms,
                cost_usd=cost_usd(
                    prompt.model, total.input, total.output
                ),  # output 에 추론 토큰 포함 (OpenAI 호환 usage)
            )

        while True:
            completion = await self._complete_with_backoff(
                backend, model, messages, params, retries, ref=prompt.template.ref, started=started
            )
            total = Usage(
                total.input + completion.usage.input,
                total.output + completion.usage.output,
                total.reasoning + completion.usage.reasoning,
            )

            if split_on_truncation and completion.finish_reason == "length":
                summary = "빈 응답"
                if completion.content.strip():
                    try:  # 상한에 딱 맞게 끝난 완전한 JSON 은 그대로 받는다
                        parsed = out_model.model_validate(_parse_json(completion.content))
                        break
                    except (ValueError, ValidationError) as exc:
                        summary = _validation_summary(exc)
                if (
                    not completion.content.strip()
                    and params.get("reasoning_effort") == "low"
                    and resamples < MAX_SPLIT_RESAMPLES
                ):
                    resamples += 1
                    retries.append("empty_truncated->resample")
                    logger.warning(
                        "llm retry prompt=%s model=%s reason=empty_truncated elapsed_ms=%d",
                        prompt.template.ref,
                        prompt.model,
                        int((time.monotonic() - started) * 1000),
                    )
                    continue
                if not empty_retry_used and params.get("reasoning_effort") != "low":
                    empty_retry_used = True
                    retries.append("output_truncated->reasoning_effort=low")
                    logger.warning(
                        "llm retry prompt=%s model=%s reason=output_truncated elapsed_ms=%d",
                        prompt.template.ref,
                        prompt.model,
                        int((time.monotonic() - started) * 1000),
                    )
                    params = {**params, "reasoning_effort": "low"}
                    continue
                cut = OutputTruncated(f"{prompt.template.ref}: 출력 잘림 — {summary}")
                cut.provenance = record()
                logger.warning(
                    "llm truncated prompt=%s model=%s reason=output_truncated elapsed_ms=%d",
                    prompt.template.ref,
                    prompt.model,
                    cut.provenance.latency_ms,
                )
                raise cut

            if not completion.content.strip():
                if (
                    completion.finish_reason == "length"
                    and params.get("reasoning_effort") == "low"
                    and resamples < MAX_RESAMPLES
                ):  # 추론이 상한을 다 씀 — 추론 길이는 호출마다 달라 같은 설정으로 다시 부르면 대개 산다
                    resamples += 1
                    # 마지막 한 번은 추론을 끈다 — 폭주가 이어져도 빈 응답으로 끝나지 않게 (품질은 정적 검사·수리 루프가 본다)
                    off = (
                        NO_REASONING.get(provider)
                        if reasoning_off_last and resamples == MAX_RESAMPLES
                        else None
                    )
                    retries.append(
                        f"empty_truncated->reasoning_effort={off}"
                        if off
                        else "empty_truncated->resample"
                    )
                    logger.warning(
                        "llm retry prompt=%s model=%s reason=empty_truncated%s elapsed_ms=%d",
                        prompt.template.ref,
                        prompt.model,
                        f" next=reasoning_effort={off}" if off else "",
                        int((time.monotonic() - started) * 1000),
                    )
                    if off:
                        params = {**params, "reasoning_effort": off}
                    continue
                if empty_retry_used or params.get("reasoning_effort") in (
                    "low",
                    *NO_REASONING.values(),
                ):
                    empty = EmptyResponse(f"{prompt.template.ref}: 빈 응답")
                    empty.provenance = record()
                    raise empty
                empty_retry_used = True
                retries.append("empty_response->reasoning_effort=low")
                logger.warning(
                    "llm retry prompt=%s model=%s reason=empty_response elapsed_ms=%d",
                    prompt.template.ref,
                    prompt.model,
                    int((time.monotonic() - started) * 1000),
                )
                params = {**params, "reasoning_effort": "low"}
                continue

            try:
                parsed = out_model.model_validate(_parse_json(completion.content))
                break
            except (ValueError, ValidationError) as exc:  # JSONDecodeError 는 ValueError
                summary = _validation_summary(exc)
                if schema_retry_used:
                    bad = SchemaViolation(f"{prompt.template.ref}: {summary}")
                    bad.provenance = record()
                    raise bad from None
                schema_retry_used = True
                retries.append("schema_violation")
                logger.warning(
                    "llm retry prompt=%s model=%s reason=schema_violation elapsed_ms=%d",
                    prompt.template.ref,
                    prompt.model,
                    int((time.monotonic() - started) * 1000),
                )
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

        prov = record()
        latency_ms = prov.latency_ms
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
        *,
        ref: str = "",
        started: float | None = None,
    ) -> Completion:
        attempt = 0
        timeouts = 0
        t0 = started if started is not None else time.monotonic()
        while True:
            try:
                return await backend.complete(model, messages, params)
            except TransientLLMError as exc:
                kind = str(exc)
                is_timeout = kind in _TIMEOUT_ERRORS
                elapsed = int((time.monotonic() - t0) * 1000)
                if attempt >= MAX_TRANSIENT_RETRIES or (
                    is_timeout and timeouts >= MAX_TIMEOUT_RETRIES
                ):
                    logger.error(
                        "llm give up prompt=%s model=%s reason=%s attempts=%d elapsed_ms=%d",
                        ref,
                        model,
                        kind,
                        attempt + 1,
                        elapsed,
                    )
                    raise
                timeouts += int(is_timeout)
                retries.append(f"transient:{kind}")
                logger.warning(
                    "llm retry prompt=%s model=%s reason=%s attempt=%d elapsed_ms=%d",
                    ref,
                    model,
                    kind,
                    attempt + 1,
                    elapsed,
                )
                await self._sleep(self._backoff * (2**attempt))
                attempt += 1
