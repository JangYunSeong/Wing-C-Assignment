"""단가표 (llm/pricing.py) — #237: CURRENT 프롬프트 모델 단가 누락이 빌드 예산을 0 으로 세던 문제."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from task_spec.llm import Completion, FakeBackend, LLMClient, Usage, pricing
from task_spec.llm.pricing import PRICE_CHANGES, PRICES, cost_usd
from task_spec.prompting import PromptRegistry

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
M = 1_000_000
GEMINI = "gemini:gemini-3.8-flash"


def test_every_current_prompt_model_is_priced() -> None:
    # 단가가 없으면 cost_usd=None → builder 가 그 호출을 예산 합계에서 0 으로 센다 (2026-11-18 Gemini 관문·gpt-4.1-mini)
    unpriced = {
        f"{task}@{PROMPTS.current_version(task)}": PROMPTS.get(task).meta.model
        for task in sorted(p.name for p in PROMPTS.root.iterdir() if p.is_dir())
        if PROMPTS.has_current(task)
        and cost_usd(PROMPTS.get(task).meta.model, 1, 1, at=datetime(2026, 11, 18, tzinfo=UTC))
        is None
    }
    assert not unpriced, (
        f"단가표에 없는 모델을 쓰는 CURRENT 프롬프트 {unpriced} — 공식 가격 페이지에서 확인한 값을 출처·확인일과 함께 "
        "llm/pricing.py PRICES 에 넣는다 (추측해서 넣지 않는다)"
    )


def test_gpt41_mini_standard_price() -> None:
    # 2026-11-18 OpenAI 공식 Standard: 입력 $0.40 · 출력 $1.60 / 1M (캐시 입력은 따로 셈하지 않음)
    assert cost_usd("openai:gpt-4.1-mini", M, 0) == pytest.approx(0.40)
    assert cost_usd("openai:gpt-4.1-mini", 0, M) == pytest.approx(1.60)


@pytest.mark.parametrize(
    ("at", "price"),
    [
        (datetime(2026, 11, 18, 3, 0, tzinfo=UTC), (0.75, 3.75)),
        (datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC), (0.75, 3.75)),
        (datetime(2027, 1, 1, 0, 0, tzinfo=UTC), (1.50, 7.50)),
        (datetime(2027, 6, 1, tzinfo=UTC), (1.50, 7.50)),
        # 호출 시각은 UTC 로 본다 — 서울 2027-01-01 08:59 는 UTC 로 아직 2026-12-31
        (datetime(2027, 1, 1, 8, 59, tzinfo=timezone(timedelta(hours=9))), (0.75, 3.75)),
        (datetime(2027, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=9))), (1.50, 7.50)),
        (datetime(2027, 1, 1, 0, 0), (1.50, 7.50)),  # 시간대 없는 값은 UTC 로
    ],
)
def test_gemini_price_by_call_time(at: datetime, price: tuple[float, float]) -> None:
    # 2026-11-18 Google 공식 Standard: 2026-12-31 까지 $0.75 · $3.75, 2027-01-01 부터 $1.50 · $7.50 (출력은 추론 포함)
    assert cost_usd(GEMINI, M, 0, at=at) == pytest.approx(price[0])
    assert cost_usd(GEMINI, 0, M, at=at) == pytest.approx(price[1])


def test_price_changes_extend_known_models_in_date_order() -> None:
    for model, changes in PRICE_CHANGES.items():
        assert model in PRICES, model  # 시작 전 값이 표에 있어야 한다
        starts = [d for d, _ in changes]
        assert starts == sorted(starts), model


def test_unknown_model_warns_once_per_process(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pricing, "_warned_unknown", set())
    with caplog.at_level(logging.WARNING, logger="task_spec.llm.pricing"):
        assert cost_usd("openai:가상-모델", 100, 100) is None
        assert cost_usd("openai:가상-모델", 100, 100) is None
        assert cost_usd("opencode:가상-모델-2", 100, 100) is None
    lines = [r.getMessage() for r in caplog.records if r.name == "task_spec.llm.pricing"]
    assert len(lines) == 2  # 모델마다 한 번
    assert "model=openai:가상-모델 " in lines[0] and "model=opencode:가상-모델-2 " in lines[1]


async def _no_sleep(_: float) -> None:
    return None


async def test_client_prices_gemini_at_call_start(monkeypatch: pytest.MonkeyPatch) -> None:
    # 호출 시작 시각(UTC)의 단가 — 날짜가 바뀌어도 손으로 표를 고치지 않는다
    from task_spec.llm import client as client_mod
    from task_spec.prompting import assemble
    from task_spec.schemas import FacilitatorChooseInput

    class _At(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return datetime(2027, 1, 1, 0, 0, 5, tzinfo=UTC)

    monkeypatch.setattr(client_mod, "datetime", _At)
    tpl = PROMPTS.get(
        "facilitator_choose"
    )  # CURRENT — gemini:gemini-3.8-flash (지원자 데이터 없음)
    assert tpl.meta.model == GEMINI
    payload = FacilitatorChooseInput(
        rule="가상 규칙", request="가상 요청", items="[q1]", question="가상 질문"
    )
    prompt = assemble(tpl, payload, ontology_version="eval")
    fake = FakeBackend().push(Completion('{"asked": []}', Usage(1000, 2000, 500)))
    r = await LLMClient({"gemini": fake}, sleep=_no_sleep).call(prompt)
    assert r.provenance.cost_usd == pytest.approx((1000 * 1.50 + 2000 * 7.50) / M)
