"""문제 분석 판 — 요청 글·요청자 지식 생성 (spec/analysis_request.py, analysis-stage.md §4·§5·§5.1). 가상 공고·가짜 LLM — 지원자 데이터 없음."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.nodes import spec as spec_mod
from task_spec.nodes.spec import make_draft_spec
from task_spec.prompting import PromptError, PromptRegistry
from task_spec.schemas import (
    ApprovalDecision,
    DraftAnalysisOutput,
    DraftAnalysisOutputV2,
    TaskItem,
)
from task_spec.spec.analysis_request import (
    FLAG_INVALID,
    FLAG_LEAK,
    FLAG_OVERLAP,
    FLAG_REPAIRED,
    apply_analysis,
    build_analysis_input,
    check_analysis_draft,
    draft_analysis_request,
    leak_flags,
    overlap_flags,
    repair_analysis,
)
from task_spec.spec.build import CommonDraft, build_common
from task_spec.spec.draft import DraftResult
from task_spec.spec.feature import apply_feature
from task_spec.spec.store import InMemoryCommonStore
from task_spec.starter.facilitator import facilitator_view
from tests.spec.test_draft import _ctx
from tests.spec.test_feature import _fdraft, _out, _skeleton
from tests.spec.test_task_kind_flow import _state

_REGISTRY = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


class _Prompts:
    """draft_analysis_request v1 은 평가 전이라 CURRENT 가 없다 — CURRENT 가 있는 것처럼 v1 을 준다 (기능형은 v1 경로)."""

    def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
        fixed = {"draft_feature_task": 1, "draft_analysis_request": 1}
        return _REGISTRY.get(task, version or fixed.get(task))


def _client(fake: FakeBackend) -> LLMClient:
    async def _no_sleep(_: float) -> None:
        return None

    return LLMClient({"opencode": fake, "openai": fake}, sleep=_no_sleep)


def _task() -> TaskItem:
    return apply_feature(_skeleton(), DraftResult(output=_out(), provenance_id="prov-1"))


def _good() -> dict[str, Any]:
    return {
        "background": "가상의 물류 회사가 주문 상태 변경 API 를 운영하고 있다. 운영팀은 주문 상태가 언제 왜 바뀌었는지 알 수 없어 고객 문의에 답하기 어렵다.",
        "request": "주문 상태가 언제 누구 요청으로 바뀌었는지 남겨 두고, 나중에 주문마다 확인할 수 있게 해 주세요.",
        "request_mentions": ["r1"],
        "facts": [
            {
                "fact_id": "f1",
                "covers": ["r1", "p1"],
                "topic": "이력을 남기는 때",
                "ask_when": "상태 변경 요청마다 이력을 남기는지 물을 때",
                "answer": "상태 변경 요청이 올 때마다 이력을 한 건씩 남겨 주세요.",
                "answer_hint": "상태가 바뀔 때 남겨야 할 것이 있어요.",
            },
            {
                "fact_id": "f2",
                "covers": ["r2", "p2"],
                "topic": "이력 조회 순서",
                "ask_when": "이력을 어떤 순서로 보여 주는지 물을 때",
                "answer": "이력은 최신순으로 보여 주세요.",
                "answer_hint": "이력은 정해진 순서로 보여야 해요.",
            },
        ],
        "acceptance_links": [
            {"ac_id": "p1", "req_ids": ["r1"]},
            {"ac_id": "p2", "req_ids": ["r2"]},
        ],
    }


def _reply(d: dict[str, Any]) -> Completion:
    return Completion(json.dumps(d, ensure_ascii=False))


def test_input_has_public_fields_but_no_hidden_requirement() -> None:
    """숨은 요구·허용 설계는 입력에 넣지 않는다 (§5)."""
    inp = build_analysis_input(_task(), None, None).model_dump_json()
    assert "상태 변경 요청마다 이력을 남긴다" in inp and "p2 조회 결과는 최신순" in inp
    assert "c2 보존 기간 — 3년 보관" in inp  # 추가 요구는 사실로 다시 쓰지 않게 보여 준다
    assert "자원을 두 개 쥐면" not in inp and "같은 단위 안에서 기록" not in inp


def test_good_draft_passes_the_checks() -> None:
    assert check_analysis_draft(DraftAnalysisOutput.model_validate(_good()), _task(), _ctx()) == []


@pytest.mark.parametrize(
    ("edit", "fragment"),
    [
        (
            lambda d: d.update(request=d["request"] + " 자원 고갈이 없게."),
            "자원 고갈",
        ),  # 먼저 말할 개념
        (
            lambda d: d.update(background=d["background"] + " 자원 고갈이 걱정이다."),
            "배경 글에 숨은 요구",
        ),
        (lambda d: d.update(request="가" * 460), "요청 글이 김"),
        (lambda d: d.update(background="가" * 610), "배경 글이 김"),
        (
            lambda d: d["facts"][1].update(answer_hint=d["facts"][1]["answer"]),
            "f2 의 모호한 답이 구체 답과 같음",
        ),
        (lambda d: d["facts"].pop(), "덮는 사실이 없는"),  # r2·p2 를 물어서 알 수 없음
        (lambda d: d["acceptance_links"].pop(), "p2 에 req_ids 가 없음"),
    ],
)
def test_checks_catch_each_problem(edit: Any, fragment: str) -> None:
    d = copy.deepcopy(_good())
    edit(d)
    problems = check_analysis_draft(DraftAnalysisOutput.model_validate(d), _task(), _ctx())
    assert any(fragment in p for p in problems), problems


@pytest.mark.parametrize(
    ("edit", "fragment"),
    [
        (
            lambda d: d.update(request=d["request"] + " 하루 3번 확인합니다."),
            "요청 글에 수치 (3번)",
        ),
        (
            lambda d: d.update(background=d["background"] + " V2X 단말도 붙어 있다."),
            "배경 글에 수치 (V2X)",
        ),  # 이름 속 숫자도 표시 — 승인자가 낱말을 보고 가린다
        (
            lambda d: d.update(request="조회 결과는 최신순으로 해 주세요."),
            "요청 글이 공개 수용 기준 p2 문장과 거의 같음",
        ),
        (
            lambda d: d.update(background=d["background"] + " 상태 변경 요청마다 이력을 남긴다."),
            "배경 글이 공식 요구 r1 와 거의 같음",
        ),
        (
            lambda d: d["facts"][1].update(answer_hint="이력은 최신순으로 보여야 해요."),
            "f2 모호한 답에 구체 답의 말 (최신순",
        ),
        (
            lambda d: d.update(background=d["background"] + " 이력은 최신순으로 보여야 한다."),
            "배경 글에 f2 답의 말 (최신순",
        ),
    ],
)
def test_word_leaks_are_flagged_not_sent_back(edit: Any, fragment: str) -> None:
    """낱말 겹침으로 본 흘림은 재요청이 아니라 표시만 — v1 평가 오탐 6건 중 4건·낱말 바꾼 흘림 놓침 (2026-11-16 사용자)."""
    d = copy.deepcopy(_good())
    edit(d)
    out = DraftAnalysisOutput.model_validate(d)
    assert check_analysis_draft(out, _task(), _ctx()) == []
    flags = leak_flags(out, _task())
    assert flags and flags[0].startswith(FLAG_LEAK) and fragment in flags[0], flags


def test_good_draft_has_no_leak_flag() -> None:
    assert leak_flags(DraftAnalysisOutput.model_validate(_good()), _task()) == []


async def test_generate_applies_request_facts_and_links() -> None:
    fake = FakeBackend().push(_reply(_good()))
    res = await draft_analysis_request(
        _task(), _ctx(), llm=_client(fake), prompts=_REGISTRY, version=1
    )
    assert len(fake.calls) == 1 and res.output is not None and not res.flags
    task = apply_analysis(_task(), res)
    assert task.request is not None and task.request_mentions == ["r1"]
    assert task.background is not None and "운영팀" in task.background
    assert [(f.fact_id, f.covers, f.match_terms) for f in task.facts] == [
        ("f1", ["r1", "p1"], []),
        ("f2", ["r2", "p2"], []),
    ]
    assert [(a.ac_id, a.req_ids) for a in task.acceptance_public] == [
        ("p1", ["r1"]),
        ("p2", ["r2"]),
    ]
    assert res.provenance and res.provenance[0].prompt == "draft_analysis_request@1"


async def test_problems_are_sent_back_once_then_the_better_answer_is_kept() -> None:
    bad = copy.deepcopy(_good())
    bad["request"] += " 자원 고갈이 없게."
    fake = FakeBackend().push(_reply(bad), _reply(_good()))
    res = await draft_analysis_request(
        _task(), _ctx(), llm=_client(fake), prompts=_REGISTRY, version=1
    )
    assert len(fake.calls) == 2 and any("자원 고갈" in p for p in res.first_problems)
    assert res.output is not None and "고갈" not in res.output.request and not res.flags


async def test_code_repairs_references_and_flags_what_is_left() -> None:
    fixable = copy.deepcopy(_good())
    fixable["facts"][0]["covers"].append("r9")  # 없는 요구 — 코드가 지운다
    fake = FakeBackend().push(_reply(fixable), _reply(fixable))
    res = await draft_analysis_request(
        _task(), _ctx(), llm=_client(fake), prompts=_REGISTRY, version=1
    )
    assert res.output is not None and res.output.facts[0].covers == ["r1", "p1"]
    assert any(f.startswith(FLAG_REPAIRED) and "r9" in f for f in res.flags)
    assert not any(f.startswith(FLAG_INVALID) for f in res.flags)

    stuck = copy.deepcopy(_good())
    stuck["request"] += " 자원 고갈이 없게."  # 문장은 코드가 고치지 않는다
    fake = FakeBackend().push(_reply(stuck), _reply(stuck))
    res = await draft_analysis_request(
        _task(), _ctx(), llm=_client(fake), prompts=_REGISTRY, version=1
    )
    assert any(f.startswith(FLAG_INVALID) and "자원 고갈" in f for f in res.flags)


def test_fact_and_clarification_overlap_is_only_flagged() -> None:
    """사실 ↔ 추가 요구 겹침은 고치지 않고 표시만 (2026-11-16 사용자) — 손 시범 c3 ↔ f4 같은 경우."""
    task = _task()
    clars = [
        c.model_copy(update={"answer_hint": "요구에는 최신순만 정해져 있어요."})
        if c.clarification_id == "c2"
        else c
        for c in task.clarifications
    ]
    task = task.model_copy(update={"clarifications": clars})
    d = copy.deepcopy(_good())
    d["facts"][0]["ask_when"] = "상태 변경 이력을 얼마나 보존하는지(보존 기간) 물을 때"
    out = DraftAnalysisOutput.model_validate(d)
    flags = overlap_flags(out, task)
    assert flags and flags[0].startswith(FLAG_OVERLAP)
    assert "c2↔f1 (보존" in flags[0] and "c2↔f2 (c2 모호한 답에 '최신순')" in flags[0]
    applied = apply_analysis(task, type("R", (), {"output": out, "flags": []})())  # type: ignore[arg-type]
    assert [c.clarification_id for c in applied.clarifications] == [
        "c1",
        "c2",
    ]  # 추가 요구는 그대로
    assert any(f.startswith(FLAG_OVERLAP) for f in applied.review_flags)


async def test_spec_node_runs_analysis_only_when_the_prompt_has_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CURRENT 가 있으면(지금 저장소 — v3, 2026-11-17 사용자 결정 (나)) 기능형 서술 뒤 요청 글·사실을 붙이고 판 키에 그 판을 넣는다. 없으면 건너뛴다."""

    async def fake_build(*args: Any, **kw: Any) -> CommonDraft:
        built = await build_common(*args, **kw)
        return CommonDraft(task=built.task, ctx=_ctx(), flags=built.flags)

    monkeypatch.setattr(spec_mod, "build_common", fake_build)
    revise = ApprovalDecision(
        status="revise",
        reviewer="reviewer-fake",
        comment="기능 개발형으로",
        task_kind="feature",
        revise_target="common",
        at=datetime(2026, 11, 6, tzinfo=UTC),
    )
    fake = FakeBackend().push(
        Completion(json.dumps(_fdraft(), ensure_ascii=False)), _reply(_good())
    )
    draft = make_draft_spec(
        None, llm=_client(fake), prompts=_Prompts(), store=InMemoryCommonStore()
    )
    out = await draft(_state("run-a", approval=revise))
    task = out["spec"].common_tasks[0]
    assert task.request is not None and [f.fact_id for f in task.facts] == ["f1", "f2"]
    assert out["spec"].common_refs[0].prompt_ref == "draft_feature_task@1+draft_analysis_request@1"

    class _NoCurrent(_Prompts):
        def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
            if task == "draft_analysis_request" and version is None:
                raise PromptError("CURRENT 없음")  # 저장소 상태와 무관하게 CURRENT 가 없는 경우
            return super().get(task, version)

    fake = FakeBackend().push(Completion(json.dumps(_fdraft(), ensure_ascii=False)))
    draft = make_draft_spec(
        None, llm=_client(fake), prompts=_NoCurrent(), store=InMemoryCommonStore()
    )
    out = await draft(_state("run-b", approval=revise))
    task = out["spec"].common_tasks[0]
    assert task.request is None and task.facts == [] and len(fake.calls) == 1
    assert out["spec"].common_refs[0].prompt_ref == "draft_feature_task@1"


# ── v3: 추가 요구 질문 문장 · 생성기가 쓴 연결 우선 [2026-11-16 사용자] ──


def _linked_task() -> TaskItem:
    """생성기 v11 처럼 공개 수용 기준에 req_ids 가 이미 있는 명세."""
    t = _task()
    links = {"p1": ["r1"], "p2": ["r2"]}
    return t.model_copy(
        update={
            "acceptance_public": [
                a.model_copy(update={"req_ids": links[a.ac_id]}) for a in t.acceptance_public
            ]
        }
    )


def _v3(**over: Any) -> dict[str, Any]:
    d = copy.deepcopy(_good())
    d["acceptance_links"] = []
    d["clarification_asks"] = [
        {"clarification_id": "c1", "ask_when": "피크 시간 요청 규모를 콕 집어 물을 때"},
        {"clarification_id": "c2", "ask_when": "이력을 얼마나 오래 보관하는지 물을 때"},
    ]
    d.update(over)
    return d


async def test_v3_writes_clarification_asks_and_keeps_generator_links() -> None:
    task = _linked_task()
    assert "- p1 [r1] " in build_analysis_input(task, None, None).acceptance_public
    fake = FakeBackend().push(_reply(_v3()))
    res = await draft_analysis_request(
        task, _ctx(), llm=_client(fake), prompts=_REGISTRY, version=3
    )
    assert len(fake.calls) == 1 and not res.flags, res.flags
    done = apply_analysis(task, res)
    assert [(a.ac_id, a.req_ids) for a in done.acceptance_public] == [
        ("p1", ["r1"]),
        ("p2", ["r2"]),
    ]
    assert {c.clarification_id: c.ask_when for c in done.clarifications} == {
        "c1": "피크 시간 요청 규모를 콕 집어 물을 때",
        "c2": "이력을 얼마나 오래 보관하는지 물을 때",
    }
    view = facilitator_view("ct-x", done)  # 진행자 보기에 문장이 나간다
    assert [a.ask_when for a in view.stage1.clarifications.items] == [
        c.ask_when for c in done.clarifications
    ]
    assert res.provenance[0].prompt == "draft_analysis_request@3"


def test_v3_missing_or_unknown_ask_is_a_problem_and_unknown_is_repaired() -> None:
    task = _linked_task()
    d = _v3()
    d["clarification_asks"] = [
        d["clarification_asks"][0],
        {"clarification_id": "c9", "ask_when": "없는 항목"},
    ]
    out = DraftAnalysisOutputV2.model_validate(d)
    problems = check_analysis_draft(out, task, _ctx())
    assert any("추가 요구 c2 의 ask_when 이 없음" in p for p in problems)
    assert any("clarification_asks 에 없는 추가 요구: c9" in p for p in problems)
    fixed = repair_analysis(out, task)
    assert [a.clarification_id for a in fixed.clarification_asks] == ["c1"]  # type: ignore[attr-defined]


def test_v3_still_links_when_the_generator_did_not() -> None:
    """예전 생성기 명세(연결 없음) — v3 도 acceptance_links 를 쓰면 그대로 붙는다."""
    out = DraftAnalysisOutputV2.model_validate(_v3(acceptance_links=_good()["acceptance_links"]))
    assert check_analysis_draft(out, _task(), _ctx()) == []
    out = DraftAnalysisOutputV2.model_validate(_v3())
    assert any("p1 에 req_ids 가 없음" in p for p in check_analysis_draft(out, _task(), _ctx()))
