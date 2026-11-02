"""가상 역량 카드·매칭 규칙 (온톨로지 실데이터 아님)."""

from __future__ import annotations

from typing import Any

from task_spec.postings.matcher import COMP, SEED, TOOLS, Matcher
from task_spec.retrieve.cards import CardSet, CompetencyCard, ExampleLine

COMPS: list[dict[str, Any]] = [
    {
        "id": "comp:data.pipeline",
        "label": "데이터 파이프라인 구축",
        "area": "data",
        "obj": "airflow|파이프라인|etl",
    },
    {
        "id": "comp:data.stat_ml",
        "label": "통계·ML 모형 적용 분석",
        "area": "data",
        "subs": ["data.eng"],
        "obj": "수요 ?예측|모형",
    },
    {"id": "comp:sw.cache", "label": "캐시 설계", "area": "sw", "obj": "redis|캐시"},
]


def rules() -> dict[str, Any]:
    return {
        COMP + "competencies.json": {
            "meta": {},
            "competencies": COMPS,
            "non_competency": {"exclusive": {}, "soft": {}, "action": "하다"},
            "levels": {"수행": "구축"},
        },
        TOOLS + "tool_family.json": {"tools": {}},
        TOOLS + "families.json": {"families": {}},
        SEED + "tech_seed.json": {"nodes": []},
    }


def cardset() -> tuple[CardSet, Matcher]:
    m = Matcher.from_rules(rules())
    cards = {
        c["id"]: CompetencyCard(
            id=c["id"], label=c["label"], area=c["area"], subs=c.get("subs"), terms=[]
        )
        for c in COMPS
    }
    cards["comp:sw.cache"].esco = ["cache management"]
    examples = [
        ExampleLine(
            "p1#1", "p1", "data.eng", "배치 데이터 처리 흐름 구축 및 운영", ("comp:data.pipeline",)
        ),
        ExampleLine("p2#1", "p2", "sw.backend", "Redis 기반 세션 저장소 운영", ("comp:sw.cache",)),
        ExampleLine(
            "p3#1", "p3", "data.eng", "판매 데이터 기반 분석 모형 개발", ("comp:data.stat_ml",)
        ),
    ]
    return CardSet(
        version="vtest", cards=cards, examples=examples, sub_labels={"data.ml": "머신러닝"}
    ), m
