"""비용 계산 단가표 (USD / 1M tokens). 모르는 모델은 None — 추측하지 않는다.

출력 토큰 수는 OpenAI 호환 usage 의 completion/output 토큰(추론 토큰 포함)을 쓴다.
출처·확인일을 함께 적는다. 공식 가격 페이지로 재확인 전 값은 [미정] 표시.
값이 정해진 날 바뀌면 PRICE_CHANGES 에 적용 시작일과 새 값을 둔다 — 호출 시각(UTC)으로 고른다 (손으로 고치지 않게).
단가를 모르는 모델은 프로세스당 한 번 WARNING(모델 이름만) — 비용·예산 합계에서 빠진다 (2026-11-18 #237).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

logger = logging.getLogger(__name__)

# "<provider>:<model>": (input, output) — PRICE_CHANGES 에 시작일이 있으면 그 전까지의 값
PRICES: dict[str, tuple[float, float]] = {
    # 2026-11-02 집계 사이트(benchlm.ai, pricepertoken.com) 일치값. OpenAI 공식 페이지 재확인 [미정]
    "openai:gpt-5.5": (5.00, 30.00),
    "openai:gpt-5.4-mini": (0.75, 4.50),
    # 2026-11-03 OpenCode Zen 가격표의 같은 이름 모델 값. OpenAI 공식 페이지 재확인 [미정]
    "openai:gpt-5.4-nano": (0.20, 1.25),
    # 2026-11-02 opencode.ai/data(사용 통계 페이지)·Maxim 일치값. Zen 가격 페이지 재확인 [미정]
    "opencode:qwen3.8-flash": (0.15, 0.47),
    # 2026-11-09 OpenCode Zen 가격표 (사용자가 붙여 준 표). 단계별 모델 비교 평가용 [미정 — 공식 페이지 재확인 전]
    "opencode:gpt-6.1-sol": (2.00, 10.00),
    "opencode:gpt-6-luna": (0.10, 0.50),
    "opencode:deepseek-v4-pro": (1.74, 3.48),
    "opencode:glm-5.3": (1.40, 4.40),
    "opencode:minimax-m3": (0.30, 1.20),
    # 2025-04 출시 때 OpenAI 공표가 (모델 지식 기준). 공식 페이지 재확인 [미정]
    "openai:gpt-4.1": (2.00, 8.00),
    # 2026-11-18 OpenAI 공식 가격 페이지 https://developers.openai.com/api/docs/pricing , Standard.
    # 캐시 입력($0.10)은 따로 셈하지 않는다 — 입력 단가로 셈 (#237)
    "openai:gpt-4.1-mini": (0.40, 1.60),
    # 2026-11-18 Google 공식 가격 페이지 https://aistudio.google.com/docs/pricing
    # (ai.google.dev/gemini-api/docs/pricing 에서 넘어감), Standard. 출력은 추론 토큰 포함, 프롬프트 길이 차등 없음.
    # 2026-12-31 까지 값 — 2027-01-01 부터는 PRICE_CHANGES (#237)
    "gemini:gemini-3.8-flash": (0.75, 3.75),
}

# 적용 시작일이 정해진 단가: "<provider>:<model>": [(시작일, (input, output)), …] 시작일 오름차순.
# 호출 시각(UTC)의 날짜가 시작일(그날 0시 UTC) 이상인 마지막 값이 PRICES 값을 대신한다. 모델은 PRICES 에도 있어야 한다.
PRICE_CHANGES: dict[str, list[tuple[date, tuple[float, float]]]] = {
    # 2026-11-18 같은 Google 공식 페이지 (Standard): 2027-01-01 부터 입력 $1.50 · 출력 $7.50
    "gemini:gemini-3.8-flash": [(date(2027, 1, 1), (1.50, 7.50))],
}

_warned_unknown: set[str] = set()  # 경고를 이미 남긴 모델 (프로세스당 한 번)


def price_at(model: str, at: datetime | None = None) -> tuple[float, float] | None:
    """호출 시각 at(없으면 지금)의 단가. 시간대 없는 at 은 UTC 로 본다."""
    price = PRICES.get(model)
    if price is None:
        return None
    when = datetime.now(UTC) if at is None else at if at.tzinfo else at.replace(tzinfo=UTC)
    day = when.astimezone(UTC).date()
    for start, changed in PRICE_CHANGES.get(model, []):
        if day >= start:
            price = changed
    return price


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, *, at: datetime | None = None
) -> float | None:
    price = price_at(model, at)
    if price is None:
        if model not in _warned_unknown:
            _warned_unknown.add(model)
            logger.warning(
                "llm price unknown model=%s cost_usd=None — 단가표(llm/pricing.py)에 없어 비용·예산 합계에서 빠진다",
                model,
            )
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)
