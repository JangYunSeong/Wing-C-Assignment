"""명세 부분 수리 규칙 (#229) — 자료 제목의 결론 암시·원 사례 고유어를 걸린 칸만 고친다 (spec/patch.py)."""

from __future__ import annotations

import json

from task_spec.llm import Completion, FakeBackend
from task_spec.spec.draft import FLAG_DRAFT_INVALID
from task_spec.spec.feature import draft_feature_task
from task_spec.spec.patch import apply, targets
from tests.spec.test_feature import _client, _out
from tests.spec.test_feature_seed import _V2, _seed_ctx, _v2draft


def test_a_title_implying_the_conclusion_targets_that_title_only() -> None:
    """2026-11-23 알파 명세 5회 중 4회 첫 답이 '…차이 목록' 제목으로 걸렸다 — 그 자료 제목 칸만, 1·2단계 모두 id 로."""
    out = _out()
    tg = targets(
        out,
        [
            "자료 a4 제목이 결론을 암시함 (차이, 누락) — 자료 종류만 쓸 것",
            "자료 a8 제목이 결론을 암시함 (비교) — 자료 종류만 쓸 것",
        ],
    )
    assert [(t.path, t.terms) for t in tg] == [
        ("artifacts[3].title", ("차이", "누락")),
        ("debug_stage.artifacts[1].title", ("비교",)),
    ]
    assert "자료 종류" in tg[0].why
    assert targets(out, ["자료 a99 제목이 결론을 암시함 (차이) — 자료 종류만 쓸 것"]) == []
    fixed = apply(out, {"artifacts[3].title": "배송 API 응답 지표 (일별)"})
    assert fixed.artifacts[3].title == "배송 API 응답 지표 (일별)"
    assert fixed.artifacts[3].content == out.artifacts[3].content


def test_a_material_proper_noun_is_fixed_wherever_the_check_saw_it() -> None:
    """원 사례 고유어 검사는 글·불변식·함정·자료 본문·2단계·허용 설계·숨은 수용 기준을 본다 — 그 낱말이 든 칸만 고친다 ('Airflow')."""
    d = _out().model_dump()
    d["scenario"] += " 지금은 Airflow 로 돌린다."
    d["artifacts"][1]["content"] += "\nAirflow DAG: daily_summary"
    d["debug_stage"]["artifacts"][0]["content"] += "\n[Airflow] task failed"
    d["traps"][0]["why_ai_fails"] += " (Airflow 재시도)"
    d["accepted_designs"][0]["conditions"] += " — Airflow 없이"
    d["acceptance_hidden"][0]["text"] += " (Airflow)"
    d["invariant"] += " Airflow"
    out = type(_out()).model_validate(d)
    tg = targets(out, ["원 사례의 고유어가 나옴 (소재를 바꿔야 함): Airflow"])
    assert {t.path for t in tg} == {
        "scenario",
        "artifacts[1].content",
        "debug_stage.artifacts[0].content",
        "traps[0].why_ai_fails",
        "accepted_designs[0].conditions",
        "acceptance_hidden[0].text",
        "invariant",
    }
    assert all(t.terms == ("Airflow",) for t in tg)
    fixed = apply(
        out,
        {
            "invariant": "가상 불변식",
            "traps[0].why_ai_fails": "가상 이유",
            "accepted_designs[0].conditions": "가상 조건",
            "acceptance_hidden[0].text": "가상 숨은 기준",
            "debug_stage.artifacts[0].content": "가상 로그",
        },
    )
    assert fixed.invariant == "가상 불변식" and fixed.traps[0].why_ai_fails == "가상 이유"
    assert fixed.accepted_designs[0].conditions == "가상 조건"
    assert fixed.acceptance_hidden[0].text == "가상 숨은 기준"
    assert fixed.debug_stage is not None and fixed.debug_stage.artifacts[0].content == "가상 로그"
    assert fixed.title == out.title  # 다른 칸은 그대로


async def test_a_title_left_after_the_retry_is_fixed_by_the_partial_repair() -> None:
    """재요청이 같은 제목을 다시 내도 멈추지 않는다 — 그 제목 칸만 고쳐 다시 검사한다 (2026-11-23 알파 woori001 r2·kb002 r2 의 멈춤)."""
    leaky = _v2draft()
    i = next(n for n, a in enumerate(leaky["artifacts"]) if a["role"] == "distractor")
    aid = leaky["artifacts"][i]["artifact_id"]
    title = leaky["artifacts"][i]["title"]
    leaky["artifacts"][i]["title"] = title + " 차이 목록"
    patch = {"fields": [{"path": f"artifacts[{i}].title", "text": title}]}
    fake = FakeBackend().push(
        Completion(json.dumps(leaky, ensure_ascii=False)),
        Completion(json.dumps(leaky, ensure_ascii=False)),  # 재요청도 같은 제목
        Completion(json.dumps(patch, ensure_ascii=False)),  # 부분 수리: 그 제목만
    )
    res = await draft_feature_task(_seed_ctx(), llm=_client(fake), prompts=_V2())  # type: ignore[arg-type]
    assert res.output is not None and res.output.artifacts[i].title == title
    assert any(p.startswith(f"자료 {aid} 제목이 결론을 암시함") for p in res.patched)
    assert not [f for f in res.flags if f.startswith(FLAG_DRAFT_INVALID)]
    assert [p.prompt for p in res.provenance][-1] == "fix_feature_fields@1"
