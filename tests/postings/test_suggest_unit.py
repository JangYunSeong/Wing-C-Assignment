"""직군·업종 추천 신호 결합 (매처 없이, 가짜 목록)."""

from __future__ import annotations

import json
from pathlib import Path

from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.postings.suggest import OntologyLists, suggest
from task_spec.prompting import PromptRegistry
from task_spec.schemas import PostingLineIn

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


def _lists() -> OntologyLists:
    return OntologyLists(
        version="vtest",
        subs={
            "data.eng": {"label": "데이터 엔지니어링", "in_pilot": True},
            "sw.backend": {"label": "백엔드", "in_pilot": True},
        },
        industries={"ind.platform": "인터넷 플랫폼", "ind.finance": "금융"},
        company_industry={"가상은행": "ind.finance"},
        positions=[
            {"id": "p1", "title": "데이터 엔지니어", "sub": "data.eng"},
            {"id": "p2", "title": "백엔드 개발자", "sub": "sw.backend"},
        ],
        competency_label={},
        tool_info={},
    )


LINES = [PostingLineIn(text="파이프라인 구축", section="duties")]


async def test_known_company_and_title_similarity() -> None:
    subs, ind, meta = await suggest(
        _lists(),
        company="가상은행 (주)",
        title="데이터 엔지니어",
        text="x",
        lines=LINES,
        llm=None,
        prompts=None,
    )
    assert subs[0].id == "data.eng" and ind[0].id == "ind.finance" and not meta["llm_used"]


async def test_exclude_positions_removes_self_match() -> None:
    subs, _, _ = await suggest(
        _lists(),
        company="가상은행",
        title="데이터 엔지니어",
        text="x",
        lines=LINES,
        llm=None,
        prompts=None,
        exclude_positions=frozenset({"p1"}),
    )
    assert all("p1" not in r for c in subs for r in c.reasons)


async def test_weak_signal_or_unknown_company_uses_llm() -> None:
    out = {
        "sub_ids": ["sw.backend", "없는직군"],
        "industry_id": "ind.platform",
        "reason": "API 서버 개발",
    }
    fake = FakeBackend().push(Completion(json.dumps(out, ensure_ascii=False)))
    subs, ind, meta = await suggest(
        _lists(),
        company="처음보는회사",
        title="",
        text="API 서버 개발",
        lines=LINES,
        llm=LLMClient({"openai": fake}),
        prompts=PROMPTS,
    )
    assert meta["llm_used"] and subs[0].id == "sw.backend"
    assert all(c.id != "없는직군" for c in subs)  # 목록 밖 값은 버림
    assert ind[0].id == "ind.platform" and ind[0].reasons == ["LLM 추천"]


async def test_llm_failure_falls_back_to_rules() -> None:
    """LLM 출력이 두 번 다 깨져도 추천은 규칙 후보로 응답한다."""
    fake = FakeBackend().push(Completion("깨진 응답"), Completion("또 깨짐"))
    subs, ind, meta = await suggest(
        _lists(),
        company="처음보는회사",
        title="데이터 엔지니어",
        text="x",
        lines=LINES,
        llm=LLMClient({"openai": fake}),
        prompts=PROMPTS,
    )
    assert meta["llm_error"] == "SchemaViolation"
    assert subs and subs[0].id == "data.eng" and ind == []


def test_nulls_in_llm_lists_are_dropped() -> None:
    from task_spec.schemas import SuggestFieldOutput

    out = SuggestFieldOutput.model_validate({"sub_ids": ["data.eng", None], "industry_ids": [None]})
    assert out.sub_ids == ["data.eng"] and out.industry_ids == []
