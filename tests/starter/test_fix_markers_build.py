"""빌드의 정답 장치 표지 거름 (#337 1절) — 이미 저장된 재료의 문장 표지는 빌드가 빼고 승인 화면에 알린다. 가상 데이터만, LLM 대역."""

from __future__ import annotations

from typing import Any

import pytest

from task_spec.nodes.starter import build as build_mod
from task_spec.nodes.starter.build import build_and_repair
from tests.starter.test_contract import BACKEND_SEED
from tests.starter.test_generate import D1, _inputs
from tests.starter.test_repair import D1_OK, NAIVE_OK, PROMPTS, ScriptedVerifier, _llm, _one_case


async def test_sentence_markers_are_left_out_of_the_build_with_a_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회사 재료 2건(인터뷰 v1)의 표지는 문장이었다 — ③ 에는 코드 꼴 표지만 가고, 뺀 것은 승인 화면에 남는다."""
    seen: list[tuple[str, ...]] = []
    real = build_mod.assign

    def spy(*a: Any, **k: Any) -> Any:
        seen.append(tuple(k.get("fix_markers", ())))
        return real(*a, **k)

    monkeypatch.setattr(build_mod, "assign", spy)
    seed = BACKEND_SEED.model_copy(
        update={"fix_markers": ["요청 번호를 먼저 저장해 중복을 막음", r"\bfindByRequestId\b"]}
    )
    llm, _ = _llm()
    out = await build_and_repair(
        _inputs(seed=seed),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=ScriptedVerifier(_one_case(NAIVE_OK, D1_OK)),
    )
    assert out.status == "review"
    assert "정답 장치 표지 1개 제외 — 코드 꼴이 아님" in out.review_flags
    assert seen and set(seen) == {(r"\bfindByRequestId\b",)}
