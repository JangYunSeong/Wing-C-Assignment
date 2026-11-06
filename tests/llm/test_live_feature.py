"""draft_feature_task 실제 생성 스모크 — 비용 발생 (OpenCode qwen, 몇 센트). `uv run pytest -m llm tests/llm/test_live_feature.py`.

입력은 가상 공고 골격(가상 역량·수행준거)뿐 — 지원자 데이터 없음 (불변식 3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from task_spec.config import Settings
from task_spec.llm.factory import build_llm_client
from task_spec.prompting import PromptRegistry
from task_spec.schemas import DraftFeatureOutput
from task_spec.spec.feature import check_feature, draft_feature_task
from tests.spec.test_draft import _ctx

pytestmark = pytest.mark.llm

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


async def test_live_draft_feature_task() -> None:
    client = build_llm_client(Settings())  # type: ignore[call-arg]
    ctx = _ctx(languages=["Java"])
    res = await draft_feature_task(ctx, llm=client, prompts=PROMPTS)
    assert res.output is not None, res.fail_reasons
    out = res.output
    assert isinstance(out, DraftFeatureOutput)
    left = check_feature(out, ctx)
    print("first_problems:", res.first_problems)
    print("left:", left, "flags:", res.flags)
    print("cost:", [p.cost_usd for p in res.provenance])
    print("title:", out.title)
    print("traps:", [t.description for t in out.traps])
    print("debug:", out.debug_stage.scenario[:300])
    ids1 = {a.artifact_id for a in out.artifacts}
    ids2 = {a.artifact_id for a in out.debug_stage.artifacts}
    assert not ids1 & ids2
