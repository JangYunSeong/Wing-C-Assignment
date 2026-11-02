"""T1 선택 (FakeBackend) — 후보 밖 id 버림·답 누락·uncovered·가명화 경계."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.nodes.pseudonymize import (
    derive_pseudonymized,
    join_pseudonymized,
    mint_pseudonymized,
)
from task_spec.prompting import PromptRegistry
from task_spec.retrieve.select import SelectInputClaim, select
from task_spec.retrieve.t1 import T1Retriever
from task_spec.schemas import SelectCompetencyInput
from tests.retrieve.fixtures import cardset

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


def test_derive_checks_quote_tokens_and_patterns() -> None:
    src = mint_pseudonymized("[ORG_1] 에서  Redis 캐시를 붙였다.")
    t = derive_pseudonymized(src, quote="[ORG_1] 에서 Redis 캐시를", summary="캐시 추가")
    assert t == "행동: 캐시 추가 / 문장: [ORG_1] 에서 Redis 캐시를"
    with pytest.raises(ValueError):  # 원문에 없는 인용
        derive_pseudonymized(src, quote="다른 회사에서 일했다", summary="x")
    with pytest.raises(ValueError):  # 위조 토큰
        derive_pseudonymized(src, quote="Redis 캐시를", summary="[PERSON_3] 와 함께")
    with pytest.raises(ValueError):  # 요약에 연락처 패턴
        derive_pseudonymized(src, quote="Redis 캐시를", summary="fake.user@example.com 로 공유")
    with pytest.raises(ValueError):
        join_pseudonymized(["평문"], ["c1"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        join_pseudonymized([src], ["c 1"])


def test_input_schema_rejects_plain_claim_text() -> None:
    with pytest.raises(ValueError):
        SelectCompetencyInput(sub="data.eng", claims="평문 주장", candidates="- x")  # type: ignore[arg-type]


async def test_select_filters_out_of_list_and_missing_answers() -> None:
    cards, m = cardset()
    r = T1Retriever(cards, m)
    claims = [
        SelectInputClaim(
            claim_id=f"d1-c{i}",
            text=mint_pseudonymized(t),
            hits=r.search(t, "data.eng", scope="soft", k=3),
        )
        for i, t in enumerate(
            [
                "Airflow 로 배치를 나눴다",
                "Redis 캐시를 붙였다",
                "수요 예측 모형을 만들었다",
                "회고",
            ],
            1,
        )
    ]
    body = {
        "items": [
            {
                "ref": "c1",
                "competency_ids": ["comp:data.pipeline", "comp:없는역량"],
                "uncovered": False,
            },
            {"ref": "c3", "competency_ids": [], "uncovered": True, "reason": "맞는 역량 없음"},
        ]
    }
    fake = FakeBackend().push(Completion(json.dumps(body, ensure_ascii=False)))
    res = await select(
        claims,
        sub="data.eng: 데이터",
        cards=cards,
        llm=LLMClient({"opencode": fake}),
        prompts=PROMPTS,
    )
    by = {x.claim_id: x for x in res.items}
    assert by["d1-c1"].competency_ids == ["comp:data.pipeline"]
    assert by["d1-c2"].competency_ids == [] and not by["d1-c2"].uncovered
    assert by["d1-c3"].uncovered and by["d1-c3"].candidates
    assert by["d1-c4"].candidates == [] and not by["d1-c4"].uncovered  # 후보 없음 → LLM 에 안 보냄
    assert sorted((e.code, e.ref_id) for e in res.errors) == [
        ("candidate_out_of_list", "d1-c1"),
        ("missing_answer", "d1-c2"),
    ]
    assert all("Airflow" not in e.message for e in res.errors)  # 에러에 원문 없음
    assert len(res.provenance) == 1 and by["d1-c1"].provenance_id == res.provenance[0].provenance_id
    assert fake.calls[0][0] == PROMPTS.get("select_competency").meta.model.split(":", 1)[1]
