"""T1 검색 (가상 카드·규칙, 로컬 Kiwi)."""

from __future__ import annotations

import pytest

from task_spec.retrieve.t1 import SCOPE_SOFT, T1Retriever
from task_spec.retrieve.text import pattern_terms
from tests.retrieve.fixtures import cardset


def _ret() -> T1Retriever:
    cards, m = cardset()
    return T1Retriever(cards, m)


def test_pattern_terms_strip_regex() -> None:
    assert pattern_terms(r"ai ?(모델|기술)|java(?!script)") == ["ai", "모델", "기술", "java"]
    assert pattern_terms(None) == []


def test_pattern_hit_ranks_first_with_channel_ranks() -> None:
    hits = _ret().search("Airflow 로 주문 집계 배치를 나눴다", "data.eng", k=3)
    assert hits[0].competency_id == "comp:data.pipeline"
    assert hits[0].ranks["pattern"] == 1


def test_scope_hard_drops_soft_keeps_out_of_scope() -> None:
    r = _ret()
    text = "판매 데이터로 수요 예측 모형을 만들었다"
    assert "comp:data.stat_ml" not in [
        h.competency_id for h in r.search(text, "data.ml", scope="hard")
    ]
    soft = {h.competency_id: h for h in r.search(text, "data.ml", scope="soft")}
    assert not soft["comp:data.stat_ml"].in_scope
    inside = {h.competency_id: h for h in r.search(text, "data.eng", scope="soft")}
    assert soft["comp:data.stat_ml"].score == pytest.approx(
        inside["comp:data.stat_ml"].score * SCOPE_SOFT
    )


def test_posting_bonus_only_for_existing_hits() -> None:
    r = _ret()
    text = "Redis 캐시를 붙였다"
    base = {h.competency_id: h.score for h in r.search(text, "sw.backend")}
    hits = r.search(text, "sw.backend", posting={"comp:sw.cache": True, "comp:data.pipeline": True})
    by = {h.competency_id: h for h in hits}
    assert by["comp:sw.cache"].posting and by["comp:sw.cache"].score > base["comp:sw.cache"]
    # 공고 역량이라도 주장과 안 맞으면 후보에 끼워 넣지 않는다 (불변식 5: 가산점만)
    assert "comp:data.pipeline" not in base and "comp:data.pipeline" not in by


def test_exclude_positions_hides_example_channel() -> None:
    r = _ret()
    ranks = r.channel_ranks("배치 데이터 처리 흐름", exclude_positions=frozenset({"p1"}))
    assert "comp:data.pipeline" not in ranks["example"]
