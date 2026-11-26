"""빌드의 정답 장치 표지 거름 (#337 1절) — 이미 저장된 재료의 문장 표지는 빌드가 빼고 승인 화면에 알린다. 가상 데이터만, LLM 대역."""

from __future__ import annotations

from typing import Any

import pytest

from task_spec.nodes.starter import build as build_mod
from task_spec.nodes.starter.build import build_and_repair
from tests.starter.test_contract import BACKEND_SEED
from tests.starter.test_generate import D1, STARTER, _files, _inputs
from tests.starter.test_repair import D1_OK, NAIVE_OK, PROMPTS, ScriptedVerifier, _llm, _one_case
from tests.starter.test_static_check import PKG


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


_LOOKUP = (
    f"{PKG}/order/Lookup.java",
    "package com.example.service.order;\n\npublic final class Lookup {\n    private Lookup() {}\n\n"
    "    public static String byRequestId(String id) {\n        return id;\n    }\n}\n",
)


async def _starter_build(*answers: Any) -> tuple[Any, Any]:
    seed = BACKEND_SEED.model_copy(update={"fix_markers": [r"\bbyRequestId\b"]})
    llm, backend = _llm(gen_starter=list(answers))
    out = await build_and_repair(
        _inputs(seed=seed),
        (D1,),
        llm=llm,
        prompts=PROMPTS,
        ontology_version="onto-test",
        verifier=ScriptedVerifier(_one_case(NAIVE_OK, D1_OK)),
    )
    return out, backend


async def test_a_starter_with_the_accepted_mechanism_is_rebuilt_once_without_it() -> None:
    """#337 2절: 시작 코드가 허용 설계의 장치를 쓰면 흔한 구현이 따라 써 함정이 무력화된다 (운영 ct-9433 b3). 참조 구현을 만들기 전에
    '이 장치 없이' 한 번 다시 만든다 — 시작 코드를 검증 뒤에 다시 만들면 참조 구현도 모두 다시 만들어야 한다."""
    # 수리는 이전 출력에 고친 파일만 덮어쓴다 — 장치가 든 파일을 장치 없이 다시 쓴 답
    plain = (_LOOKUP[0], _LOOKUP[1].replace("byRequestId", "byId"))
    out, backend = await _starter_build(_files(*STARTER, _LOOKUP), _files(plain))
    assert backend.calls.count("gen_starter") == 2
    assert [(r.gate, r.target, r.result) for r in out.repairs][:1] == [
        ("static", "gen_starter", "regenerated")
    ]
    assert out.status == "review"
    assert not [f for f in out.review_flags if "시작 코드가 허용 설계의 장치" in f]


async def test_a_starter_that_keeps_the_mechanism_is_flagged_not_blocked() -> None:
    """다시 만든 뒤에도 장치가 남으면 막지 않고 표시만 — 표지는 테스트 밖 모든 파일을 봐 헛걸림이 있을 수 있다. ③ 이 시작 코드의 표지를
    같이 보고 흔한 구현이 숨은 케이스를 통과하면 재료 판정으로 멈춘다."""
    out, backend = await _starter_build(_files(*STARTER, _LOOKUP), _files(*STARTER, _LOOKUP))
    assert backend.calls.count("gen_starter") == 2 and out.status == "review"
    assert any(
        f.startswith("시작 코드가 허용 설계의 장치(byRequestId)를 씀 — 한 번 다시 만든 뒤에도")
        for f in out.review_flags
    )
