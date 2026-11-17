"""기능 개발형 공통 과제 서술 (spec/feature.py) — 가상 공고·가짜 LLM. 지원자 데이터 없음."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.nodes import spec as spec_mod
from task_spec.nodes.spec import FLAG_FEATURE_DEFERRED, make_draft_spec
from task_spec.prompting import PromptError, PromptRegistry
from task_spec.schemas import ApprovalDecision, DraftFeatureOutput, TaskItem
from task_spec.spec.build import CommonDraft, build_common
from task_spec.spec.common import FEATURE_DEBUG_MIN
from task_spec.spec.draft import FLAG_DRAFT_INVALID, FLAG_DRAFT_REPAIRED, DraftResult
from task_spec.spec.feature import (
    apply_feature,
    check_feature,
    clarification_overlaps,
    draft_feature_task,
    repair_feature,
    shuffle_feature,
)
from task_spec.spec.store import InMemoryCommonStore
from tests.spec.test_draft import _ctx
from tests.spec.test_task_kind_flow import _state

_REGISTRY = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


class NoAnalysis:
    """요청 글 단계(draft_analysis_request)를 끈 레지스트리 — 기능형 서술만 보는 테스트용. 요청 글 단계는 test_analysis_request 가 본다
    (CURRENT v3 — 2026-11-17 사용자 결정 (나))."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
        if task == "draft_analysis_request" and version is None:
            raise PromptError("요청 글 단계를 끈 테스트")
        return self._registry.get(task, version)


class _V1:
    """이 파일은 v1(공개 사례 원문 입력, LLM 이 숨은 요구까지 씀) 경로를 본다 — CURRENT 는 v2 (test_feature_seed). 요청 글 단계는 끈다."""

    def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
        return NoAnalysis(_REGISTRY).get(
            task, version or (1 if task == "draft_feature_task" else None)
        )


PROMPTS = _V1()


def _art(aid: str, role: str, title: str, content: str) -> dict[str, Any]:
    return {"artifact_id": aid, "title": title, "content": content, "role": role}


def _fdraft() -> dict[str, Any]:
    return {
        "title": "가상 주문 상태 변경 이력 추가",
        "scenario": "가상의 물류 회사가 주문 상태 변경마다 이력을 남기려 한다. 아래 자료 중 최대 2개를 요청해 볼 수 있고, 질문은 최대 2개까지 할 수 있습니다.",
        "requirements": [
            {"req_id": "r1", "text": "상태 변경 요청마다 이력을 남긴다"},
            {"req_id": "r2", "text": "이력을 최신순으로 조회한다"},
        ],
        "acceptance_public": [
            {"ac_id": "p1", "text": "성공한 변경 1건이면 이력 1건"},
            {"ac_id": "p2", "text": "조회 결과는 최신순"},
        ],
        "deliverables": ["핵심 구현", "테스트", "설계 메모"],
        "artifacts": [
            _art("a1", "key", "배송 서비스 설정 파일", "pool: 8\nthreads: 32"),
            _art("a2", "support", "주문 테이블 스키마", "CREATE TABLE orders (id bigint)"),
            _art("a3", "distractor", "포인트 서비스 코드", "class PointService {}"),
            _art("a4", "distractor", "배송 API 응답 지표", "p50 40ms"),
            _art("a5", "distractor", "주문 서비스 기존 테스트", "void ok() {}"),
            _art("a6", "distractor", "백오피스 화면 명세", "목록 화면"),
        ],
        "max_requests": 2,
        "clarifications": [
            {
                "clarification_id": "c1",
                "topic": "피크 요청량",
                "answer": "마감 직전 수십 건이 동시에 들어온다",
                "reveals": ["t1"],
            },
            {
                "clarification_id": "c2",
                "topic": "보존 기간",
                "answer": "3년 보관",
                "reveals": [],
            },
        ],
        "max_clarifications": 2,
        "traps": [
            {
                "trap_id": "t1",
                "description": "피크 동시 요청에서 요청당 자원을 두 개 쥐면 고갈",
                "why_ai_fails": "AI 는 이력을 별도 단위로 즉시 쓰는 구현을 낸다",
            }
        ],
        "acceptance_hidden": [{"ac_id": "h1", "trap_id": "t1", "text": "동시 32건 모두 기한 안"}],
        "accepted_designs": [
            {
                "design_id": "d1",
                "summary": "같은 단위 안에서 기록",
                "conditions": "실패 기록은 끝난 뒤",
                "trap_ids": ["t1"],
            },
            {
                "design_id": "d2",
                "summary": "끝난 뒤 별도로 기록",
                "conditions": "기록 실패 처리",
                "trap_ids": ["t1"],
            },
        ],
        "debug_stage": {
            "scenario": "배포 다음 날 마감 시간대에 상태 변경 요청 일부가 오래 걸리다 실패했다.",
            "trap_ids": ["t1"],
            "artifacts": [
                _art("a7", "key", "배송 API 오류 로그", "timeout after 5000ms"),
                _art("a8", "distractor", "DB 서버 자원 지표", "cpu 20%"),
                _art("a9", "support", "배포 기록", "v2 배포 14:00"),
            ],
            "max_requests": 2,
            "deliverables": [
                "원인 진단과 수정 (이미 대응했다면 이 증상이 내 구현에서 생기지 않는 근거)"
            ],
            "observables": [
                {"kind": "first_mention_of", "value": "자원 고갈"},
                {"kind": "requests_artifact", "value": "a7"},
                {"kind": "catches_trap", "value": "t1"},
            ],
        },
        "invariant": "요청당 자원 점유 범위가 피크 동시성과 맞물려 고갈",
        "variable": ["domain_surface", "numbers"],
        "observables": [
            {"kind": "first_mention_of", "value": "점유 범위"},
            {"kind": "catches_trap", "value": "t1"},
            {"kind": "requests_artifact", "value": "a1"},
            {"kind": "asks_clarification", "value": "c1"},
        ],
        "behavioral_anchors": {
            "level_1": {"behavior": "AI 첫 구현을 그대로 냄", "criterion_ids": ["E1.1#1"]},
            "level_3": {"behavior": "자료로 일부 확인", "criterion_ids": ["E2.1#1"]},
            "level_5": {"behavior": "운영 조건을 먼저 확인", "criterion_ids": ["E1.1#2", "E2.1#2"]},
        },
        "tech_used": ["Python"],
    }


def _out(d: dict[str, Any] | None = None) -> DraftFeatureOutput:
    return DraftFeatureOutput.model_validate(d or _fdraft())


def test_valid_feature_draft_passes() -> None:
    assert check_feature(_out(), _ctx()) == []


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda d: d["acceptance_hidden"][0].update(trap_id="t9"), "t9"),
        (lambda d: d["accepted_designs"][1].update(trap_ids=["t9"]), "허용 설계가 1개"),
        (lambda d: d["observables"].append({"kind": "asks_clarification", "value": "c9"}), "c9"),
        (lambda d: d["requirements"][0].update(text="트랜잭션 안에서 이력을 남긴다"), "구현 방법"),
        (lambda d: d["acceptance_public"][0].update(text="점유 범위가 짧아야 한다"), "점유 범위"),
        (lambda d: d["debug_stage"].update(scenario="자원 고갈로 실패했다"), "2단계 상황"),
        (lambda d: d["debug_stage"]["artifacts"][0].update(artifact_id="a1"), "겹침"),
        (
            lambda d: d["debug_stage"]["observables"][1].update(value="a2"),
            "2단계 requests_artifact",
        ),
        (lambda d: d["debug_stage"].update(trap_ids=["t5"]), "t5"),
        (lambda d: d.update(max_clarifications=3), "max_clarifications"),
        (lambda d: d["clarifications"][1].update(answer="점유 범위를 줄이면 된다"), "점유 범위"),
    ],
)
def test_feature_check_catches_each_problem(mutate: Any, expect: str) -> None:
    d = copy.deepcopy(_fdraft())
    mutate(d)
    problems = check_feature(_out(d), _ctx())
    assert any(expect in p for p in problems), problems


def test_howto_terms_match_words_not_substrings() -> None:
    d = copy.deepcopy(_fdraft())
    d["requirements"][0]["text"] = (
        "blocklist 에 오른 주문은 상태 변경을 거부한다"  # 'lock' 이 단어로는 없음
    )
    assert check_feature(_out(d), _ctx()) == []


def test_shuffle_renumbers_stages_contiguously_and_keeps_refs() -> None:
    out = shuffle_feature(_out(), "seed-x")
    assert sorted(a.artifact_id for a in out.artifacts) == [f"a{i}" for i in range(1, 7)]
    assert sorted(a.artifact_id for a in out.debug_stage.artifacts) == ["a7", "a8", "a9"]
    key1 = next(a for a in out.artifacts if a.title == "배송 서비스 설정 파일")
    req1 = [o.value for o in out.observables if o.kind == "requests_artifact"]
    assert req1 == [key1.artifact_id]
    key2 = next(a for a in out.debug_stage.artifacts if a.title == "배송 API 오류 로그")
    req2 = [o.value for o in out.debug_stage.observables if o.kind == "requests_artifact"]
    assert req2 == [key2.artifact_id]
    assert check_feature(out, _ctx()) == []
    assert shuffle_feature(_out(), "seed-x") == out  # 같은 seed → 같은 순서


def test_shuffle_leaves_overlapping_ids_for_check() -> None:
    d = copy.deepcopy(_fdraft())
    d["debug_stage"]["artifacts"][0]["artifact_id"] = "a1"
    out = _out(d)
    assert shuffle_feature(out, "s") is out


def test_repair_drops_bad_refs_and_keeps_debug_trap() -> None:
    d = copy.deepcopy(_fdraft())
    d["acceptance_hidden"].append({"ac_id": "h2", "trap_id": "t9", "text": "x"})
    d["clarifications"][0]["reveals"] = ["t1", "t9"]
    d["debug_stage"]["trap_ids"] = ["t9"]
    d["observables"].append({"kind": "asks_clarification", "value": "c9"})
    fixed, ok = repair_feature(_out(d), _ctx())
    assert ok
    assert [h.ac_id for h in fixed.acceptance_hidden] == ["h1"]
    assert fixed.clarifications[0].reveals == ["t1"]
    assert fixed.debug_stage.trap_ids == ["t1"]  # 비면 첫 숨은 요구로
    assert all(o.value != "c9" for o in fixed.observables)


def _skeleton() -> TaskItem:
    from task_spec.schemas import TargetCompetency, TechEnv

    return TaskItem(
        task_id="t",
        track="common",
        task_kind="feature",
        target_competencies=[TargetCompetency(competency_id="comp:a", label="A")],
        tech_env=TechEnv(languages=["Python"], basis="공고 언어"),
        difficulty_target="high",
        time_limit_min=60,
    )


def test_apply_feature_fills_two_stages() -> None:
    res = DraftResult(output=_out(), provenance_id="prov-1")
    task = apply_feature(_skeleton(), res)
    assert task.task_kind == "feature" and task.title == "가상 주문 상태 변경 이력 추가"
    assert [r.req_id for r in task.requirements] == ["r1", "r2"]
    assert task.debug_stage is not None
    assert task.debug_stage.time_limit_min == FEATURE_DEBUG_MIN
    assert task.debug_stage.reveal_after == "stage1_submitted"
    assert len(task.accepted_designs) == 2 and task.provenance_id == "prov-1"
    # 한도 없음 (analysis-stage.md §4.4, 2026-11-16) — 초안의 한도 칸은 명세에 넣지 않는다
    assert (task.max_requests, task.max_clarifications, task.debug_stage.max_requests) == (
        None,
        None,
        None,
    )
    # 초안의 공개 수용 기준은 명세에서 요구 연결 칸이 있는 모델로 (아직 비어 있음)
    assert all(a.req_ids == [] for a in task.acceptance_public)
    assert [a.ac_id for a in task.acceptance_public] == [a.ac_id for a in _out().acceptance_public]


async def _no_sleep(_: float) -> None:
    return None


def _client(fake: FakeBackend) -> LLMClient:
    return LLMClient({"opencode": fake, "openai": fake}, sleep=_no_sleep)


async def test_draft_feature_first_answer_ok_with_real_prompt() -> None:
    fake = FakeBackend().push(Completion(json.dumps(_fdraft(), ensure_ascii=False)))
    res = await draft_feature_task(_ctx(), llm=_client(fake), prompts=PROMPTS)
    assert res.output is not None and res.flags == [] and len(fake.calls) == 1
    assert res.provenance[0].prompt == "draft_feature_task@1"


async def test_draft_feature_reasks_then_repairs() -> None:
    bad = copy.deepcopy(
        _fdraft()
    )  # 스키마는 통과하고 검사에서 걸리는 답 (숨은 요구 하나에 허용 설계 1개)
    bad["accepted_designs"][1]["trap_ids"] = ["t9"]
    fake = FakeBackend().push(
        Completion(json.dumps(bad, ensure_ascii=False)),
        Completion(json.dumps(bad, ensure_ascii=False)),
    )
    res = await draft_feature_task(_ctx(), llm=_client(fake), prompts=PROMPTS)
    assert len(fake.calls) == 2 and res.output is not None
    assert any(f.startswith(FLAG_DRAFT_INVALID) for f in res.flags)


async def test_repaired_format_clarification_is_not_a_failure() -> None:
    """2026-11-15 손 시범 seats-ae1029: 재요청 뒤에도 단서 아닌 추가 요구가 응답 형식을 정해(c3 '차감 실패 시 응답 형식') 고쳐 쓰기가
    지웠는데, 지우기 전 문제로 '자동 검증 실패' 가 붙어 명세 실패로 셌다 — 다시 검사해 풀렸으면 '코드가 고침' 만."""
    bad = copy.deepcopy(_fdraft())
    bad["clarifications"].append(
        {
            "clarification_id": "c3",
            "topic": "차감 실패 시 응답 형식",
            "answer": "409 와 사유 코드를 돌려준다",
            "reveals": [],
        }
    )
    fake = FakeBackend().push(
        Completion(json.dumps(bad, ensure_ascii=False)),
        Completion(json.dumps(bad, ensure_ascii=False)),
    )
    res = await draft_feature_task(_ctx(), llm=_client(fake), prompts=PROMPTS)
    assert len(fake.calls) == 2 and res.output is not None
    assert [c.clarification_id for c in res.output.clarifications] == ["c1", "c2"]
    assert not any(f.startswith(FLAG_DRAFT_INVALID) for f in res.flags), res.flags
    assert any(f.startswith(FLAG_DRAFT_REPAIRED) and "c3" in f for f in res.flags)


async def test_node_drafts_feature_when_reviewer_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """승인자가 기능형을 지정 → draft_feature_task 로 서술, 판 키는 기능형 프롬프트. 기본값은 꺼져 있어 첫 판은 이슈형."""

    async def fake_build(*args: Any, **kw: Any) -> CommonDraft:
        built = await build_common(*args, **kw)
        return CommonDraft(task=built.task, ctx=_ctx(), flags=built.flags)

    monkeypatch.setattr(spec_mod, "build_common", fake_build)
    fake = FakeBackend().push(Completion(json.dumps(_fdraft(), ensure_ascii=False)))
    store = InMemoryCommonStore()
    draft = make_draft_spec(None, llm=_client(fake), prompts=PROMPTS, store=store)

    # 기본값 규칙은 feature 지만 꺼져 있음 → 이슈형 (LLM 없이 비교하려고 draft_task 응답을 넣지 않고 flag 만 본다)
    choice = await spec_mod.choose_kind(
        _state("run-a").posting_profile,  # type: ignore[arg-type]
        "p-backend",
        "onto-v2026.11.02",
        store=store,
        requested=None,
        current=None,
        rule_feature_on=False,
    )
    assert choice[0] == "incident" and choice[1][0].startswith(FLAG_FEATURE_DEFERRED)

    revise = ApprovalDecision(
        status="revise",
        reviewer="reviewer-fake",
        comment="기능 개발형으로",
        task_kind="feature",
        revise_target="common",
        at=datetime(2026, 11, 6, tzinfo=UTC),
    )
    out = await draft(_state("run-a", approval=revise))
    task = out["spec"].common_tasks[0]
    assert task.task_kind == "feature" and task.debug_stage is not None
    assert task.requirements and task.accepted_designs
    assert out["spec"].common_refs[0].prompt_ref == "draft_feature_task@1"


def test_slots_see_feature_public_parts_and_hide_both_stage_concepts() -> None:
    from task_spec.spec.slots import common_secrets, common_visible

    task = apply_feature(_skeleton(), DraftResult(output=_out()))
    visible = common_visible(task)
    assert "공개 요구: 상태 변경 요청마다 이력을 남긴다" in visible
    assert "공개 수용 기준:" in visible
    assert "동시 32건" not in visible and "배포 다음 날" not in visible  # 숨김 기준·2단계는 안 보임
    secrets = common_secrets(task, _ctx())
    assert "점유 범위" in secrets and "자원 고갈" in secrets  # 2단계 원인 개념도 슬롯에 쓰면 안 됨


def test_non_clue_clarification_repeating_requirement_or_format_is_flagged_and_dropped() -> None:
    """#165 — 함정 단서가 아닌 추가 요구가 공식 요구를 되풀이하거나 응답 형식을 정하면 진행자가 두 항목을 골라 "한 가지씩"이 나간다."""
    d = _fdraft()
    d["requirements"][0]["text"] = (
        "상태 변경이 실패하거나 거절된 요청도 실패 사유와 함께 이력을 남긴다"
    )
    d["clarifications"].append(
        {
            "clarification_id": "c3",
            "topic": "실패한 변경 기록",
            "answer": "상태 변경이 실패하거나 거절된 요청도 실패 사유와 함께 이력을 남깁니다",
            "reveals": [],
        }
    )
    d["clarifications"].append(
        {
            "clarification_id": "c4",
            "topic": "거절 시 응답 형식",
            "answer": "409 와 사유 코드",
            "reveals": [],
        }
    )
    out = _out(d)
    got = clarification_overlaps(out)
    assert set(got) == {"c3", "c4"}
    assert "공식 요구 r1 를 되풀이" in got["c3"] and "응답 형식을 정함" in got["c4"]
    problems = check_feature(out, _ctx())
    assert any("c3 가 공식 요구 r1" in p for p in problems)
    assert any("c4 가 요청·응답 형식" in p for p in problems)
    # 재요청 뒤에도 남으면 빼고(새 정보 없음), 관찰 항목도 따라 정리된다 — 함정 단서 c1·업무 사실 c2 는 그대로
    fixed, _ = repair_feature(out, _ctx())
    assert [c.clarification_id for c in fixed.clarifications] == ["c1", "c2"]
    assert all(o.value not in ("c3", "c4") for o in fixed.observables)


def test_clue_and_new_rule_clarifications_are_kept() -> None:
    """함정 단서(reveals 있음)는 보지 않는다. 답에 상태 코드가 있어도 주제가 새 규칙(인가 등)이면 응답 형식이 아니다."""
    d = _fdraft()
    d["clarifications"][1] = {
        "clarification_id": "c2",
        "topic": "차감 API의 인증·인가 방식",
        "answer": "토큰의 차량과 요청 대상이 다르면 403 을 반환합니다",
        "reveals": [],
    }
    d["clarifications"][0]["topic"] = (
        "피크 시 HTTP 요청량"  # 단서는 주제에 HTTP 가 있어도 보지 않음
    )
    assert clarification_overlaps(_out(d)) == {}
