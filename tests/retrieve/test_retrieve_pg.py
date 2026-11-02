"""실제 적재 온톨로지로 검색기 만들기 (`uv run pytest -m pg`)."""

from __future__ import annotations

import pytest

from task_spec.retrieve import build_retriever
from tests.postings.test_postings_pg import ctx  # noqa: F401 — 픽스처 재사용

pytestmark = pytest.mark.pg


async def test_cards_and_search_on_loaded_ontology(ctx) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, repo, lists = ctx
    ret = await build_retriever(repo, lists)
    assert ret is not None
    cards = ret.cards
    assert len(cards.cards) == len(lists.matcher.comps)  # type: ignore[union-attr]
    assert len(cards.examples) > 2000
    assert cards.cards["comp:data.stat_ml"].subs == [
        "data.analytics",
        "data.eng",
    ]  # = competency_scope
    assert any(c.esco for c in cards.cards.values()) and any(c.ncs for c in cards.cards.values())
    hits = ret.search("Kafka 토픽을 재설계하고 소비자 지연 알림을 붙였다", "data.eng", scope="soft")
    assert "comp:data.streaming" in [h.competency_id for h in hits[:3]]


async def test_relatedness_on_loaded_ontology(ctx) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from task_spec.ontology.relatedness import load_relatedness

    _, repo, _ = ctx
    rel = await load_relatedness(repo)
    assert (
        rel.level("comp:sw.container_dev", "infra.cloud", ["comp:infra.container"])[0] == "strong"
    )
    assert rel.level("comp:ai.service", "data.ml", [])[0] == "strong"  # data.ml 공고 다수가 요구
    assert rel.level("comp:infra.security", "data.ml", [])[0] == "weak"
