"""기능형 재료(공개 사례 feature 블록)와 draft_feature_task v2 — 공개 사례 파일(지원자 데이터 아님) + 가짜 LLM."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from task_spec.llm import Completion, FakeBackend
from task_spec.prompting import PromptRegistry
from task_spec.schemas import DraftFeatureOutput, DraftFeatureOutputV2
from task_spec.spec.feature import (
    FLAG_NO_SEED,
    FLAG_NO_STARTER,
    FLAG_SEED_UNREVIEWED,
    apply_feature,
    check_feature,
    draft_feature_task,
    fill_from_seed,
    seed_block,
)
from task_spec.spec.incidents import feature_ref, incident_ref, load_incidents
from tests.spec.test_draft import _ctx
from tests.spec.test_feature import _client, _fdraft, _skeleton

ROOT = Path(__file__).resolve().parents[2]
INCIDENTS = load_incidents(ROOT / "incidents" / "public_v1")
BACKEND = INCIDENTS["sw.backend"]
_REG = PromptRegistry(ROOT / "prompts")


class _V2:
    """draft_feature_task 를 v2 로 고정한 레지스트리 (CURRENT 는 평가 뒤에 바꾼다)."""

    def get(self, task: str, version: int | None = None):  # type: ignore[no-untyped-def]
        return _REG.get(task, version or (2 if task == "draft_feature_task" else None))


def _v2draft() -> dict[str, Any]:
    d = copy.deepcopy(_fdraft())
    d.pop("traps")
    d.pop("accepted_designs")
    return d


def _seed_ctx():  # type: ignore[no-untyped-def]
    return dataclasses.replace(_ctx(), incident=BACKEND)


def test_seeds_are_well_formed() -> None:
    seeded = {s: i for s, i in INCIDENTS.items() if i.feature is not None}
    assert {"sw.backend", "sw.frontend", "data.eng", "data.ml", "infra.dba"} <= set(seeded)
    for sub, inc in seeded.items():
        f = inc.feature
        assert f is not None and len(f.accepted_designs) >= 2, sub
        visible = (f.business_need + f.stage2_symptom).lower()
        assert not [t for t in [*f.leak_terms, *f.stage2_cause_terms] if t.lower() in visible], sub
    assert BACKEND.feature is not None and BACKEND.feature.starter == "delivery-audit"


def test_incident_fingerprint_ignores_feature_but_feature_fingerprint_does_not() -> None:
    bare = BACKEND.model_copy(update={"feature": None})
    assert incident_ref(bare) == incident_ref(BACKEND)  # 재료를 붙여도 이슈형 판은 그대로
    assert BACKEND.feature is not None
    changed = BACKEND.model_copy(
        update={"feature": BACKEND.feature.model_copy(update={"trigger": "다른 조건"})}
    )
    assert feature_ref(changed) != feature_ref(BACKEND)
    assert "+feature#" in feature_ref(BACKEND)


def test_seed_block_has_material_not_source_identity() -> None:
    block = seed_block(BACKEND)
    assert BACKEND.feature is not None and BACKEND.feature.business_need in block
    for secret in (
        BACKEND.org,
        BACKEND.url,
        BACKEND.title,
        BACKEND.symptom,
        *BACKEND.source_literals,
    ):
        assert secret not in block


def test_fill_from_seed_pins_hidden_parts() -> None:
    out = fill_from_seed(DraftFeatureOutputV2.model_validate(_v2draft()), BACKEND.feature)  # type: ignore[arg-type]
    assert [t.trap_id for t in out.traps] == ["t1"]
    assert BACKEND.feature is not None
    assert out.traps[0].description.startswith(BACKEND.feature.hidden_requirement)
    assert len(out.accepted_designs) == len(BACKEND.feature.accepted_designs)
    assert all(d.trap_ids == ["t1"] for d in out.accepted_designs)
    cause = [o.value for o in out.debug_stage.observables if o.kind == "first_mention_of"]
    assert cause[0] == BACKEND.feature.stage2_cause_terms[0]
    assert out.debug_stage.trap_ids == ["t1"]


def test_seed_leak_terms_are_checked_on_both_stages() -> None:
    ctx = _seed_ctx()
    ok = fill_from_seed(DraftFeatureOutputV2.model_validate(_v2draft()), BACKEND.feature)  # type: ignore[arg-type]
    assert check_feature(ok, ctx) == []
    d = _v2draft()
    d["scenario"] = "피크 때 커넥션 풀이 부족하지 않게 이력을 남긴다. " + d["scenario"]
    d["debug_stage"]["scenario"] = "마감 시간대에 커넥션 풀 교착이 생겼다."
    bad = fill_from_seed(DraftFeatureOutputV2.model_validate(d), BACKEND.feature)  # type: ignore[arg-type]
    problems = "\n".join(check_feature(bad, ctx))
    assert "1단계 지원자 화면" in problems and "커넥션 풀" in problems
    assert "2단계 상황" in problems


def test_howto_terms_that_are_the_feature_subject_are_allowed() -> None:
    inc = INCIDENTS["infra.dba"]
    assert inc.feature is not None
    own = inc.feature.model_copy(
        update={"business_need": inc.feature.business_need + " 트랜잭션 기록을 남긴다."}
    )
    ctx = dataclasses.replace(_ctx(), incident=inc.model_copy(update={"feature": own}))
    d = _v2draft()
    d["requirements"][0]["text"] = "트랜잭션 처리 결과를 기록한다"
    out = fill_from_seed(DraftFeatureOutputV2.model_validate(d), own)
    assert not [p for p in check_feature(out, ctx) if "구현 방법" in p]


async def test_v2_draft_fills_seed_and_flags_review() -> None:
    fake = FakeBackend().push(Completion(json.dumps(_v2draft(), ensure_ascii=False)))
    res = await draft_feature_task(_seed_ctx(), llm=_client(fake), prompts=_V2())  # type: ignore[arg-type]
    assert isinstance(res.output, DraftFeatureOutput) and res.flags == []
    assert res.provenance[0].prompt == "draft_feature_task@2"
    assert res.incident_id == BACKEND.incident_id and res.seed is BACKEND.feature
    task = apply_feature(_skeleton(), res)
    assert task.traps[0].trap_id == "t1" and len(task.accepted_designs) == 3
    assert FLAG_SEED_UNREVIEWED in task.review_flags  # 재료 검토 전
    assert FLAG_NO_STARTER not in task.review_flags  # 백엔드는 배송 감사 시작 코드가 있음


async def test_v2_without_seed_returns_skeleton_flag() -> None:
    fake = FakeBackend()
    res = await draft_feature_task(_ctx(), llm=_client(fake), prompts=_V2())  # type: ignore[arg-type]
    assert res.output is None and FLAG_NO_SEED in res.flags and fake.calls == []


def test_feature_stage1_needs_only_two_distractors() -> None:
    d = _v2draft()  # 원래: 핵심 1 · 보조 1 · 방해 4
    for i in (3, 4):
        d["artifacts"][i]["role"] = "key"  # 핵심 3 · 보조 1 · 방해 2
    d["max_requests"] = 3
    out = fill_from_seed(DraftFeatureOutputV2.model_validate(d), BACKEND.feature)  # type: ignore[arg-type]
    assert not [p for p in check_feature(out, _seed_ctx()) if "방해(distractor)" in p]
    d["artifacts"][5]["role"] = "key"  # 방해 1 → 걸림
    out = fill_from_seed(DraftFeatureOutputV2.model_validate(d), BACKEND.feature)  # type: ignore[arg-type]
    assert [p for p in check_feature(out, _seed_ctx()) if "방해(distractor) 2개" in p]


async def test_node_drafts_feature_v2_with_seed_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """승인자가 기능형 지정 → CURRENT(v2) 로 재료를 넣어 서술, 판 키에 재료 지문."""
    from task_spec.nodes import spec as spec_mod
    from task_spec.spec.build import CommonDraft, build_common
    from task_spec.spec.store import InMemoryCommonStore
    from tests.spec.test_task_kind_flow import _decision, _state

    async def fake_build(*args: Any, **kw: Any) -> CommonDraft:
        built = await build_common(*args, **kw)
        return CommonDraft(task=built.task, ctx=_seed_ctx(), flags=built.flags)

    monkeypatch.setattr(spec_mod, "build_common", fake_build)
    fake = FakeBackend().push(Completion(json.dumps(_v2draft(), ensure_ascii=False)))
    draft = spec_mod.make_draft_spec(
        None, llm=_client(fake), prompts=_REG, store=InMemoryCommonStore()
    )
    revise = _decision(
        "revise", comment="기능 개발형으로", task_kind="feature", revise_target="common"
    )
    spec = (await draft(_state("run-a", approval=revise)))["spec"]
    task = spec.common_tasks[0]
    assert task.task_kind == "feature" and task.traps[0].trap_id == "t1"
    assert spec.common_refs[0].prompt_ref == f"draft_feature_task@2+{feature_ref(BACKEND)}"
    assert FLAG_SEED_UNREVIEWED in task.review_flags


def test_current_feature_prompt_is_v2() -> None:
    assert _REG.get("draft_feature_task").ref == "draft_feature_task@2"


def test_stage1_must_not_hold_post_deploy_key_evidence() -> None:
    from task_spec.spec.feature import stage2_kinds_in_stage1

    fe = INCIDENTS["sw.frontend"].feature
    assert fe is not None
    titles = [
        "브라우저 로컬 저장소 무결성 점검 결과",
        "서버 API 응답 로그(정상)",
        "사용자 환경 통계",
    ]
    # 핵심 증거(첫 항목)와 같은 종류만 — 서버 응답 로그는 배포 전에도 있는 방해 자료라 괜찮다
    assert stage2_kinds_in_stage1(titles, fe.stage2_evidence, fe.discoverable_by) == [
        "브라우저 로컬 저장소 무결성 점검 결과"
    ]
    # 재료가 1단계 단서로도 적어 둔 종류는 1단계에 있어도 된다
    assert (
        stage2_kinds_in_stage1(
            ["커넥션 풀 상태 로그"], ["커넥션 풀 상태 로그"], ["커넥션 풀 상태 로그"]
        )
        == []
    )
    d = _v2draft()
    d["artifacts"][1]["title"] = (
        "커넥션 풀 상태 로그"  # 백엔드 핵심 증거와 같은 종류 (1단계 단서엔 '평시 커넥션 풀 대시보드')
    )
    out = fill_from_seed(DraftFeatureOutputV2.model_validate(d), BACKEND.feature)  # type: ignore[arg-type]
    assert [p for p in check_feature(out, _seed_ctx()) if "배포 뒤(2단계) 증거" in p]
