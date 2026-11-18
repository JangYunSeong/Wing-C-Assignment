"""경험 검증만 다시 만드는 수정 요청(revise_target=verification)은 명세의 공통 과제를 그대로 쓴다 (#234).

저장소에서 판을 다시 고르면 (a) 배포로 프롬프트 판이 바뀐 뒤 공통 과제를 새로 만들고(LLM 호출),
(b) 다른 실행이 승인한 판으로 되돌아가고, (c) 다른 실행이 수정 요청으로 만든 새 초안으로 바뀐다 —
승인자가 보고 고친 공통 과제가 말없이 사라진다. 이슈형·기능형·문제 분석 판 경로 모두.
가상 공고·가짜 LLM — 지원자 데이터 없음.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from task_spec.llm import Completion, FakeBackend
from task_spec.nodes import spec as spec_mod
from task_spec.prompting import PromptError, PromptRegistry
from task_spec.schemas import ApprovalDecision, RunState, TaskSpec
from task_spec.spec.build import CommonDraft, build_common
from task_spec.spec.store import InMemoryCommonStore
from tests.spec.test_analysis_request import _good
from tests.spec.test_draft import _client, _ctx, _draft
from tests.spec.test_feature import _fdraft
from tests.spec.test_task_kind_flow import _state

_REGISTRY = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
DraftNode = Callable[[RunState], Awaitable[dict[str, Any]]]


class _Pins:
    """작업별 프롬프트 판을 고정한다 (배포마다 CURRENT 가 다른 것처럼). 0 = 그 작업의 CURRENT 가 없음."""

    def __init__(self, pins: dict[str, int]) -> None:
        self.pins = pins

    def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
        pinned = self.pins.get(task)
        if pinned == 0 and version is None:
            raise PromptError(f"{task} CURRENT 없음 (테스트)")
        return _REGISTRY.get(task, version or pinned)


@dataclass(frozen=True)
class _Path:
    name: str
    kind: str
    first: dict[str, int]  # 첫 초안 때 프롬프트 판
    later: dict[str, int]  # 배포 뒤 (CURRENT 승격) — 공통 과제 저장 키(prompt_ref)가 달라진다
    replies: tuple[dict[str, Any], ...]  # 공통 과제 한 판을 만드는 LLM 답 (차례대로)


PATHS = [
    _Path("incident", "incident", {"draft_task": 2}, {"draft_task": 3}, (_draft(),)),
    _Path(
        "feature",
        "feature",
        {"draft_feature_task": 1, "draft_analysis_request": 0},
        {"draft_feature_task": 2, "draft_analysis_request": 0},
        (_fdraft(),),
    ),
    _Path(
        "analysis",
        "feature",
        {"draft_feature_task": 1, "draft_analysis_request": 1},
        {"draft_feature_task": 1, "draft_analysis_request": 2},
        (_fdraft(), _good()),
    ),
]
COMMON = {"comment": "상황 설명을 줄여 주세요", "revise_target": "common"}
VERIFY = {"comment": "면접 질문을 더 구체적으로", "revise_target": "verification"}


@pytest.fixture(autouse=True)
def _drafting_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """온톨로지 없이도 서술 입력(ctx)이 있게 — 공통 과제를 LLM 으로 쓰는 경로를 탄다."""

    async def fake_build(*args: Any, **kw: Any) -> CommonDraft:
        built = await build_common(*args, **kw)
        return CommonDraft(task=built.task, ctx=_ctx(), flags=built.flags)

    monkeypatch.setattr(spec_mod, "build_common", fake_build)


def _decision(status: str, **over: Any) -> ApprovalDecision:
    return ApprovalDecision(
        status=status,  # type: ignore[arg-type]
        reviewer="reviewer-fake",
        at=datetime(2026, 11, 18, tzinfo=UTC),
        **over,
    )


def _node(
    path: _Path, pins: dict[str, int], fake: FakeBackend, store: InMemoryCommonStore
) -> DraftNode:
    return spec_mod.make_draft_spec(
        None,
        llm=_client(fake),
        prompts=_Pins(pins),
        store=store,
        feature_default=path.kind == "feature",  # 기능형 경로는 첫 초안부터 기능형 (기본값 규칙)
    )


def _push(fake: FakeBackend, path: _Path) -> None:
    fake.push(*(Completion(json.dumps(r, ensure_ascii=False)) for r in path.replies))


def _assert_kept(before: TaskSpec, after: TaskSpec) -> None:
    assert after.revision == before.revision + 1
    assert after.common_tasks == before.common_tasks  # 같은 공통 과제 (id·본문·표시)
    assert [(r.common_id, r.revision, r.prompt_ref) for r in after.common_refs] == [
        (r.common_id, r.revision, r.prompt_ref) for r in before.common_refs
    ]


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
async def test_verification_revise_keeps_common_after_prompt_change(path: _Path) -> None:
    """(a) 첫 초안 뒤 CURRENT 가 바뀌어도 경험 검증 수정은 공통 과제를 새로 만들지 않는다."""
    store, fake = InMemoryCommonStore(), FakeBackend()
    _push(fake, path)
    s1 = (await _node(path, path.first, fake, store)(_state("run-a")))["spec"]
    first = s1.common_tasks[0]
    assert first.task_kind == path.kind and first.title
    calls, rows = len(fake.calls), len(store.rows)

    revise = _decision("revise", **VERIFY)
    out = await _node(path, path.later, fake, store)(_state("run-a", spec=s1, approval=revise))
    _assert_kept(s1, out["spec"])
    assert len(fake.calls) == calls and len(store.rows) == rows  # LLM 재호출·새 판 없음
    assert not out.get("provenance")  # 공통 과제 provenance 는 첫 초안 때 이미 State 에 있다


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
async def test_verification_revise_does_not_fall_back_to_approved_common(path: _Path) -> None:
    """(b) 다른 실행이 승인한 판 X 가 있어도, 이 실행이 수정 요청으로 만든 B 를 그대로 쓴다."""
    store, fake = InMemoryCommonStore(), FakeBackend()
    node = _node(path, path.first, fake, store)
    _push(fake, path)
    x = (await node(_state("run-x")))["spec"]
    await spec_mod.make_finalize(store)(_state("run-x", spec=x, approval=_decision("approved")))
    b1 = (await node(_state("run-b")))["spec"]
    assert b1.common_refs[0].common_id == x.common_refs[0].common_id  # 승인된 판 재사용
    _push(fake, path)
    b2 = (await node(_state("run-b", spec=b1, approval=_decision("revise", **COMMON))))["spec"]
    assert (b2.common_refs[0].revision, b2.common_refs[0].status) == (2, "draft")
    calls = len(fake.calls)

    b3 = (await node(_state("run-b", spec=b2, approval=_decision("revise", **VERIFY))))["spec"]
    _assert_kept(b2, b3)  # 승인된 X 로 되돌아가지 않는다
    assert b3.common_tasks[0].task_id != x.common_tasks[0].task_id
    assert len(fake.calls) == calls


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
async def test_verification_revise_keeps_own_draft_over_newer_draft(path: _Path) -> None:
    """(c) 승인 판이 없을 때 다른 실행이 수정 요청으로 만든 새 초안(r2)으로 바뀌지 않는다."""
    store, fake = InMemoryCommonStore(), FakeBackend()
    node = _node(path, path.first, fake, store)
    _push(fake, path)
    a1 = (await node(_state("run-a")))["spec"]
    b1 = (await node(_state("run-b")))["spec"]
    assert b1.common_refs[0].reused is True  # 같은 초안 재사용
    _push(fake, path)
    b2 = (await node(_state("run-b", spec=b1, approval=_decision("revise", **COMMON))))["spec"]
    assert b2.common_refs[0].revision == 2
    calls = len(fake.calls)

    a2 = (await node(_state("run-a", spec=a1, approval=_decision("revise", **VERIFY))))["spec"]
    _assert_kept(a1, a2)
    assert a2.common_tasks[0].task_id != b2.common_tasks[0].task_id
    assert len(fake.calls) == calls


async def test_verification_revise_without_store_keeps_task_id() -> None:
    """저장소 없는 그래프(단위 테스트·DB 없음)도 같은 공통 과제 — 실행 범위 id 가 판마다 바뀌지 않는다."""
    draft = spec_mod.make_draft_spec(None)
    s1 = (await draft(_state("run-a")))["spec"]
    s2 = (await draft(_state("run-a", spec=s1, approval=_decision("revise", **VERIFY))))["spec"]
    assert s2.revision == 2 and s2.common_tasks == s1.common_tasks
    assert s2.common_tasks[0].task_id == "run-a-r1-common-1"
