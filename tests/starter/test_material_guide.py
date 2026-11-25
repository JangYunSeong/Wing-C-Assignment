"""재료 숨은 안내 (#256 재료 옮기기) — incidents/public_v1/hidden/ 읽기·템플릿에 합치기(starter/material_guide.py)·생성기 입력·빌드 연결.

데이터 템플릿 v1~v3 의 하네스 문서·사유 how·허용 구현 조건과 숨은·공개 테스트 프롬프트에 한 재료(batch-rerun)의 함정이 일반 규칙처럼
들어 있던 것을 재료 파일로 옮겼다. 이 재료의 생성기 입력은 옮기기 전과 같은 안내를 받아야 하고(사유 정의·허용 구현 조건은 글자까지),
템플릿·프롬프트에는 재료 낱말이 남지 않아야 한다. 가상 데이터만.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.builder import steps as steps_mod
from task_spec.builder.finalize import _template_reasons
from task_spec.builder.runner import ContractResult, Job, run_job
from task_spec.nodes.starter.build import BuildOutcome
from task_spec.nodes.starter.generate import BuildInputs, accepted_input, hidden_input, public_input
from task_spec.nodes.starter.plan_contract import PlanResult, for_material, plan_input
from task_spec.prompting import PromptRegistry
from task_spec.schemas import FailureReason, GeneratedFile, MaterialGuide
from task_spec.spec.feature import find_terms
from task_spec.spec.incidents import incident_by_id, load_incidents, load_interfaces
from task_spec.starter.material_guide import (
    HIDDEN_TITLE,
    PUBLIC_TITLE,
    contract_rules_text,
    guide_problems,
    guide_ref,
    load_guides,
    public_rules,
    with_guide,
)
from task_spec.starter.static_check import Overlay, _code_pitfalls
from task_spec.starter.templates import load_template, restrict_reasons
from tests.builder.test_build_provenance import _job, _real_steps
from tests.builder.test_runner import FakeSteps, _cfg, _claimed
from tests.starter.test_data_template import _contract, _plan
from tests.starter.test_generate import _task

INCIDENTS = Path("incidents/public_v1")
BATCH = "pub:data.eng:batch-rerun-late-partial"
DUMP = "pub:data.eng:dump-deleted-still-current"
DE = load_template("data/python-duckdb")
DATA_CODES = {
    "late_data_missed",
    "duplicate_counted",
    "partial_output_visible",
    "rerun_not_idempotent",
    "contract_mismatch",
}


def _guide() -> MaterialGuide:
    return load_guides(INCIDENTS)[BATCH]


def _seed() -> Any:
    inc = incident_by_id(load_incidents(INCIDENTS), BATCH)
    assert inc is not None and inc.feature is not None
    return inc.feature


def _reason(code: str, how: str = "일으키는 법 한 줄", tag: str | None = None) -> FailureReason:
    return FailureReason(
        code=code,
        description=f"{code} 설명",
        log_patterns=[tag or rf"\[reason:{code}\]"],
        how=how,
    )


# ── 재료 파일 (git) ─────────────────────────────────────────────────────


def test_every_committed_guide_fits_its_material_and_template() -> None:
    """재료 안내마다 재료가 있고, 직군 템플릿과 합쳐도 표기가 같고, 재료 사유를 다 담는다 (guide_problems — builder 가 계약 단계 앞에서
    같은 검사를 한다). 공고 배정 전 평가 전용 재료(templates 빈 것)도 직군 템플릿과 대조한다."""
    guides = load_guides(INCIDENTS)
    assert {BATCH, DUMP} <= set(guides)
    catalog = load_incidents(INCIDENTS)
    for incident_id, guide in guides.items():
        assert guide_problems(guide, incident_by_id(catalog, incident_id)) == [], incident_id
    assert guide_ref(guides[BATCH]) and guide_ref(None) is None


def test_public_side_guide_text_never_names_the_hidden_requirement() -> None:
    """공개 테스트 입력 규칙은 계약 입력·공개 테스트 생성기 입력으로 나간다 — 모든 재료 안내에서 재료의 숨은 낱말이 없어야 한다."""
    catalog = load_incidents(INCIDENTS)
    for incident_id, guide in load_guides(INCIDENTS).items():
        inc = incident_by_id(catalog, incident_id)
        assert inc is not None and inc.feature is not None
        f = inc.feature
        terms = [
            *f.trigger_terms,
            *f.leak_terms,
            *f.stage2_cause_terms,
            *inc.hidden_terms,
            *inc.avoid_terms,
        ]
        assert find_terms(terms, json.dumps(guide.public_rules, ensure_ascii=False)) == [], (
            incident_id
        )


def test_moving_the_guide_lost_nothing_for_batch_rerun() -> None:
    """옮기기 전(데이터 템플릿 v3)의 사유 정의·허용 구현 조건을 이 재료는 그대로 받는다 — 템플릿 v4 + 재료 안내 = v3."""
    v3 = load_template("data/python-duckdb", 3).manifest
    merged = with_guide(load_template("data/python-duckdb", 4), _guide()).manifest
    seed = _seed()
    before = [r for r in v3.failure_reasons if r.code in seed.reasons]
    after = restrict_reasons(
        with_guide(load_template("data/python-duckdb", 4), _guide()), seed.reasons
    ).manifest.failure_reasons
    assert after == before  # 순서·설명·일으키는 법·표기까지
    assert merged.stack_conditions == v3.stack_conditions


def test_template_v4_keeps_the_data_reason_codes_for_materials_without_a_guide() -> None:
    """인터뷰 재료(안내 없음)는 템플릿 사유 목록에서 고른다 (interview/task.prepare_coding_incident) — 코드 다섯은 그대로, 시험법은 일반."""
    m = DE.manifest
    assert m.ref == "data/python-duckdb@v4" and m.reason_codes == DATA_CODES
    hows = " ".join(r.how or "" for r in m.failure_reasons)
    assert "다음 날짜" not in hows and "날짜 폴더" not in hows and "늦은 데이터·사본" not in hows


@pytest.mark.parametrize("doc", ["HARNESS.md", "PUBLIC.md"])
def test_template_v4_docs_and_conditions_carry_no_material_words(doc: str) -> None:
    text = (DE.hidden / doc).read_text("utf-8") + "\n".join(DE.manifest.stack_conditions)
    for word in ("사본", "늦게 온", "늦은 레코드", "결제", "payment", "paid_at", "재전송", "매장"):
        assert word not in text, (doc, word)


@pytest.mark.parametrize("task", ["gen_hidden_py", "gen_public_py"])
def test_current_test_prompts_carry_no_material_words(task: str) -> None:
    p = PromptRegistry(Path("prompts")).get(task)
    body = p.system + p.user
    for word in ("사본", "늦게 온", "늦은 레코드", "늦은 데이터"):
        assert word not in body, (task, word)
    assert "재료" in body  # 재료 안내·재료가 정한 공개 테스트 입력 규칙을 따르라고


# ── 합치기 ─────────────────────────────────────────────────────────────


def test_with_guide_replaces_same_codes_in_place_and_appends_new_ones() -> None:
    base = DE.manifest
    guide = MaterialGuide(
        incident_id="pub:data.eng:other-material",
        failure_reasons=[
            _reason("artifact_over_limit"),
            _reason("contract_mismatch", how="재료의 출력 비교법"),
        ],
        accepted_conditions=["재료의 허용 구현 조건 한 줄입니다"],
    )
    t = with_guide(DE, guide)
    codes = [r.code for r in t.manifest.failure_reasons]
    assert codes == [*(r.code for r in base.failure_reasons), "artifact_over_limit"]
    assert next(r for r in t.manifest.failure_reasons if r.code == "contract_mismatch").how == (
        "재료의 출력 비교법"
    )
    assert t.manifest.stack_conditions == [
        *base.stack_conditions,
        "재료의 허용 구현 조건 한 줄입니다",
    ]
    assert t.guide is guide and DE.guide is None and DE.manifest is base  # 원본 템플릿은 그대로
    assert with_guide(t, guide) is t and with_guide(DE, None) is DE
    # 좁힌 뒤 다시 불러도 좁힌 사유가 되살아나지 않는다 (계약 단계·빌드가 각자 부른다)
    narrowed = restrict_reasons(t, ["artifact_over_limit"])
    assert with_guide(narrowed, guide).manifest.reason_codes == {"artifact_over_limit"}


def test_with_guide_rejects_a_different_harness_tag_and_a_second_material() -> None:
    bad = MaterialGuide(
        incident_id="pub:data.eng:other-material",
        failure_reasons=[_reason("contract_mismatch", tag=r"\[why:contract_mismatch\]")],
    )
    with pytest.raises(ValueError, match="표기"):
        with_guide(DE, bad)
    with pytest.raises(ValueError, match="이미 재료"):
        with_guide(with_guide(DE, _guide()), bad.model_copy(update={"failure_reasons": []}))


def test_guide_schema_rejects_duplicate_codes_and_odd_sentences() -> None:
    with pytest.raises(ValidationError, match="겹침"):
        MaterialGuide(incident_id=BATCH, failure_reasons=[_reason("a_b"), _reason("a_b")])
    with pytest.raises(ValidationError, match="10~240"):
        MaterialGuide(incident_id=BATCH, public_rules=["짧음"])
    with pytest.raises(ValidationError, match="10~400"):
        MaterialGuide(incident_id=BATCH, hidden_rules=["짧음"])
    with pytest.raises(ValidationError):
        MaterialGuide(incident_id="co:data.eng:company-case")  # 공개 사례만


# ── 생성기 입력 ─────────────────────────────────────────────────────────


def _inputs(guide: MaterialGuide | None) -> BuildInputs:
    return BuildInputs(
        task=_task(), contract=_contract(), plan=_plan(), template=DE, seed=_seed(), guide=guide
    )


def test_build_and_contract_stage_merge_the_same_way() -> None:
    seed, guide = _seed(), _guide()
    b = _inputs(guide)
    planned = for_material(DE, seed, guide)
    assert b.template.manifest.failure_reasons == planned.manifest.failure_reasons
    assert b.template.guide == guide == planned.guide
    assert [r.code for r in b.template.manifest.failure_reasons] == list(seed.reasons)


def test_hidden_input_gets_the_guide_after_the_harness_doc() -> None:
    guide = _guide()
    b = _inputs(guide)
    api = hidden_input(b.task, b.contract, b.plan, b.template).harness_api
    doc = (DE.hidden / "HARNESS.md").read_text("utf-8").rstrip("\n")
    assert api.startswith(doc)
    tail = api[len(doc) :]
    assert tail.index(HIDDEN_TITLE) < tail.index(
        "grading/cases/contract.py"
    )  # 재료 안내 → 계약 도우미
    assert all(rule in tail for rule in guide.hidden_rules)
    assert guide.hidden_example.splitlines()[0] in tail and "```python" in tail
    # 사유 줄·케이스의 일으키는 법은 재료 정의로
    inp = hidden_input(b.task, b.contract, b.plan, b.template)
    assert (
        next(r.how for r in guide.failure_reasons if r.code == "late_data_missed")
        in inp.hidden_plan
    )
    assert "같은 폴더·다음 날짜 폴더" in inp.failure_reasons


def test_public_input_gets_the_public_rules_and_nothing_hidden() -> None:
    guide = _guide()
    b = _inputs(guide)
    api = public_input(b.task, b.contract, b.plan, b.template).harness_api
    assert PUBLIC_TITLE in api and all(rule in api for rule in guide.public_rules)
    assert HIDDEN_TITLE not in api
    assert not [rule for rule in guide.hidden_rules if rule in api]
    assert not [r.how for r in guide.failure_reasons if r.how and r.how in api]
    assert public_rules(b.template) == tuple(guide.public_rules)


def test_accepted_input_gets_the_material_conditions() -> None:
    guide = _guide()
    b = _inputs(guide)
    starter = Overlay(kind="starter", files=())
    conditions = accepted_input(
        b.task, b.contract, b.template, starter, b.task.accepted_designs[0]
    ).conditions
    assert all(c in conditions for c in guide.accepted_conditions)


def test_without_a_guide_the_inputs_are_the_template_alone() -> None:
    b = _inputs(None)
    assert b.template.guide is None
    hid = hidden_input(b.task, b.contract, b.plan, b.template).harness_api
    pub = public_input(b.task, b.contract, b.plan, b.template).harness_api
    assert HIDDEN_TITLE not in hid and PUBLIC_TITLE not in pub
    assert contract_rules_text(b.template) == "" and public_rules(b.template) == ()


def test_plan_input_puts_the_public_rules_after_the_interface_block() -> None:
    """#256 3단계에 인터페이스 파일에서 렌더하던 글과 같은 자리·같은 글 (계약 입력이 옮기기 전과 같다)."""
    seed, guide = _seed(), _guide()
    iface = load_interfaces(INCIDENTS)[BATCH]
    task = _task()
    text = plan_input(task, for_material(DE, seed, guide), None, seed, iface).row_rules
    block = "- 재료가 정한 공개 테스트 입력 범위 — 공개 테스트 checks 는 아래를 지킨다 (숨은 케이스는 해당 없음)."
    assert block in text and all(f"  - {r}" in text for r in guide.public_rules)
    assert text.index("엔티티 m2 DailySales") < text.index(block)
    bare = plan_input(task, for_material(DE, seed, None), None, seed, iface).row_rules
    assert block not in bare and not [r for r in guide.public_rules if r in bare]


# ── 빌드 연결 ───────────────────────────────────────────────────────────


async def test_build_steps_hand_the_guide_to_the_contract_and_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps, job = _real_steps(tmp_path), await _job(tmp_path)
    assert steps.guides[BATCH] == _guide()
    seen: dict[str, Any] = {}

    async def plan(*_: Any, **kw: Any) -> PlanResult:
        seen["contract"] = kw.get("guide")
        return PlanResult(contract=_contract(), hidden_plan=_plan())

    monkeypatch.setattr(steps_mod, "plan_contract", plan)
    c = await steps.contract(job)
    assert seen["contract"] == _guide() and c.guide == _guide()

    async def loop(b: BuildInputs, *_: Any, **__: Any) -> BuildOutcome:
        seen["build"] = b.template.guide
        return BuildOutcome(status="failed")

    monkeypatch.setattr(steps_mod, "build_and_repair", loop)
    c = ContractResult(
        template=DE, contract=_contract(), plan=_plan(), cost_usd=0.0, seed=_seed(), guide=_guide()
    )
    with pytest.raises(steps_mod.StepFailed):
        await steps.build(job, c)
    assert seen["build"] == _guide()


def test_eval_kit_reason_descriptions_follow_the_guide() -> None:
    guide = _guide()
    plain = _template_reasons(DE.manifest.ref)
    merged = _template_reasons(DE.manifest.ref, guide)
    assert plain["duplicate_counted"] == "같은 레코드의 사본이 두 번 이상 집계됨"
    assert merged["duplicate_counted"] == "같은 레코드의 사본(재전송)이 두 번 이상 집계됨"
    assert set(merged) == set(plain)


def test_narrowing_first_then_merging_gives_the_same_reasons() -> None:
    """좁힌 코드는 템플릿에 남는다 — 재료 안내를 나중에 합쳐도 좁힌 밖의 사유가 되살아나지 않는다 (리뷰 2026-11-24)."""
    guide = _guide().model_copy(
        update={"failure_reasons": [*_guide().failure_reasons, _reason("artifact_over_limit")]}
    )
    for codes in (["duplicate_counted"], ["artifact_over_limit", "late_data_missed"]):
        merged_first = restrict_reasons(with_guide(DE, guide), codes)
        narrowed_first = with_guide(restrict_reasons(DE, codes), guide)
        assert merged_first.manifest.failure_reasons == narrowed_first.manifest.failure_reasons
        assert {r.code for r in narrowed_first.manifest.failure_reasons} == set(codes)
    t = restrict_reasons(
        DE, list(DE.manifest.reason_codes)
    )  # 걸러진 것이 없어도 좁힌 코드는 남는다
    assert t.narrowed == DE.manifest.reason_codes and restrict_reasons(t, t.narrowed) is t
    assert "artifact_over_limit" not in with_guide(t, guide).manifest.reason_codes


def test_guide_problems_catch_a_guide_that_does_not_fit_its_template() -> None:
    inc = incident_by_id(load_incidents(INCIDENTS), BATCH)
    assert guide_problems(_guide(), inc) == []
    assert guide_problems(_guide(), None) == [
        f"재료 안내의 재료 {BATCH} 가 없거나 기능형 칸이 없음"
    ]
    tagged = _guide().model_copy(
        update={"failure_reasons": [_reason("late_data_missed", tag=r"\[why:late\]")]}
    )
    assert any("표기" in p for p in guide_problems(tagged, inc))
    short = _guide().model_copy(update={"failure_reasons": []})
    assert guide_problems(short, inc) == []  # 템플릿이 세 사유를 다 가진다 — 재료 정의만 빠짐
    odd = FailureReason(
        code="artifact_over_limit",
        description="한도 넘은 산출물",
        log_patterns=[r"\[reason:artifact_over_limit\]"],
        feasible=[{"when": "rows >= max_bytes", "why": "한도보다 큰 입력"}],
    )
    assert any(
        "max_bytes" in p
        for p in guide_problems(_guide().model_copy(update={"failure_reasons": [odd]}), inc)
    )
    missing = inc.model_copy(  # type: ignore[union-attr]
        update={"feature": inc.feature.model_copy(update={"reasons": ["artifact_over_limit"]})}  # type: ignore[union-attr]
    )
    assert any("artifact_over_limit" in p for p in guide_problems(_guide(), missing))


async def test_contract_step_stops_on_a_guide_that_does_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps, job = _real_steps(tmp_path), await _job(tmp_path)
    steps.guides[BATCH] = _guide().model_copy(
        update={"failure_reasons": [_reason("late_data_missed", tag=r"\[why:late\]")]}
    )

    async def never(*_: Any, **__: Any) -> PlanResult:
        raise AssertionError("재료 안내가 맞지 않으면 계약을 만들지 않는다")

    monkeypatch.setattr(steps_mod, "plan_contract", never)
    with pytest.raises(steps_mod.StepFailed, match="재료 숨은 안내"):
        await steps.contract(job)


async def test_a_build_records_the_guide_it_used(tmp_path: Path) -> None:
    store, b = await _claimed()

    class GuidedSteps(FakeSteps):
        async def contract(self, job: Job) -> ContractResult:
            c = await super().contract(job)
            return dataclasses.replace(c, guide=_guide())

    done = await run_job(store, GuidedSteps(tmp_path), _cfg(tmp_path), b)
    assert done.outcome is not None and done.outcome.guide_sha256 == guide_ref(_guide())
    store, b = await _claimed()
    plain = await run_job(store, FakeSteps(tmp_path), _cfg(tmp_path), b)
    assert plain.outcome is not None and plain.outcome.guide_sha256 is None


def test_eval_only_materials_are_checked_against_the_sub_template() -> None:
    """공고 배정 전(templates 가 빈 평가 전용) 재료도 빌드는 직군 템플릿으로 만든다 — 대조도 그 템플릿과 (#256 5단계)."""
    inc = incident_by_id(load_incidents(INCIDENTS), BATCH)
    assert inc is not None
    unassigned = inc.model_copy(update={"templates": []})
    assert guide_problems(_guide(), unassigned) == []
    tagged = _guide().model_copy(
        update={"failure_reasons": [_reason("late_data_missed", tag=r"\[why:late\]")]}
    )
    assert any("표기" in p for p in guide_problems(tagged, unassigned))  # 대조가 돈다


def test_read_rows_on_a_single_file_is_a_code_pitfall() -> None:
    """#256 5단계 woori001: 생성 구현 셋이 data_files 를 돌며 read_rows(파일) — 실행 전에 바른 쓰는 법과 함께 되돌린다."""
    bad = Overlay(
        kind="ref_accepted",
        files=(
            GeneratedFile(
                path="pipeline/build.py",
                content=(
                    "from pipeline.inputs import data_files, read_rows\n"
                    "def load(d):\n    rows = []\n    for f in data_files(d):\n        rows.extend(read_rows(f))\n    return rows\n"
                ),
            ),
        ),
        label="d1",
    )
    good = Overlay(
        kind="ref_accepted",
        files=(
            GeneratedFile(
                path="pipeline/build.py",
                content="from pipeline.inputs import data_files, read_rows\nrows = read_rows(data_files('x'))\n",
            ),
        ),
        label="d1",
    )
    assert any("read_rows([파일])" in p.message for p in _code_pitfalls(bad, DE))
    assert _code_pitfalls(good, DE) == []
