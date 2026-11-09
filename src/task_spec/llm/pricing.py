"""비용 계산 단가표 (USD / 1M tokens). 모르는 모델은 None — 추측하지 않는다.

출력 토큰 수는 OpenAI 호환 usage 의 completion/output 토큰(추론 토큰 포함)을 쓴다.
출처·확인일을 함께 적는다. 공식 가격 페이지로 재확인 전 값은 [미정] 표시.
"""

from __future__ import annotations

# "<provider>:<model>": (input, output)
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
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)
