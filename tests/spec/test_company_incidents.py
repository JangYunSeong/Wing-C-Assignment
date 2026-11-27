"""회사 결정적 사건 [확정: 2026-11-19 사용자] — 저장소 규칙, 공고마다 사례 고르기, 명세 노드 연결. 가상 데이터만.

같은 회사에서 공유 승인된 사례만 그 회사 공고의 공통 과제에 공개 사례보다 먼저 쓴다. 공고마다 처음 정한 사례를 계속 쓴다
(공통 과제가 이미 있는 공고는 그 사례 그대로 — 같은 공고 지원자 비교 가능성, 불변식 3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from task_spec.nodes import spec as spec_mod
from task_spec.schemas import PublicIncident, TaskItem
from task_spec.spec.common import FLAG_COMPANY_INCIDENT, FLAG_PUBLIC_INCIDENT, incident_flag
from task_spec.spec.company_incidents import (
    FLAG_COMPANY_NO_FEATURE,
    FLAG_COMPANY_REUSED,
    FLAG_COMPANY_SWITCHED,
    CompanyIncident,
    InMemoryCompanyIncidents,
    feature_fit,
    pick_company_incident,
    scan_incident,
)
from task_spec.spec.incidents import IncidentCatalog, load_incidents
from task_spec.spec.store import CommonKey, InMemoryCommonStore
from tests.spec.test_task_kind_flow import _state

PUBLIC = Path(__file__).resolve().parents[2] / "incidents" / "public_v1"
SUB = "sw.backend"  # tests.spec.test_task_kind_flow 의 공고 직군
AT = datetime(2026, 11, 19, tzinfo=UTC)


def _public(name: str = "sw.backend.json") -> PublicIncident:
    return PublicIncident.model_validate_json((PUBLIC / name).read_text("utf-8"))


def _company_incident(suffix: str, *, feature: bool = True) -> PublicIncident:
    """공개 사례 내용을 빌린 가상 회사 사례 (id·출처 칸만 회사 사례로)."""
    data = _public().model_dump(mode="json")
    data.update(incident_id=f"co:{SUB}:{suffix}", org="", url="", verified=False)
    if not feature:
        data.update(feature=None, templates=[])
    return PublicIncident.model_validate(data)


async def _shared(
    store: InMemoryCompanyIncidents, suffix: str, *, company: str = "가상회사", feature: bool = True
) -> CompanyIncident:
    rec = await store.submit(
        company,
        _company_incident(suffix, feature=feature),
        source={"kind": "manual"},
        created_by="담당자-가상",
    )
    assert await store.decide(
        rec.incident_id, rec.content_sha256, "shared", "승인자-가상", AT, None
    )
    got = await store.get(rec.incident_id)
    assert got is not None and got.status == "shared"
    return got


def _task_for(source: str | None) -> TaskItem:
    return TaskItem.model_validate(
        {
            "task_id": "tmp",
            "track": "common",
            "target_competencies": [{"competency_id": "comp:a", "label": "가상 역량"}],
            "tech_env": {"basis": "공고 언어"},
            "difficulty_target": "medium",
            "time_limit_min": 60,
            "source_incident": source,
        }
    )


async def _used_by(
    common: InMemoryCommonStore, posting: str, incident_id: str, *, at: datetime = AT
) -> None:
    """그 공고에 그 사례로 만든 공통 과제가 승인돼 있다."""
    row = await common.save_draft(
        CommonKey(posting, "onto-v", f"draft_task@3#{incident_id}"), _task_for(incident_id), []
    )
    await common.approve(row.id, "승인자-가상", at)


# ── 저장소 ─────────────────────────────────────────────────────────────────


async def test_submit_validates_id_and_company() -> None:
    store = InMemoryCompanyIncidents()
    with pytest.raises(ValueError, match="co:"):
        await store.submit("가상회사", _public(), source={}, created_by="x")
    other_sub = _company_incident("aaaa00000001").model_copy(update={"sub": "sw.frontend"})
    with pytest.raises(ValueError, match="직군"):
        await store.submit("가상회사", other_sub, source={}, created_by="x")
    with pytest.raises(ValueError, match="회사"):
        await store.submit(" ", _company_incident("aaaa00000002"), source={}, created_by="x")
    rec = await store.submit(
        "가상회사", _company_incident("aaaa00000003"), source={}, created_by="x"
    )
    assert rec.status == "pending" and len(rec.content_sha256) == 64
    with pytest.raises(ValueError, match="이미"):
        await store.submit("가상회사", _company_incident("aaaa00000003"), source={}, created_by="x")


async def test_decide_only_pending_with_same_content_and_retire() -> None:
    store = InMemoryCompanyIncidents()
    rec = await store.submit(
        "가상회사", _company_incident("bbbb00000001"), source={}, created_by="x"
    )
    assert not await store.decide(rec.incident_id, "0" * 64, "shared", "r", AT, None)  # 다른 내용
    assert await store.decide(rec.incident_id, rec.content_sha256, "shared", "r", AT, None)
    assert not await store.decide(rec.incident_id, rec.content_sha256, "rejected", "r", AT, "늦음")
    assert await store.retire(rec.incident_id, "r", AT, "밖으로 샘")
    assert not await store.retire(rec.incident_id, "r", AT, "두 번")
    got = await store.get(rec.incident_id)
    assert got is not None and (got.status, got.retire_reason) == ("retired", "밖으로 샘")


async def test_shared_is_per_company_and_sub() -> None:
    store = InMemoryCompanyIncidents()
    mine = await _shared(store, "cccc00000001")
    await _shared(store, "cccc00000002", company="다른회사")
    await store.submit("가상회사", _company_incident("cccc00000003"), source={}, created_by="x")
    assert [r.incident_id for r in await store.shared("가상회사", SUB)] == [mine.incident_id]
    assert await store.shared("가상회사", "sw.frontend") == []
    assert len(await store.find(company_id="가상회사")) == 2
    assert len(await store.find(status="shared")) == 2


def test_scan_counts_by_field_and_category_without_values() -> None:
    inc = _company_incident("dddd00000001").model_copy(
        update={"symptom": "가상 증상 A", "root_cause": "가상 원인 B"}
    )

    def detect(text: str) -> list[str]:
        return {"가상 증상 A": ["PERSON", "PERSON"], "가상 원인 B": ["CONTACT"]}.get(text, [])

    found = scan_incident(inc, detect)
    assert [(f.field, f.category, f.count) for f in found] == [
        ("symptom", "PERSON", 2),
        ("root_cause", "CONTACT", 1),
    ]
    assert found[0].text() == "symptom: 사람 이름으로 보이는 표현 2곳"


def test_incident_flag_tells_company_from_public() -> None:
    assert incident_flag("co:sw.backend:abc") == FLAG_COMPANY_INCIDENT
    assert incident_flag("pub:sw.backend:abc") == FLAG_PUBLIC_INCIDENT


# ── 공고마다 고르기 ───────────────────────────────────────────────────────────


async def test_new_posting_gets_least_used_and_flag_when_reused() -> None:
    store, common = InMemoryCompanyIncidents(), InMemoryCommonStore()
    a = await _shared(store, "eeee00000001")
    b = await _shared(store, "eeee00000002")
    await _used_by(common, "다른공고-1", a.incident_id)
    pick = await pick_company_incident(
        [a, b], kind="incident", sub=SUB, posting_id="p-new", prev=[], common=common
    )
    assert pick.incident is not None and pick.incident.incident_id == b.incident_id
    assert pick.flags == []
    await _used_by(common, "다른공고-2", b.incident_id)
    await _used_by(common, "다른공고-3", b.incident_id)
    again = await pick_company_incident(
        [a, b], kind="incident", sub=SUB, posting_id="p-new2", prev=[], common=common
    )
    assert again.incident is not None and again.incident.incident_id == a.incident_id
    assert again.flags == [FLAG_COMPANY_REUSED.format(id=a.incident_id, n=1)]


async def test_posting_keeps_its_company_incident_even_if_others_are_less_used() -> None:
    store, common = InMemoryCompanyIncidents(), InMemoryCommonStore()
    a = await _shared(store, "ffff00000001")
    b = await _shared(store, "ffff00000002")
    await _used_by(common, "다른공고-1", a.incident_id)
    pick = await pick_company_incident(
        [a, b], kind="incident", sub=SUB, posting_id="p", prev=[a.incident_id], common=common
    )
    assert pick.incident is not None and pick.incident.incident_id == a.incident_id
    assert pick.flags == [FLAG_COMPANY_REUSED.format(id=a.incident_id, n=1)]


async def test_retired_incident_switches_with_flag_and_public_start_is_kept() -> None:
    store, common = InMemoryCompanyIncidents(), InMemoryCommonStore()
    b = await _shared(store, "gggg00000002")
    gone = "co:sw.backend:gggg00000001"  # 보관돼 shared 목록에 없다
    pick = await pick_company_incident(
        [b], kind="incident", sub=SUB, posting_id="p", prev=[gone], common=common
    )
    assert pick.incident is not None and pick.incident.incident_id == b.incident_id
    assert pick.flags == [FLAG_COMPANY_SWITCHED.format(id=gone)]
    kept = await pick_company_incident(
        [b], kind="incident", sub=SUB, posting_id="p", prev=["pub:sw.backend:x"], common=common
    )
    assert kept.incident is None and kept.flags == []


async def test_feature_task_needs_feature_fit_company_incident() -> None:
    store, common = InMemoryCompanyIncidents(), InMemoryCommonStore()
    plain = await _shared(store, "hhhh00000001", feature=False)
    assert not feature_fit(plain.incident, SUB)
    pick = await pick_company_incident(
        [plain], kind="feature", sub=SUB, posting_id="p", prev=[], common=common
    )
    assert pick.incident is None and pick.flags == [FLAG_COMPANY_NO_FEATURE]
    seeded = await _shared(store, "hhhh00000002")
    assert feature_fit(seeded.incident, SUB)
    pick = await pick_company_incident(
        [plain, seeded], kind="feature", sub=SUB, posting_id="p", prev=[], common=common
    )
    assert pick.incident is not None and pick.incident.incident_id == seeded.incident_id


# ── 명세 노드 연결 ────────────────────────────────────────────────────────────


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """draft_spec 이 build_common 에 넘긴 사례 카탈로그를 기록한다 (진짜 build_common 그대로 부름)."""
    calls: list[dict[str, Any]] = []
    real = spec_mod.build_common

    async def spy(*args: Any, **kw: Any) -> Any:
        calls.append(kw)
        return await real(*args, **kw)

    monkeypatch.setattr(spec_mod, "build_common", spy)
    return calls


def _node(
    common: InMemoryCommonStore,
    store: InMemoryCompanyIncidents,
    catalog: IncidentCatalog,
    company: str | None = "가상회사",
) -> Any:
    async def company_of(posting_id: str) -> str | None:
        return company

    return spec_mod.make_draft_spec(
        None,
        store=common,
        incidents=catalog,
        company_incidents=store,
        company_of=company_of,
    )


async def test_draft_uses_company_incident_for_new_posting(seen: list[dict[str, Any]]) -> None:
    store, common, catalog = (
        InMemoryCompanyIncidents(),
        InMemoryCommonStore(),
        load_incidents(PUBLIC),
    )
    rec = await _shared(store, "iiii00000001")
    await _node(common, store, catalog)(_state("run-a"))
    used = seen[-1]["incidents"]
    assert used[SUB].incident_id == rec.incident_id
    assert used["sw.frontend"] is catalog["sw.frontend"]  # 다른 직군은 공개 사례 그대로


async def test_draft_never_uses_other_company_incidents(seen: list[dict[str, Any]]) -> None:
    store, common, catalog = (
        InMemoryCompanyIncidents(),
        InMemoryCommonStore(),
        load_incidents(PUBLIC),
    )
    await _shared(store, "jjjj00000001", company="다른회사")
    await _node(common, store, catalog)(_state("run-a"))
    assert seen[-1]["incidents"] is catalog
    await _node(common, store, catalog, company=None)(_state("run-b"))  # 회사를 모르는 공고
    assert seen[-1]["incidents"] is catalog


async def test_draft_keeps_public_incident_of_posting_that_already_has_a_task(
    seen: list[dict[str, Any]],
) -> None:
    """회사 사례가 나중에 공유돼도, 공개 사례로 공통 과제를 만든 공고는 바꾸지 않는다 — 같은 공고 지원자 비교 가능성."""
    store, common, catalog = (
        InMemoryCompanyIncidents(),
        InMemoryCommonStore(),
        load_incidents(PUBLIC),
    )
    await _used_by(common, "p-backend", catalog[SUB].incident_id)
    await _shared(store, "kkkk00000001")
    out = await _node(common, store, catalog)(_state("run-a"))
    assert seen[-1]["incidents"] is catalog
    assert not any("회사 사례" in f for f in out["spec"].review_flags)


# ── 리뷰 반영: 보관 뒤 고정, 공유 사례가 다 사라질 때, 과제 유지, 검사 칸, 순서 ──────────────────


async def test_replacement_after_retirement_stays_fixed() -> None:
    """보관된 X 를 Y 로 바꾼 공고는 다음 실행에도 Y — 덜 쓴 Z 가 생겨도 다시 바꾸지 않는다."""
    store, common = InMemoryCompanyIncidents(), InMemoryCommonStore()
    y = await _shared(store, "llll00000002")
    z = await _shared(store, "llll00000003")
    await _used_by(common, "다른공고-1", y.incident_id)
    gone = "co:sw.backend:llll00000001"
    pick = await pick_company_incident(
        [y, z], kind="incident", sub=SUB, posting_id="p", prev=[gone, y.incident_id], common=common
    )
    assert pick.incident is not None and pick.incident.incident_id == y.incident_id
    assert pick.flags[0] == FLAG_COMPANY_SWITCHED.format(id=gone)
    moved = await pick_company_incident(
        [y, z],
        kind="incident",
        sub=SUB,
        posting_id="p",
        prev=[gone, "pub:sw.backend:x"],
        common=common,
    )
    assert moved.incident is None  # 이미 공개 사례로 옮겨 간 공고 — 다시 바꾸지 않는다
    assert moved.flags == [FLAG_COMPANY_SWITCHED.format(id=gone)]


async def test_draft_flags_switch_when_all_shared_incidents_are_gone(
    seen: list[dict[str, Any]],
) -> None:
    """회사의 공유 사례가 모두 보관되면 그 사례를 쓰던 공고는 공개 사례로 — 말없이 바뀌지 않게 표시 (리뷰 W1)."""
    store, common, catalog = (
        InMemoryCompanyIncidents(),
        InMemoryCommonStore(),
        load_incidents(PUBLIC),
    )
    rec = await _shared(store, "mmmm00000001")
    await _used_by(common, "p-backend", rec.incident_id)
    assert await store.retire(rec.incident_id, "r", AT, "밖으로 샘")
    out = await _node(common, store, catalog)(_state("run-a"))
    assert seen[-1]["incidents"] is catalog
    assert FLAG_COMPANY_SWITCHED.format(id=rec.incident_id) in out["spec"].review_flags


async def test_verification_revise_keeps_task_and_only_recounts_reuse(
    seen: list[dict[str, Any]],
) -> None:
    """경험 검증만 다시 만들면 공통 과제를 유지한다 — 사례를 다시 고르지 않고, 그 과제 사례의 재사용 표시만 다시 센다 (리뷰 W3)."""
    from task_spec.schemas import ApprovalDecision

    store, common, catalog = (
        InMemoryCompanyIncidents(),
        InMemoryCommonStore(),
        load_incidents(PUBLIC),
    )
    a = await _shared(store, "nnnn00000001")
    node = _node(common, store, catalog)
    s1 = (await node(_state("run-a")))["spec"]
    task = s1.common_tasks[0].model_copy(update={"source_incident": a.incident_id})
    s1 = s1.model_copy(update={"common_tasks": [task]})
    await _used_by(common, "다른공고-1", a.incident_id)
    retired_other = await _shared(store, "nnnn00000002")
    assert await store.retire(retired_other.incident_id, "r", AT, "x")
    revise = ApprovalDecision(
        status="revise",
        reviewer="r",
        comment="면접 질문을 더 구체적으로",
        revise_target="verification",
        at=AT,
    )
    calls = len(seen)
    out = await node(_state("run-a", spec=s1, approval=revise))
    assert seen[calls]["incidents"] is catalog  # 고르지 않았다
    assert out["spec"].common_tasks == s1.common_tasks
    flags = out["spec"].review_flags
    assert FLAG_COMPANY_REUSED.format(id=a.incident_id, n=1) in flags
    assert not any(f.startswith("이 공고가 쓰던 회사 사례") for f in flags)


def test_scan_blocks_only_llm_fields_and_reads_feature_seed() -> None:
    inc = _company_incident("oooo00000001")
    assert inc.feature is not None

    def detect(text: str) -> list[str]:
        return ["LINK"] if text.strip() else []

    found = {f.field: f for f in scan_incident(inc, detect)}
    assert "feature" in found and found["feature"].blocks  # 계약 생성 LLM 입력
    assert found["symptom"].blocks
    literal = _company_incident("oooo00000002").model_copy(update={"source_literals": ["log line"]})
    found = {f.field: f for f in scan_incident(literal, detect)}
    assert not found["source_literals"].blocks  # LLM 에 보내지 않는 칸 — 표시만


async def test_posting_incidents_order_approved_latest_first_then_drafts() -> None:
    """승인된 과제가 있는 사례(최근 승인 순) → 초안만 있는 사례 → 사례 없는 과제 "" (PG 와 같은 순서, 리뷰 W4)."""
    common = InMemoryCommonStore()
    old, new = "co:sw.backend:pppp00000001", "co:sw.backend:pppp00000002"
    await _used_by(common, "p", old)
    assert await common.posting_incidents("p") == [old]
    await _used_by(common, "p", new, at=datetime(2026, 11, 20, tzinfo=UTC))
    await common.save_draft(CommonKey("p", "onto-v", "skeleton"), _task_for(None), [])
    assert await common.posting_incidents("p") == [new, old, ""]


# ── 공유 회사 사례의 함정 틀 (설계 검사 #332 S-F8) ──────────────────────────────


async def test_a_shared_company_case_goes_through_the_trap_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """일반 파이프라인(지원자 실행)이 공유 회사 사례의 기능형 재료로 공통 과제를 만들 때도 인터뷰처럼 함정 틀을 거친다 (사용자 결정
    2026-11-26). 초안은 틀 적용 재료로 쓰고, 틀 적용 재료는 공통 과제 행에 — 빌드 계약 단계가 원 재료 대신 쓴다. 재사용 초안은 다시
    틀을 만들지 않는다 (저장된 틀)."""
    from types import SimpleNamespace

    from task_spec.llm import FakeBackend
    from task_spec.schemas import FramedMaterial, TrapFrame
    from task_spec.spec.case_frame import CaseFrame
    from tests.spec.test_feature import PROMPTS, _client

    store, common, catalog = (
        InMemoryCompanyIncidents(),
        InMemoryCommonStore(),
        load_incidents(PUBLIC),
    )
    rec = await _shared(store, "mmmm00000001")
    seen: dict[str, Any] = {"framer": 0}
    frame = TrapFrame.model_validate(
        {
            "kind": "design",
            "hidden_requirement": "가상 틀 숨은 요구",
            "reason": "pool_acquire_timeout",
        }
    )

    async def framer(incident: PublicIncident, profile: Any) -> CaseFrame:
        seen["framer"] += 1
        seen["framed_id"] = incident.incident_id
        assert incident.feature is not None
        seed = incident.feature.model_copy(update={"hidden_requirement": "가상 틀 숨은 요구"})
        return CaseFrame(
            incident=incident.model_copy(update={"feature": seed}),
            frame=frame,
            framed=FramedMaterial(seed=seed, frame=frame),
            flags=["함정 틀: 가상 표시"],
        )

    async def draft_feature_task(ctx: Any, **kw: Any) -> Any:
        seen["draft_hidden"] = ctx.incident.feature.hidden_requirement
        seen["draft_frame"] = ctx.trap_frame
        return SimpleNamespace(output=object(), provenance=[], errors=[])

    monkeypatch.setattr(spec_mod, "draft_feature_task", draft_feature_task)
    # 진짜 apply_feature 처럼 과제에 쓴 사례 id 를 남긴다 — 다음 실행이 이 공고의 회사 사례를 이어 쓴다 (posting_incidents)
    monkeypatch.setattr(
        spec_mod,
        "apply_feature",
        lambda task, res: task.model_copy(update={"source_incident": rec.incident_id}),
    )
    real_build = spec_mod.build_common

    async def build_common(*args: Any, **kw: Any) -> Any:
        # 온톨로지 없이도 초안 맥락을 채운다 — 사례는 노드가 고른 카탈로그의 그 직군 것 (회사 사례)
        from task_spec.spec.build import CommonDraft
        from tests.spec.test_feature import _ctx

        built = await real_build(*args, **kw)
        ctx = _ctx()
        ctx.incident = kw["incidents"][SUB]
        return CommonDraft(task=built.task, ctx=ctx, flags=built.flags)

    monkeypatch.setattr(spec_mod, "build_common", build_common)

    async def company_of(posting_id: str) -> str | None:
        return "가상회사"

    node = spec_mod.make_draft_spec(
        None,
        llm=_client(FakeBackend()),
        prompts=PROMPTS,
        store=common,
        incidents=catalog,
        feature_default=True,
        company_incidents=store,
        company_of=company_of,
        case_framer=framer,
    )
    out = await node(_state("run-a"))
    task = out["spec"].common_tasks[0]
    assert task.task_kind == "feature", task.task_kind
    assert seen["framer"] == 1 and seen["framed_id"] == rec.incident_id
    assert seen["draft_hidden"] == "가상 틀 숨은 요구" and seen["draft_frame"] == frame
    assert "함정 틀: 가상 표시" in task.review_flags
    stored = await common.framed(task.task_id)
    assert stored is not None and stored.frame == frame
    # 다음 지원자 실행은 저장된 초안을 쓴다 — 틀을 다시 만들지 않는다
    again = await node(_state("run-b"))
    assert again["spec"].common_tasks[0].task_id == task.task_id and seen["framer"] == 1


async def test_a_failing_case_framer_does_not_stop_the_applicant_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """개인정보 점검 M1 (#332 S-F8): 틀 만들기가 예상 밖으로 실패해도 지원자 실행은 원 재료로 계속하고 표시만 남긴다."""
    from types import SimpleNamespace

    from task_spec.llm import FakeBackend
    from task_spec.spec.build import CommonDraft
    from task_spec.spec.case_frame import FLAG_CASE_FRAME_FAILED
    from tests.spec.test_feature import PROMPTS, _client, _ctx

    store, common, catalog = (
        InMemoryCompanyIncidents(),
        InMemoryCommonStore(),
        load_incidents(PUBLIC),
    )
    rec = await _shared(store, "nnnn00000001")
    seen: dict[str, Any] = {}

    async def framer(incident: PublicIncident, profile: Any) -> Any:
        raise RuntimeError("가상 장애")

    async def draft_feature_task(ctx: Any, **kw: Any) -> Any:
        seen["draft_incident"] = ctx.incident.incident_id
        return SimpleNamespace(output=object(), provenance=[], errors=[])

    real_build = spec_mod.build_common

    async def build_common(*args: Any, **kw: Any) -> Any:
        built = await real_build(*args, **kw)
        ctx = _ctx()
        ctx.incident = kw["incidents"][SUB]
        return CommonDraft(task=built.task, ctx=ctx, flags=built.flags)

    monkeypatch.setattr(spec_mod, "draft_feature_task", draft_feature_task)
    monkeypatch.setattr(spec_mod, "apply_feature", lambda task, res: task)
    monkeypatch.setattr(spec_mod, "build_common", build_common)

    async def company_of(posting_id: str) -> str | None:
        return "가상회사"

    node = spec_mod.make_draft_spec(
        None,
        llm=_client(FakeBackend()),
        prompts=PROMPTS,
        store=common,
        incidents=catalog,
        feature_default=True,
        company_incidents=store,
        company_of=company_of,
        case_framer=framer,
    )
    out = await node(_state("run-a"))
    task = out["spec"].common_tasks[0]
    assert seen["draft_incident"] == rec.incident_id  # 원 재료로 초안
    assert FLAG_CASE_FRAME_FAILED in task.review_flags
    assert await common.framed(task.task_id) is None
