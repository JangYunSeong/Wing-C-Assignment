"""LLM 호출 1건 = 조립된 프롬프트 → 검증된 출력 + provenance (llm-client 스킬).

재시도 정책 (이 한 곳에만 있다)
| 증상                               | 처리                                         |
| 빈 응답 (추론에 토큰 소진 등)        | reasoning_effort=low 로 1회 — 추론 설정을 바꿀 수 있을 때만 (can_set_reasoning: 허용 칸에 있고 openai 는 추론 모델) |
| 빈 응답, 추론 설정이 이미 low       | 최대 MAX_RESAMPLES 회 다시 — 같은 설정(잘렸든 아니든), reasoning_off_last 호출(빌드 생성기)만 마지막은 추론 끔(NO_REASONING, 확인한 프로바이더만) |
| 빈 응답, 추론 설정을 못 바꾸는 모델   | 같은 설정으로 MAX_FIXED_RESAMPLES(1)회 다시 → 그래도 비면 EmptyResponse (Gemini·gpt-4.1 계열, #236) |
| 빈 응답, 내용 필터(content_filter)   | 다시 부르지 않고 EmptyResponse — 같은 입력에 같은 결정 |
| TransientLLMError (429·5xx·연결)     | 지수 백오프 최대 3회                          |
| 시간 초과 (APITimeoutError)          | 1회만 다시 (요청 제한 시간 LLM_TIMEOUT_SECONDS, 기본 300초) |
| JSON 파싱·스키마 검증 실패           | 검증 에러를 붙여 1회 재요청, 그래도 실패면 예외 |
| 출력 잘림 (finish_reason=length)     | 호출자가 split_on_truncation=True 일 때만: (추론 설정을 바꿀 수 있고 low 가 아니면 low 로 1회 · 빈 응답이면 같은 설정으로 1회 — MAX_SPLIT_RESAMPLES) → OutputTruncated, 형식 재요청 없음 — 호출자가 입력을 나눈다. 기본(False)은 위 규칙 그대로 |

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
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from task_spec.llm.backend import ChatBackend, Completion, TextCallback, TransientLLMError, Usage
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
    # 손 시범 진행자가 응시 플랫폼처럼 Gemini 를 쓴다 (OpenAI 호환 엔드포인트, eval/pilot — 2026-11-13 사용자)
    "gemini": frozenset({"temperature", "max_tokens", "top_p"}),
}


def can_set_reasoning(provider: str, model: str) -> bool:
    """재시도에서 reasoning_effort 를 넣을 수 있나 — 허용 칸에 있고, openai 면 추론 모델일 때만.
    2026-11-18 #236: 빈 응답 재시도가 이 확인 없이 low 를 넣었다 — Gemini 는 이 칸을 받지 않고(허용 칸에 없음),
    gpt-4.1(-mini) 는 비추론 모델이라 요청 오류(400)로 빌드 관문·/postings/suggest 가 실패했다."""
    if "reasoning_effort" not in ALLOWED_PARAMS.get(provider, frozenset()):
        return False
    return provider != "openai" or is_reasoning_model(provider, model)


MAX_TRANSIENT_RETRIES = 3
# 빈 응답 + 잘림(추론이 상한을 다 씀)을 같은 설정으로 다시 부르는 횟수. 2026-11-09 측정: qwen3.8-flash 는 low 에서 추론이
# 정확히 4096 토큰에서 끊기고 본문은 ~2k 토큰 — 빈 응답은 그 예산이 듣지 않는 가끔의 폭주라, 상한을 낮춰 빨리 실패시키고 여러 번 다시 부른다
MAX_RESAMPLES = 3
# 입력을 나눌 수 있는 호출(split_on_truncation — 주장 추출)은 1회 뒤 바로 돌려준다 (나눠 다시 부르는 편이 빠르다)
# 마지막 재시도에서 추론을 끄는 값 (프로바이더별 — 확인한 곳만). 2026-11-09 확인: OpenCode qwen3.8-flash 는
# reasoning_effort=none 이면 추론 0 으로 바로 답한다 (minimal 은 low 와 같음). 모르는 프로바이더는 low 로 다시 부른다
NO_REASONING: dict[str, str] = {"opencode": "none"}
MAX_SPLIT_RESAMPLES = 1
# 추론 설정을 바꿀 수 없는 공급자·모델(can_set_reasoning 거짓 — Gemini·gpt-4.1 계열)의 빈 응답은 같은 설정으로 이만큼만 다시.
# 같은 설정 여러 번(MAX_RESAMPLES)은 qwen 의 추론 폭주를 잰 값이라 넓히지 않는다 (2026-11-18 #236)
MAX_FIXED_RESAMPLES = 1
# 시간 초과는 1번만 다시 시도한다 — 출력이 길어 생긴 초과는 같은 요청을 반복해도 대개 다시 넘는다
# (2026-11-03 운영: 120초 × 4회 × 호출 여러 번으로 draft_spec 이 수십 분 멈춤)
MAX_TIMEOUT_RETRIES = 1
_TIMEOUT_ERRORS = frozenset({"APITimeoutError", "TimeoutError", "ReadTimeout"})
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class SchemaViolation(Exception):
    """재요청 후에도 출력이 스키마를 만족하지 못함. provenance = 실패까지 쓴 토큰·비용 (본문 없음)."""

    provenance: Provenance | None = None


class EmptyResponse(Exception):
    """재시도 후에도 빈 응답. provenance = 실패까지 쓴 토큰·비용 (본문 없음).
    finish_reason = 마지막 호출이 끝난 이유 — length 면 추론·출력이 상한을 다 씀(잘림), stop 이면 그냥 빈 답.
    2026-11-15 운영 계약 실패: 빈 응답이 잘림인지 그냥 빈 답인지 실패 사유로는 알 수 없어 builder 로그를 봐야 했다."""

    provenance: Provenance | None = None
    finish_reason: str | None = None


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


_SIMPLE_ESCAPES = frozenset('"\\/bfnrt')
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")


def _fix_escapes(text: str) -> str:
    """JSON 에 없는 역슬래시 이스케이프(\\d·\\s 같은 정규식, 윈도 경로)를 글자 역슬래시로 — 맞는 이스케이프는 그대로."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "\\":
            out.append(text[i])
            i += 1
            continue
        nxt = text[i + 1 : i + 2]
        if nxt == "u" and _HEX4.fullmatch(text[i + 2 : i + 6]):
            out.append(text[i : i + 6])
            i += 6
        elif nxt and nxt in _SIMPLE_ESCAPES:
            out.append(text[i : i + 2])
            i += 2
        else:
            out.append("\\\\")
            i += 1
    return "".join(out)


def _parse_json(content: str) -> tuple[Any, tuple[str, ...]]:
    """JSON 을 읽고, 고쳐 읽었으면 고친 종류를 함께 돌려준다 (#280 D2 — 형식 오류 재시도 한 번이 수 분짜리 재생성이다).

    모델이 흔히 깨뜨리는 꼴만 결정적으로 고친다: 문자열 안 제어 문자(줄바꿈·탭 — 코드가 든 자료), JSON 에 없는 역슬래시
    이스케이프(정규식 \\d), JSON 앞뒤 군더더기(설명 문장·두 번째 JSON). 잘린 JSON 은 고치지 않는다(finish_reason=length 는
    따로) — 첫 객체가 닫히지 않으면 그대로 실패. 고친 뒤에도 출력 스키마 검증은 그대로 받는다. 못 고치면 원래 오류를 원인으로
    단 마지막 오류를 던진다 (_violation_kinds 가 둘 다 적는다)."""
    text = _FENCE.sub("", content.strip())
    try:
        return json.loads(text), ()
    except json.JSONDecodeError as first:
        original = first
    lenient = json.JSONDecoder(strict=False)  # 문자열 안 제어 문자 허용
    last = original
    try:
        return lenient.decode(text), ("control_char",)
    except json.JSONDecodeError as exc:
        last = exc
    fixed = _fix_escapes(text)
    escaped: tuple[str, ...] = ("bad_escape",) if fixed != text else ()
    if escaped:
        try:
            return lenient.decode(fixed), escaped
        except json.JSONDecodeError as exc:
            last = exc
    starts = [i for i in (fixed.find("{"), fixed.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        try:
            value, end = lenient.raw_decode(fixed, start)
            return value, (*escaped, "extra_text")
        except json.JSONDecodeError as exc:
            last = exc
    if last is original:
        raise original
    raise last from original


def _decode_kind(exc: json.JSONDecodeError) -> str:
    """json 모듈의 고정 문구(입력 글 없음)·위치 비율·길이·끝이 닫혔는지 — 원문 없이 원인을 가른다 (#280 D1: 이스케이프·제어
    문자·군더더기·잘림)."""
    kind = re.sub(r"[^a-z]+", "_", exc.msg.lower()).strip("_")
    size = len(exc.doc)
    pct = exc.pos * 100 // size if size else 0
    tail = "closed" if exc.doc.rstrip().endswith(("}", "]")) else "open"
    return f"{kind}@{pct}%/len={size}/{tail}"


def _violation_kinds(exc: Exception) -> str:
    """로그용 — 어느 칸이 어떤 종류로 깨졌는지만 (위치·오류 종류). 메시지·값은 넣지 않는다 (원문이 섞일 수 있음)."""
    if isinstance(exc, ValidationError):
        # 받은 값의 형식 이름만 (내용 아님) — 목록 칸에 무엇이 왔는지 가리게 (2026-11-15 #187 woori001: list_type 이 null 인지 글자인지 몰랐다)
        return ";".join(
            f"{'.'.join(map(str, e['loc'])) or '(root)'}:{e['type']}({type(e.get('input')).__name__})"
            for e in exc.errors()[:5]
        )
    if isinstance(exc, json.JSONDecodeError):
        # 고쳐 읽기도 실패했으면 원래 오류 > 마지막 오류
        cause = exc.__cause__
        if isinstance(cause, json.JSONDecodeError):
            return f"json_parse:{_decode_kind(cause)}>{_decode_kind(exc)}"
        return f"json_parse:{_decode_kind(exc)}"
    return "json_parse"


# 받은 값을 보여 줄 수 있는 꼴 — 영문 식별자 같은 짧은 값만 (자유 글·한글은 원문이 섞일 수 있어 넣지 않는다)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,39}")


def _validation_summary(exc: Exception) -> str:
    """재요청 문구·실패 사유 — 허용 값 밖(literal·enum)이면 받은 값도 앞에 붙인다 (짧은 값만). 2026-11-10 프론트 알파 fe7-dup:
    '이 중 하나여야 함' 목록만 보여 줘 계약 3번이 같은 값을 반복했고, 실패 사유가 300자에서 잘려 무슨 값이었는지도 남지 않았다."""
    if isinstance(exc, ValidationError):
        parts = []
        for e in exc.errors()[:10]:
            loc = ".".join(map(str, e["loc"])) or "(root)"
            got = e.get("input")
            if (
                e["type"] in ("literal_error", "enum")
                and isinstance(got, str)
                and _TOKEN.fullmatch(got)
            ):
                parts.append(f"{loc}: 받은 값 {got!r} 은 허용 값이 아님 — {e['msg']}")
            else:
                parts.append(f"{loc}: {e['msg']}")
        return "; ".join(parts)
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
        on_delta: TextCallback | None = None,
    ) -> LLMResult[T]:
        """split_on_truncation=True: 잘린 응답(finish_reason=length)을 형식 재요청 없이 OutputTruncated 로
        돌려준다 (추론 설정을 바꿀 수 있고 low 가 아니면 low 로 1회 먼저). 입력을 나눠 다시 부를 수 있는 호출자만 켠다.
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
        # 재시도에서 reasoning_effort 를 바꿀 수 있나 — 못 바꾸면(Gemini·gpt-4.1 계열) 빈 응답은 같은 설정으로만 다시 (#236)
        tunable = can_set_reasoning(provider, model)

        messages = list(prompt.messages)
        retries: list[str] = []
        repairs: list[str] = []  # 고쳐 읽은 JSON 형식 (재시도가 아님 — 로그만)
        total = Usage()
        started = time.monotonic()
        called_at = datetime.now(UTC)  # 단가 적용일 기준 (llm/pricing.PRICE_CHANGES)
        schema_retry_used = False
        empty_retry_used = False
        # 빈 응답 (이미 low) → 같은 설정으로 MAX_RESAMPLES 회까지 (추론 설정을 못 바꾸는 모델은 MAX_FIXED_RESAMPLES)
        resamples = 0

        def record() -> Provenance:
            latency_ms = int((time.monotonic() - started) * 1000)
            return Provenance(
                provenance_id=f"prov-{uuid.uuid4().hex[:12]}",
                prompt=prompt.template.ref,
                # 파일 + 렌더된 출력 스키마 — 파일만의 해시(template.sha256)는 출력 모델이 바뀌어도 같다 (#240)
                prompt_sha256=prompt.template.prompt_sha256,
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
                    prompt.model, total.input, total.output, at=called_at
                ),  # output 에 추론 토큰 포함 (OpenAI 호환 usage)
            )

        while True:
            completion = await self._complete_with_backoff(
                backend,
                model,
                messages,
                params,
                retries,
                ref=prompt.template.ref,
                started=started,
                on_delta=on_delta,
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
                        data, fixed = _parse_json(completion.content)
                        parsed = out_model.model_validate(data)
                        repairs.extend(fixed)
                        break
                    except (ValueError, ValidationError) as exc:
                        summary = _validation_summary(exc)
                # 추론 설정을 더 내릴 수 없으면(이미 low, 또는 못 바꾸는 모델) 빈 응답만 같은 설정으로 1회 —
                # 내용이 있는 잘림은 같은 설정이면 같은 곳에서 다시 잘린다
                settled = params.get("reasoning_effort") == "low" or not tunable
                if not completion.content.strip() and settled and resamples < MAX_SPLIT_RESAMPLES:
                    resamples += 1
                    retries.append("empty_truncated->resample")
                    logger.warning(
                        "llm retry prompt=%s model=%s reason=empty_truncated elapsed_ms=%d",
                        prompt.template.ref,
                        prompt.model,
                        int((time.monotonic() - started) * 1000),
                    )
                    continue
                if not empty_retry_used and tunable and params.get("reasoning_effort") != "low":
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
                cut.finish_reason = completion.finish_reason
                logger.warning(
                    "llm truncated prompt=%s model=%s reason=output_truncated elapsed_ms=%d",
                    prompt.template.ref,
                    prompt.model,
                    cut.provenance.latency_ms,
                )
                raise cut

            if not completion.content.strip():
                # 빈 응답은 다시 부르면 대개 산다 — 잘림(추론이 상한을 다 씀, 추론 길이는 호출마다 다름)이든 아니든. 내용 필터만은
                # 같은 입력에 같은 결정이라 다시 부르지 않는다. 2026-11-15: 잘리지 않은 빈 응답은 재시도 없이 바로 실패해 알파 둘
                # (retry-lig024 gen_public@8 두 번 · g187c woori001 plan_contract@30)이 생성기와 무관하게 떨어졌다 — 잘린 빈 응답
                # 재시도(2026-11-05·09 측정)만 있었고 잘리지 않은 것을 뺀 근거는 없었다.
                # 추론 설정을 못 바꾸는 모델(Gemini·gpt-4.1 계열)은 low 를 넣지 않고 같은 설정으로 MAX_FIXED_RESAMPLES 회만 (#236)
                kind = (
                    "empty_truncated" if completion.finish_reason == "length" else "empty_resample"
                )
                filtered = completion.finish_reason == "content_filter"
                if (
                    not filtered
                    and (params.get("reasoning_effort") == "low" or not tunable)
                    and resamples < (MAX_RESAMPLES if tunable else MAX_FIXED_RESAMPLES)
                ):
                    resamples += 1
                    # 마지막 한 번은 추론을 끈다 — 폭주가 이어져도 빈 응답으로 끝나지 않게 (품질은 정적 검사·수리 루프가 본다)
                    off = (
                        NO_REASONING.get(provider)
                        if reasoning_off_last and tunable and resamples == MAX_RESAMPLES
                        else None
                    )
                    retries.append(
                        f"{kind}->reasoning_effort={off}" if off else f"{kind}->resample"
                    )
                    logger.warning(
                        "llm retry prompt=%s model=%s reason=%s finish=%s%s elapsed_ms=%d",
                        prompt.template.ref,
                        prompt.model,
                        kind,
                        completion.finish_reason,
                        f" next=reasoning_effort={off}" if off else "",
                        int((time.monotonic() - started) * 1000),
                    )
                    if off:
                        params = {**params, "reasoning_effort": off}
                    continue
                if (
                    filtered  # 첫 재시도(low 로 바꾸기)도 내용 필터는 다시 부르지 않는다 (#236)
                    or not tunable
                    or empty_retry_used
                    or params.get("reasoning_effort") in ("low", *NO_REASONING.values())
                ):
                    empty = EmptyResponse(
                        f"{prompt.template.ref}: 빈 응답 (finish_reason={completion.finish_reason}, "
                        f"재시도 {','.join(retries) or '없음'})"
                    )
                    empty.provenance = record()
                    empty.finish_reason = completion.finish_reason
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
                data, fixed = _parse_json(completion.content)
                parsed = out_model.model_validate(data)
                repairs.extend(fixed)
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
                    "llm retry prompt=%s model=%s reason=schema_violation where=%s elapsed_ms=%d",
                    prompt.template.ref,
                    prompt.model,
                    _violation_kinds(exc),
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
            "llm call prompt=%s model=%s retries=%s reasons=%s repairs=%s tokens_in=%d tokens_out=%d "
            "latency_ms=%d",
            prov.prompt,
            prov.model,
            prov.retries,
            ",".join(retries) or "-",
            ",".join(repairs) or "-",
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
        on_delta: TextCallback | None = None,
    ) -> Completion:
        attempt = 0
        timeouts = 0
        t0 = started if started is not None else time.monotonic()
        while True:
            try:
                if on_delta is not None:
                    return await backend.complete(model, messages, params, on_delta=on_delta)
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
