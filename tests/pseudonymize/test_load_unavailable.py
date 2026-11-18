"""가명화본을 읽을 수 없을 때 (#246) — 없는 파일·읽기 실패·UTF-8 아님도 PseudonymizedNotAvailable 하나로.

주장 텍스트를 가명화본에서 다시 만드는 세 곳(retrieve_t1 · verify_overlap · draft_spec 의 면접 질문)이 같은 규칙으로
그 주장만 빼고 claim_not_pseudonymized 를 남긴다 — 예전에는 파일이 없으면 retrieve_t1·draft_spec 만
FileNotFoundError 로 실행이 죽었다. LLM 에는 아무것도 가지 않는다. 가상 데이터만.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from task_spec.llm import FakeBackend, LLMClient
from task_spec.nodes import spec as spec_mod
from task_spec.nodes.pseudonymize import PseudonymizedNotAvailable, load_pseudonymized
from task_spec.nodes.retrieve import make_retrieve_t1
from task_spec.nodes.verify_overlap import make_verify_overlap
from task_spec.retrieve.t1 import T1Retriever
from task_spec.schemas import (
    ApprovalDecision,
    Comparison,
    PseudoCheck,
    PseudoDocRef,
    RunState,
    TaskItem,
    VerifyTarget,
)
from task_spec.spec.build import CommonDraft, build_common
from task_spec.spec.questions import FLAG_Q_FAILED, build_slot_verifications
from tests.graph.test_verify_overlap import PROMPTS
from tests.graph.test_verify_overlap import _state as _claims_state
from tests.retrieve.fixtures import cardset
from tests.spec.test_draft import _ctx
from tests.spec.test_task_kind_flow import _state as _posting_state

ALL = ["d1-c1", "d1-c2", "d1-c3"]
TO_VERIFY = Comparison(
    claims_to_verify=[
        VerifyTarget(claim_id="d1-c1", competency_ids=["comp:x"], score=5.0),
        VerifyTarget(claim_id="d1-c2", competency_ids=["comp:y"], score=4.0),
    ]
)


def _ref(path: Path, data: bytes) -> PseudoDocRef:
    return PseudoDocRef(
        doc_id="d1",
        kind="resume",
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        check=PseudoCheck(passed=True, residual_count=0),
    )


def _gone(tmp_path: Path) -> RunState:
    """경로·해시 모양은 맞고 파일만 없는 실행 (보관 기간이 지나 지운 가명화본처럼)."""
    state = _claims_state(tmp_path)
    Path(state.pseudo_docs[0].path or "").unlink()
    return state


def _skipped(errors: list[Any]) -> list[tuple[str, str | None]]:
    return [(e.code, e.ref_id) for e in errors if e.code == "claim_not_pseudonymized"]


def test_missing_unreadable_or_non_utf8_file_is_not_available(tmp_path: Path) -> None:
    path = tmp_path / "pseudonymized" / "r1" / "d1.txt"
    path.parent.mkdir(parents=True)
    with pytest.raises(PseudonymizedNotAvailable):  # 없음 (예전: FileNotFoundError)
        load_pseudonymized(_ref(path, b"x"))
    path.mkdir()
    with pytest.raises(PseudonymizedNotAvailable):  # 읽을 수 없음 (파일 자리에 디렉터리)
        load_pseudonymized(_ref(path, b"x"))
    path.rmdir()
    bad = b"\xff\xfe [PERSON_1] \xc3"
    path.write_bytes(bad)
    with pytest.raises(PseudonymizedNotAvailable):  # 해시는 맞지만 UTF-8 아님
        load_pseudonymized(_ref(path, bad))


async def test_retrieve_t1_skips_claims_without_pseudonymized_text(tmp_path: Path) -> None:
    cards, m = cardset()
    fake = FakeBackend()  # 불리면 '준비된 응답 없음' 으로 실패한다
    node = make_retrieve_t1(T1Retriever(cards, m), LLMClient({"opencode": fake}), PROMPTS)
    out = await node(_gone(tmp_path))
    assert _skipped(out["errors"]) == [("claim_not_pseudonymized", c) for c in ALL]
    assert out["candidates"] == [] and fake.calls == []


async def test_verify_overlap_keeps_the_same_rule(tmp_path: Path) -> None:
    fake = FakeBackend()
    out = await make_verify_overlap(LLMClient({"opencode": fake}), PROMPTS)(_gone(tmp_path))
    assert out["overlap_check"] is None
    assert _skipped(out["errors"]) == [("claim_not_pseudonymized", c) for c in ALL]
    assert fake.calls == []


async def test_interview_questions_skip_claims_without_pseudonymized_text(tmp_path: Path) -> None:
    state = _gone(tmp_path).model_copy(update={"comparison": TO_VERIFY})
    common = TaskItem(
        task_id="ct-1",
        track="common",
        target_competencies=_ctx().targets,
        tech_env=_ctx().env,
        difficulty_target="high",
        time_limit_min=60,
    )
    fake = FakeBackend()
    assignments, items, prov, errors = await build_slot_verifications(
        state, common, spec_id="r1-r2", llm=LLMClient({"opencode": fake}), prompts=PROMPTS
    )
    assert _skipped(errors) == [("claim_not_pseudonymized", c) for c in ("d1-c1", "d1-c2")]
    assert [a.claim_id for a in assignments] == ["d1-c1", "d1-c2"]
    assert all(FLAG_Q_FAILED in a.review_flags for a in assignments)  # 면접관이 주장을 직접 본다
    assert items == [] and prov == [] and fake.calls == []


async def test_draft_spec_verification_revise_survives_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """이슈 재현 경로: 오래 승인 대기한 실행의 가명화본이 지워진 뒤 경험 검증만 수정 요청."""

    async def fake_build(*args: Any, **kw: Any) -> CommonDraft:
        built = await build_common(*args, **kw)
        return CommonDraft(task=built.task, ctx=_ctx(), flags=built.flags)

    monkeypatch.setattr(spec_mod, "build_common", fake_build)
    gone = _gone(tmp_path)
    state = _posting_state(
        "r1", pseudo_docs=gone.pseudo_docs, claims=gone.claims, comparison=TO_VERIFY
    )
    s1 = (await spec_mod.make_draft_spec(None)(state))["spec"]
    revise = ApprovalDecision(
        status="revise",
        reviewer="reviewer-fake",
        comment="면접 질문을 다시",
        revise_target="verification",
        at=datetime(2026, 11, 18, tzinfo=UTC),
    )
    fake = FakeBackend()
    node = spec_mod.make_draft_spec(None, llm=LLMClient({"opencode": fake}), prompts=PROMPTS)
    out = await node(state.model_copy(update={"spec": s1, "approval": revise}))
    assert out["spec"].revision == 2
    assert _skipped(out["errors"]) == [("claim_not_pseudonymized", c) for c in ("d1-c1", "d1-c2")]
    assert [a.claim_id for a in out["spec"].slot_assignments] == ["d1-c1", "d1-c2"]
    assert fake.calls == []
