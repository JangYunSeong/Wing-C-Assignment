"""재료 입구 분석 (#301 PR 3, interview/material_analysis) — 표시 규칙·재료 블록·인터뷰 과제 만들기 연결. 가상 데이터·가짜 LLM 만.

분석은 승인을 막지 않는다 — 재현 가능이 아니거나 이름·사유가 어긋날 때만 과제 검토 표시로 남는다. 프롬프트에 CURRENT 가 없으면(평가 전)
부르지 않는다 (기존 인터뷰 테스트는 CURRENT 없이 돈다).
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from task_spec.api.app import create_app
from task_spec.interview.material_analysis import FAILED, FLAG, TASK, analysis_flags, material_block
from task_spec.llm.backend import Completion
from task_spec.prompting import PromptRegistry
from task_spec.schemas import FeatureSeed, MaterialAnalysis
from task_spec.spec.store import InMemoryCommonStore
from task_spec.starter.templates import load_template
from tests.api.conftest import make_settings
from tests.interview.conftest import FACTS, REPO, Session, make_service, prompts_with_current
from tests.interview.test_task import (
    SELECTED,
    FakeTaskMaker,
    confirmed,
    feature_seed_output,
    known_incident,
)

JAVA = load_template("backend/java-spring-boot3-jpa")
SEED = FeatureSeed.model_validate_json(feature_seed_output())


def _analysis(**over: Any) -> MaterialAnalysis:
    base: dict[str, Any] = {
        "mechanism": "요청마다 연결을 쥔 채 두 번째 연결을 기다려 동시 요청이 풀보다 많으면 모두 멈춤",
        "check": "동시 요청 ≥ 풀 크기면 모든 연결이 첫 연결로 잡혀 두 번째를 기다린다",
        "reason": "pool_acquire_timeout",
        "split_params": ["pool_size", "concurrency"],
        "reproducible": "yes",
        "gaps": [],
    }
    return MaterialAnalysis.model_validate(base | over)


# ── 표시 규칙 ───────────────────────────────────────────────────────


def test_a_reproducible_analysis_that_agrees_with_the_material_adds_no_flag() -> None:
    assert analysis_flags(_analysis(), JAVA, SEED) == []
    assert analysis_flags(_analysis(), JAVA, None) == []
    own = SEED.model_copy(update={"reasons": ["pool_acquire_timeout"]})
    assert analysis_flags(_analysis(), JAVA, own) == []


def test_a_material_that_may_not_reproduce_is_flagged_with_what_is_missing() -> None:
    flags = analysis_flags(
        _analysis(reproducible="no", split_params=[], gaps=["요청 하나의 DB 문장 수"]), JAVA, SEED
    )
    assert flags == [
        f"{FLAG}빌드 하네스로 재현되지 않을 수 있음 — 요청마다 연결을 쥔 채 두 번째 연결을 기다려 동시 요청이 풀보다 많으면 "
        "모두 멈춤 (사유 pool_acquire_timeout, 갈림 조절값 없음) · 빠진 정보: 요청 하나의 DB 문장 수"
    ]
    unsure = analysis_flags(_analysis(reproducible="unsure"), JAVA, SEED)
    assert len(unsure) == 1 and "재현 여부 판단 불가" in unsure[0]


def test_names_are_checked_against_the_template_and_the_material_reasons() -> None:
    """이름 대조는 코드가 한다 — 템플릿에 없는 사유·조절값, 재료가 고른 사유와 다른 사유."""
    bad = analysis_flags(
        _analysis(reason="pool_timeout", split_params=["pool", "concurrency"]), JAVA, SEED
    )
    assert bad == [f"{FLAG}템플릿에 없는 이름 pool, pool_timeout — 분석 결과 확인 필요"]
    own = SEED.model_copy(update={"reasons": ["lost_update"]})
    other = analysis_flags(_analysis(), JAVA, own)
    assert (
        len(other) == 1
        and "재료 사유(lost_update)와 분석 사유(pool_acquire_timeout) 불일치" in other[0]
    )


def test_the_material_block_carries_the_mechanism_not_who_or_where() -> None:
    incident = known_incident()
    block = material_block(incident)
    assert {"naive_impl", "why_naive_breaks", "accepted_designs", "reasons", "harness"} <= set(
        block
    )
    assert not {"title", "org", "url", "known_incident_cases", "incident_id"} & set(block)


# ── 인터뷰 과제 만들기 연결 ─────────────────────────────────────────


@pytest.fixture
async def analysis_session(
    tmp_path: Path, detectors: Any
) -> AsyncIterator[tuple[Session, FakeTaskMaker]]:
    """분석 프롬프트를 CURRENT 로 둔 인터뷰 서비스 (레포는 평가 전이라 CURRENT 가 없다)."""
    prompts_with_current(tmp_path)
    dst = tmp_path / "prompts" / TASK
    dst.mkdir()
    versions = sorted((REPO / "prompts" / TASK).glob("v*.md"))
    for f in versions:
        shutil.copy(f, dst / f.name)
    (dst / "CURRENT").write_text(f"v{max(int(f.stem[1:]) for f in versions)}\n")
    maker = FakeTaskMaker()
    service, backend, store, postings, clock = make_service(
        tmp_path,
        detectors,
        prompts=PromptRegistry(tmp_path / "prompts"),
        tasks=maker,
        common_store=InMemoryCommonStore(),
    )
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Session(client, backend, service, store, postings, tmp_path, clock), maker


async def _generate(session: Session, *analysis: Completion | Exception) -> dict[str, Any]:
    await confirmed(session)
    assert (await session.post("competencies", competency_ids=SELECTED)).status_code == 200
    session.backend.push(Completion(feature_seed_output()), *analysis)
    response = await session.generate(competency_ids=SELECTED)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stage"] == "task_review", body
    return dict(body["spec"])


async def test_generation_flags_a_material_that_may_not_reproduce(analysis_session: Any) -> None:
    session, _ = analysis_session
    calls = len(session.backend.prompts)
    analysis = _analysis(reproducible="no", gaps=["요청 하나의 DB 문장 수"])
    spec = await _generate(session, Completion(analysis.model_dump_json()))
    assert any(f.startswith(FLAG) and "재현되지 않을 수 있음" in f for f in spec["review_flags"])
    sent = session.backend.prompts[-1]  # 재료 분석 — 가명화한 재료 블록만 간다
    assert "재료:" in sent and FACTS["naive_fix"] in sent and "가상테크" not in sent
    assert len(session.backend.prompts) > calls
    assert any(p["prompt"].startswith(f"{TASK}@") for p in spec["generation_provenance"])


async def test_a_reproducible_material_adds_no_flag(analysis_session: Any) -> None:
    session, _ = analysis_session
    spec = await _generate(session, Completion(_analysis().model_dump_json()))
    assert not any(f.startswith(FLAG) for f in spec["review_flags"])


async def test_a_failed_analysis_does_not_stop_the_task(analysis_session: Any) -> None:
    """분석이 실패해도 과제는 만든다 — 실패를 표시로 남겨 빌드에서 확인하게."""
    session, _ = analysis_session
    spec = await _generate(session, ValueError("가상 분석 실패"))
    assert FAILED in spec["review_flags"]
