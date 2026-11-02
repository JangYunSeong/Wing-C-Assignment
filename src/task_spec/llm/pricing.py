"""비용 계산 단가표 (USD / 1M tokens). 모르는 모델은 None — 추측하지 않는다."""

from __future__ import annotations

# "<provider>:<model>": (input, output). 출력 단가는 추론 토큰에도 적용한다.
PRICES: dict[str, tuple[float, float]] = {}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)
