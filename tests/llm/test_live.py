"""실제 외부 API 스모크 — 비용 발생. `uv run pytest -m llm -q` 로만 돈다.

가상 이력서 텍스트(가명화 토큰 포함)만 보낸다. 지원자 실데이터 금지.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from task_spec.config import Settings
from task_spec.llm import verify_quote
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.pseudonymize import mint_pseudonymized
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import ClaimExtractionInput, ClaimExtractionOutput

pytestmark = pytest.mark.llm

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
FAKE_RESUME = """가상 데이터 — 실존 인물·회사 아님
[PERSON_1]
[ORG_1] 데이터팀 (2024.03 ~ 2025.08)
⟦문제 정의⟧ 주문 로그 집계가 하루 6시간 걸려 다음 날 리포트가 늦었다.
⟦과정⟧ Airflow DAG 를 재구성하고 Spark 집계 잡을 파티션 단위로 나눴다.
⟦기여한 점⟧ 집계 시간을 6시간에서 40분으로 줄였다.
"""


@pytest.mark.parametrize("model", ["opencode:qwen3.8-flash", "openai:gpt-4.1-mini"])
async def test_live_extract_claims(model: str) -> None:
    settings = Settings()  # type: ignore[call-arg]
    client = build_llm_client(settings)
    payload = ClaimExtractionInput(doc_kind="resume", text=mint_pseudonymized(FAKE_RESUME))
    prompt = assemble(PROMPTS.get("extract_claims"), payload, ontology_version="dev")
    if not model.startswith("opencode:"):
        prompt = prompt.model_copy(update={"params": {"temperature": 0, "max_tokens": 2048}})
        prompt = prompt.model_copy(
            update={
                "template": prompt.template.model_copy(
                    update={"meta": prompt.template.meta.model_copy(update={"model": model})}
                )
            }
        )
    result = await client.call(prompt, ClaimExtractionOutput)
    assert result.parsed.claims, "주장이 하나도 없음"
    ok = [c for c in result.parsed.claims if verify_quote(c.quote, FAKE_RESUME)]
    assert ok, "인용이 맞는 주장이 하나도 없음"
    p = result.provenance
    print(
        f"\n{model}: claims={len(result.parsed.claims)} quote_ok={len(ok)} retries={p.retries} "
        f"tokens={p.tokens.in_}/{p.tokens.out}/{p.tokens.reasoning} latency_ms={p.latency_ms}"
    )


async def test_live_draft_task() -> None:
    """공통 과제 서술 (CURRENT 모델) — 가상 온톨로지 값만. 자동 검증 통과까지."""
    from task_spec.spec.draft import FLAG_DRAFT_FAILED, check, draft_task
    from tests.spec.test_draft import _ctx

    ctx = _ctx()
    res = await draft_task(ctx, llm=build_llm_client(Settings()), prompts=PROMPTS)  # type: ignore[call-arg]
    assert res.output is not None and FLAG_DRAFT_FAILED not in res.flags
    if (
        not res.flags
    ):  # 재요청 후에도 어긋나면 flags 로 남으므로, flags 가 없을 때만 검증 통과를 요구
        assert check(res.output, ctx) == []
    p = res.provenance[-1]
    print(
        f"\ndraft_task: calls={len(res.provenance)} flags={res.flags} out={p.tokens.out} latency_ms={p.latency_ms}"
    )
