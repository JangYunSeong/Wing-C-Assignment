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
from task_spec.interview.material_analysis import (
    FAILED,
    FLAG,
    GAPS,
    TASK,
    analysis_flags,
    material_block,
)
from task_spec.llm.backend import Completion
from task_spec.prompting import PromptRegistry
from task_spec.schemas import FeatureSeed, MaterialAnalysis, PublicIncident
from task_spec.spec.company_incidents import LLM_FIELDS, InMemoryCompanyIncidents
from task_spec.spec.incidents import load_incidents
from task_spec.spec.store import InMemoryCommonStore
from task_spec.starter.templates import load_template
from tests.api.conftest import make_settings
from tests.interview.conftest import FACTS, REPO, Session, make_service, prompts_with_current
from tests.interview.test_sources import PUBLIC_DEFAULT, Env, choose, company_case
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
        f"{FLAG}빌드 하네스로 재현 불가 가능성 있음 — 요청마다 연결을 쥔 채 두 번째 연결을 기다려 동시 요청이 풀보다 많으면 "
        "모두 멈춤 (사유 pool_acquire_timeout, 갈림 조절값 없음) · 빠진 정보: 요청 하나의 DB 문장 수"
    ]
    unsure = analysis_flags(_analysis(reproducible="unsure"), JAVA, SEED)
    assert len(unsure) == 1 and "재현 여부 판단 불가" in unsure[0]


def test_gaps_show_even_when_the_material_is_judged_reproducible() -> None:
    """#308 팩트체크: 세션 유지 재료 분석이 gaps 에 'SELECT·UPDATE 가 커넥션 하나를 쥐는가'를 냈는데 판정이 yes 라 숨었다 — 떼어 둔
    결제 대행 표본도 같다. yes 여도 빠진 정보가 있으면 기전과 함께 표시한다 (판정 표시와 머리 GAPS 로 갈라 센다)."""
    gaps = ["SELECT·UPDATE 가 커넥션 하나를 쥐는가", "주입 지연이 지배적인가"]
    flags = analysis_flags(_analysis(gaps=gaps), JAVA, SEED)
    assert flags == [
        f"{GAPS} — 요청마다 연결을 쥔 채 두 번째 연결을 기다려 동시 요청이 풀보다 많으면 모두 멈춤 "
        "(사유 pool_acquire_timeout, 갈림 조절값 pool_size, concurrency) · 빠진 정보: "
        "SELECT·UPDATE 가 커넥션 하나를 쥐는가; 주입 지연이 지배적인가"
    ]
    assert GAPS.startswith(FLAG)
    # 사유 불일치 표시와 함께 나온다 — 둘 다 보인다
    own = SEED.model_copy(update={"reasons": ["lost_update"]})
    both = analysis_flags(_analysis(gaps=gaps), JAVA, own)
    assert len(both) == 2 and both[0].startswith(GAPS) and "불일치" in both[1]


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


def test_a_shared_block_holds_only_what_the_registration_rule_sends() -> None:
    """공유 회사 사례 입력(shared=True)은 등록 규칙이 LLM 입력으로 승인한 칸만 — 위 칸은 LLM_FIELDS 안, 아래 칸은 LLM_FIELDS 의
    feature 하위 (등록 검사 scan_incident 가 feature 하위 전체를 본다). notes 는 LLM 에 보내지 않는 칸이다 (#308).
    재료 블록에 칸을 더하면 여기서 등록 규칙과 맞는지 다시 본다."""
    block = material_block(known_incident(), shared=True)
    assert "feature" in LLM_FIELDS and "notes" not in block
    assert all(k in LLM_FIELDS or k in FeatureSeed.model_fields for k in block), sorted(block)
    assert "notes" in material_block(known_incident())  # 인터뷰로 새로 만든 재료는 그대로


# ── 인터뷰 과제 만들기 연결 ─────────────────────────────────────────


def _analysis_prompts(tmp_path: Path) -> PromptRegistry:
    """인터뷰 프롬프트 + 분석 프롬프트를 CURRENT 로 (테스트 기본 프롬프트에는 분석이 없어 기존 테스트는 분석 없이 돈다)."""
    prompts_with_current(tmp_path)
    dst = tmp_path / "prompts" / TASK
    dst.mkdir()
    versions = sorted((REPO / "prompts" / TASK).glob("v*.md"))
    for f in versions:
        shutil.copy(f, dst / f.name)
    (dst / "CURRENT").write_text(f"v{max(int(f.stem[1:]) for f in versions)}\n")
    return PromptRegistry(tmp_path / "prompts")


@pytest.fixture
async def analysis_session(
    tmp_path: Path, detectors: Any
) -> AsyncIterator[tuple[Session, FakeTaskMaker]]:
    """분석 프롬프트를 CURRENT 로 둔 인터뷰 서비스."""
    maker = FakeTaskMaker()
    service, backend, store, postings, clock = make_service(
        tmp_path,
        detectors,
        prompts=_analysis_prompts(tmp_path),
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
    assert any(f.startswith(FLAG) and "재현 불가 가능성 있음" in f for f in spec["review_flags"])
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


# ── 공개·회사 사례 (#308) ───────────────────────────────────────────


@pytest.fixture
async def source_env(tmp_path: Path, detectors: Any) -> AsyncIterator[Env]:
    """분석 프롬프트를 CURRENT 로 둔 서비스 + 공개 사례·같은 회사의 공유 사례 (test_sources 의 env 와 같은 준비)."""
    common, incidents = InMemoryCommonStore(), InMemoryCompanyIncidents()
    service, backend, store, postings, clock = make_service(
        tmp_path,
        detectors,
        prompts=_analysis_prompts(tmp_path),
        tasks=FakeTaskMaker(),
        common_store=common,
        incidents=incidents,
    )
    service.public_incidents = load_incidents(REPO / "incidents" / "public_v1")
    service.company_incidents = incidents
    app = create_app(settings=make_settings(tmp_path))
    app.state.interviews = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Env(
            Session(client, backend, service, store, postings, tmp_path, clock), common, incidents
        )


async def _source_spec(
    env: Env, source: str, incident_id: str, *analysis: Completion | Exception
) -> tuple[dict[str, Any], list[str]]:
    """출처 고르기 → 확인 → 역량 → 과제 생성. (과제 명세, 생성 중 LLM 에 간 글들)."""
    s = env.session
    await s.start()
    chosen = await choose(s, source, incident_id)
    assert chosen.status_code == 200, chosen.text
    confirmed = await s.post("incident/confirm", incident=chosen.json()["incident_draft"])
    assert confirmed.status_code == 200, confirmed.text
    assert (await s.post("competencies", competency_ids=SELECTED)).status_code == 200
    calls = len(s.backend.prompts)
    s.backend.push(*analysis)
    generated = await s.generate(competency_ids=SELECTED)
    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert body["stage"] == "task_review", body
    return dict(body["spec"]), list(s.backend.prompts[calls:])


FOREIGN = "[PRODUCT_3]"  # 재료를 등록한 인터뷰의 토큰 — 이 인터뷰 대응표에는 없다


async def _shared_case(env: Env, suffix: str = "pool-a", **update: Any) -> PublicIncident:
    """다른 인터뷰에서 등록한 모양의 회사 재료 — LLM 입력 칸에 그 인터뷰의 토큰, notes 에 메모·링크 (가상)."""
    fields: dict[str, Any] = {
        "symptom": f"{FOREIGN} 주문 API 가 피크에 응답하지 않음",
        "notes": "가상 메모 — 수치 미확인, 참고 https://example.com/memo",
    }
    return await company_case(env, suffix, **(fields | update))


async def test_a_company_case_is_analyzed_with_its_own_tokens_and_without_notes(
    source_env: Env,
) -> None:
    """#308 4차: 회사 사례 분기는 재료 분석을 부르지 않아, #311 전에 등록한 회사 재료를 다시 쓰면 재현 의문이 보이지 않았다.
    재료는 그대로 쓰고(interview_feature_seed 없음 — LLM 은 분석 한 번) 표시만 남긴다. 등록한 인터뷰의 토큰은 그 재료에 있는
    토큰이라 통과하고, notes 는 등록 규칙상 LLM 입력 칸이 아니라 보내지 않는다. 회사·제목도 가지 않는다."""
    inc = await _shared_case(source_env)
    analysis = _analysis(reproducible="no", mechanism=f"{FOREIGN} 요청마다 연결을 쥔 채 기다림")
    spec, sent = await _source_spec(
        source_env, "company", inc.incident_id, Completion(analysis.model_dump_json())
    )
    assert len(sent) == 1 and "재료:" in sent[0] and FOREIGN in sent[0]
    assert "가상 메모" not in sent[0] and "example.com" not in sent[0]
    assert "가상테크" not in sent[0] and "가상 회사 사례" not in sent[0]
    # 표시가 재료의 토큰을 옮겨 써도 통과한다 — 분석 실패로 바뀌지 않는다
    assert any(f.startswith(FLAG) and FOREIGN in f for f in spec["review_flags"])
    assert FAILED not in spec["review_flags"]
    assert any(p["prompt"].startswith(f"{TASK}@") for p in spec["generation_provenance"])


async def test_a_company_case_analysis_that_invents_a_token_only_flags_a_failure(
    source_env: Env,
) -> None:
    """출력 허용 토큰은 재료에 있던 토큰과 이 인터뷰 토큰뿐 — 지어낸 토큰([PERSON_9])은 거절하고 고정 실패 문구만 남긴다."""
    inc = await _shared_case(source_env)
    analysis = _analysis(reproducible="no", mechanism="[PERSON_9] 가 연결을 쥔 채 기다림")
    spec, sent = await _source_spec(
        source_env, "company", inc.incident_id, Completion(analysis.model_dump_json())
    )
    assert len(sent) == 1
    assert FAILED in spec["review_flags"]
    assert not any("[PERSON_9]" in f for f in spec["review_flags"])


async def test_a_company_case_with_a_link_in_an_llm_field_is_not_sent(source_env: Env) -> None:
    """등록 규칙(LLM 입력 칸의 연락처·링크)으로 다시 본다 — 걸리면 보내지 않고 실패 문구만 남긴다 (과제는 만든다)."""
    inc = await _shared_case(
        source_env, root_cause="원인 설명은 https://example.com/postmortem 참고"
    )
    spec, sent = await _source_spec(source_env, "company", inc.incident_id)
    assert sent == [] and FAILED in spec["review_flags"]


async def test_a_public_case_is_not_analyzed_here(source_env: Env) -> None:
    """공개 재료는 서비스에서 보지 않는다 — 평가로 한 번 본다 (#301 결정 그대로)."""
    spec, sent = await _source_spec(source_env, "public_fixed", PUBLIC_DEFAULT)
    assert sent == [] and not any(f.startswith(FLAG) for f in spec["review_flags"])
