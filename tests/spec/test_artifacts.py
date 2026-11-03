"""자료 점진 공개(v3) — 순서 섞기·번호 재매김·결론 암시 검사. 가상 데이터만."""

from __future__ import annotations

from typing import Any

from task_spec.schemas import DraftTaskOutputV3
from task_spec.spec.draft import check, shuffle_artifacts
from tests.spec.test_draft import _ctx, _draft


def _v3(**over: Any) -> DraftTaskOutputV3:
    arts = [
        {
            "artifact_id": f"a{i}",
            "title": f"가상 구성 요소 {i} 지표",
            "content": f"가상 로그 {i}",
            "role": r,
        }
        for i, r in enumerate(
            [
                "key",
                "support",
                "distractor",
                "distractor",
                "distractor",
                "support",
                "support",
                "support",
                "key",
            ],
            1,
        )
    ]
    d = _draft(
        traps=[{"trap_id": "t1", "description": "a3 만 보면 틀림", "why_ai_fails": "a1 을 안 봄"}],
        observables=[
            {"kind": "requests_artifact", "value": "a1"},
            {"kind": "catches_trap", "value": "t1"},
        ],
        artifacts=arts,
        max_requests=3,
        scenario="가상의 회사에서 응답이 느려졌다. 아래 자료 중 최대 3개를 요청할 수 있다.",
    )
    d.update(over)
    return DraftTaskOutputV3.model_validate(d)


def test_shuffle_renumbers_and_rewrites_references_consistently() -> None:
    out = _v3()
    sh = shuffle_artifacts(out, "seed-1")
    assert [a.artifact_id for a in sh.artifacts] == [f"a{i}" for i in range(1, 10)]
    by_content = {a.content: a.artifact_id for a in sh.artifacts}
    new_a1 = by_content["가상 로그 1"]  # 원래 a1 (key)
    new_a3 = by_content["가상 로그 3"]
    assert sh.observables[0].value == new_a1
    assert sh.traps[0].description == f"{new_a3} 만 보면 틀림"
    assert sh.traps[0].why_ai_fails == f"{new_a1} 을 안 봄"
    assert shuffle_artifacts(out, "seed-1") == sh  # 같은 seed → 같은 순서


def test_tells_in_titles_and_key_content_are_flagged() -> None:
    out = _v3()
    arts = [a.model_copy() for a in out.artifacts]
    arts[0] = arts[0].model_copy(update={"title": "학습 피처 vs 서빙 피처 대조표"})
    arts[8] = arts[8].model_copy(update={"content": "feature_x: 학습 O / 서빙 X → 누락"})
    problems = "\n".join(check(out.model_copy(update={"artifacts": arts}), _ctx()))
    assert "제목이 결론을 암시" in problems and "결론을 직접 말함" in problems
    assert not any("암시" in p or "직접 말함" in p for p in check(out, _ctx()))


async def test_v3_current_draft_shuffles_and_carries_artifacts_into_task() -> None:
    """CURRENT(v3) 로 생성: 자료가 섞여 TaskItem 에 실리고, 요청 한도·source_incident 도 붙는다."""
    import json
    from pathlib import Path

    from task_spec.llm import Completion, FakeBackend
    from task_spec.prompting import PromptRegistry
    from task_spec.schemas import TaskItem
    from task_spec.spec.draft import apply, draft_task
    from tests.spec.test_draft import _client

    reg = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
    assert reg.get("draft_task").meta.output_schema == "DraftTaskOutputV3"
    fake = FakeBackend().push(
        Completion(json.dumps(_v3().model_dump(mode="json"), ensure_ascii=False))
    )
    ctx = _ctx()
    res = await draft_task(ctx, llm=_client(fake), prompts=reg)
    assert res.output is not None and res.flags == []
    contents = [a.content for a in res.output.artifacts]  # type: ignore[attr-defined]
    assert sorted(contents) == sorted(a.content for a in _v3().artifacts)
    task = TaskItem(
        task_id="t",
        track="common",
        target_competencies=ctx.targets,
        tech_env=ctx.env,
        difficulty_target="high",
        time_limit_min=60,
    )
    out = apply(task, res)
    assert len(out.artifacts) == 9 and out.max_requests == 3
