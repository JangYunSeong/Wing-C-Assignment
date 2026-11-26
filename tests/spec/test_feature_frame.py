"""draft_feature_task v13 — 함정 틀의 공개 쪽 입력 (#318, docs/design/trap-frame.md §7). 가상 틀·가짜 생성만.

숨은 쪽(틀이 다듬은 숨은 요구·금지 낱말)은 틀 적용 재료로 feature_seed 에 이미 들어가므로, 새 입력에는 공개 쪽만 있다.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from task_spec.prompting import assemble
from task_spec.schemas import TrapFrame
from task_spec.spec import feature as feature_mod
from task_spec.spec.draft import DraftResult, build_input
from task_spec.spec.feature import (
    NO_FRAME,
    draft_feature_task,
    extra_input,
    frame_public_text,
    seed_block,
)
from tests.spec.test_feature_seed import _REG, BACKEND, _seed_ctx

FRAME = TrapFrame.model_validate(
    {
        "kind": "design",
        "hidden_requirement": "가상 숨은 요구 — 공개 쪽 입력에 없어야 함",
        "public_rules": ["피크 동시 요청이 커넥션 풀보다 많다 (가상)"],
        "forbidden_terms": ["가상 금지 낱말"],
        "forbidden_storage": [{"entity": "Session", "why": "흔한 구현이 세션을 DB 테이블에 둔다"}],
        "reason": "pool_acquire_timeout",
    }
)


class _Pinned:
    """draft_feature_task 만 지정한 판으로 (CURRENT 는 평가 뒤에 바꾼다)."""

    def __init__(self, version: int) -> None:
        self.version = version

    def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
        pinned = self.version if task == "draft_feature_task" else None
        return _REG.get(task, version or pinned)


def test_the_public_side_of_the_frame_is_rules_and_storage_only() -> None:
    assert frame_public_text(None) == NO_FRAME
    text = frame_public_text(FRAME)
    assert "공개할 것: 피크 동시 요청이 커넥션 풀보다 많다 (가상)" in text
    assert "엔티티(테이블)로 두지 않을 것: Session (흔한 구현이 세션을 DB 테이블에 둔다)" in text
    assert (
        "가상 숨은 요구" not in text and "가상 금지 낱말" not in text
    )  # 숨은 쪽·금지 낱말은 feature_seed 로
    empty = FRAME.model_copy(update={"public_rules": [], "forbidden_storage": []})
    assert frame_public_text(empty) == NO_FRAME


async def test_v13_gets_the_frame_and_v12_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def capture(ctx: Any, **kw: Any) -> DraftResult:
        seen.append(kw["extra_input"])
        return DraftResult()

    monkeypatch.setattr(feature_mod, "draft_task", capture)
    framed = dataclasses.replace(_seed_ctx(), trap_frame=FRAME)
    await draft_feature_task(framed, llm=None, prompts=_Pinned(13))  # type: ignore[arg-type]
    await draft_feature_task(_seed_ctx(), llm=None, prompts=_Pinned(13))  # type: ignore[arg-type]
    await draft_feature_task(framed, llm=None, prompts=_Pinned(12))  # type: ignore[arg-type]
    assert seen[0]["trap_frame"] == frame_public_text(FRAME)
    assert seen[1]["trap_frame"] == NO_FRAME  # 틀 없는 공고(일반 파이프라인·공개 재료)
    assert "trap_frame" not in seen[2]  # v12 는 입력 칸이 없다


def test_v13_renders_the_frame_block_after_the_material() -> None:
    v13 = _REG.get("draft_feature_task", 13)
    extra = {
        **extra_input(),
        "feature_seed": seed_block(BACKEND),
        "trap_frame": frame_public_text(FRAME),
    }
    built = assemble(
        v13, build_input(_seed_ctx(), None, v13.input_model, extra), ontology_version="v"
    )
    user = built.messages[-1].content
    assert (
        user.index("기능형 재료")
        < user.index("함정 틀 (공개 쪽")
        < user.index("Session (흔한 구현이")
    )
