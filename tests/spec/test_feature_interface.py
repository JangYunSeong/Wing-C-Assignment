"""기능형 명세 ↔ 재료 인터페이스 (#256 1단계 명세 쪽) — 명세가 계약과 다른 형식·칸 이름·스케줄 등록 요구를 공개 요구로 정하지 않게.
가상 글만 (2026-11-15 kb002·11-18 woori001 명세의 모양을 가상 문장으로 옮긴 것)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from task_spec.schemas import MaterialInterface
from task_spec.spec.feature import check_feature, seed_block
from task_spec.spec.incidents import (
    feature_ref,
    incident_by_id,
    interface_for,
    load_incidents,
    pin_feature_incident,
    with_incident,
)
from task_spec.spec.interface_terms import (
    FLAG_SPEC_INTERFACE,
    spec_interface_flags,
    spec_interface_problems,
)
from tests.spec.test_draft import _ctx
from tests.spec.test_feature import _fdraft, _out

INCIDENTS = Path("incidents/public_v1")
BATCH = "pub:data.eng:batch-rerun-late-partial"


def _catalog():  # type: ignore[no-untyped-def]
    return load_incidents(INCIDENTS)


def _iface() -> MaterialInterface:
    return _catalog().interfaces[BATCH]


def _batch():  # type: ignore[no-untyped-def]
    inc = incident_by_id(_catalog(), BATCH)
    assert inc is not None
    return inc


def test_the_catalog_carries_interfaces_through_its_copies() -> None:
    """앱·그래프·인터뷰·평가가 카탈로그를 그대로 넘긴다 — 고정·교체 카탈로그도 인터페이스를 잃지 않아야 한다."""
    catalog = _catalog()
    assert BATCH in catalog.interfaces
    assert interface_for(catalog, _batch()) == _iface()
    pinned = pin_feature_incident(catalog, "data.eng", BATCH)
    assert interface_for(pinned, _batch()) == _iface()
    other = next(i for i in catalog.values() if i.incident_id != BATCH)
    swapped = with_incident(catalog, other)
    assert swapped.interfaces == catalog.interfaces
    assert interface_for(swapped, other) is None  # 인터페이스 없는 재료
    assert interface_for(catalog, None) is None
    assert interface_for({}, _batch()) is None  # 카탈로그가 아닌 dict


def test_seed_block_adds_the_interface_brief_only_when_given() -> None:
    plain = seed_block(_batch())
    assert "재료가 정한 공개 인터페이스" not in plain
    brief = seed_block(_batch(), interface=_iface())
    assert brief.startswith(plain)  # 앞부분은 예전 블록 그대로
    assert (
        "`{out}/daily_sales/date=YYYY-MM-DD/*.parquet` — parquet, 한 줄(행)에 DailySales 하나: store_id · orders · sales_amount"
        in brief
    )
    assert "옵션 date · raw · out" in brief and "스케줄 등록" in brief


def test_other_formats_names_and_schedule_requests_are_sent_back() -> None:
    reqs = [
        ("r1", "결제 이벤트로 날짜·매장별 매출 요약을 만든다"),
        ("r2", "집계 결과를 CSV 파일로 출력한다"),
        ("r3", "매일 정해진 시간에 자동으로 실행되도록 스케줄에 등록한다"),
    ]
    acs = [
        ("p1", "출력 CSV 파일의 컬럼은 collection_date, merchant_category 이다"),
        ("p2", "crontab 목록에 작업이 있다"),
        ("p3", "수동 개입 없이 스케줄 설정된 시각에 작업이 시작된다"),
    ]
    names, schedule = spec_interface_problems(reqs, acs, _iface())
    assert "r2 CSV; p1 CSV·collection_date·merchant_category" in names
    assert "출력 parquet(DailySales: store_id · orders · sales_amount)" in names
    assert "r3, p2, p3" in schedule


def test_what_the_interface_says_is_not_sent_back() -> None:
    ok = [
        ("r1", "요약은 parquet 날짜 파티션으로 쓰고 store_id 별 orders·sales_amount 를 담는다"),
        ("r2", "원시 이벤트 jsonl 은 한 줄에 JSON 객체 하나다 — paid_at 의 날짜로 묶는다"),
        ("r3", "daily_sales 아래 날짜 폴더마다 결과를 둔다. 스케줄러가 매일 실행 날짜로 부른다"),
    ]
    assert spec_interface_problems(ok, [], _iface()) == []
    assert (
        spec_interface_problems([("r1", "CSV 로 출력한다")], [], None) == []
    )  # 인터페이스 없는 재료


def test_names_glued_to_a_korean_particle_are_caught() -> None:
    """한글 조사가 붙어도 (`collection_date로`) — 이름 경계는 ASCII 로 본다."""
    (problem,) = spec_interface_problems(
        [("r1", "collection_date로 묶고 csv로 낸다")], [], _iface()
    )
    assert "r1 CSV·collection_date" in problem


def test_scenario_and_materials_are_only_flagged() -> None:
    """상황 글은 형식·snake_case 이름, 1단계 자료는 형식·JSON 키만 — 승인 화면 표시 (재요청 아님)."""
    scenario = "재무팀은 대시보드에서 요약을 CSV 로 내려받아 정산과 맞춰 본다"
    sample = '{"payment_time": "2026-10-05 09:00:00", "store_id": "S001", "amount": 1200}'
    log = "2026-10-05 02:00 job_id=12 exit_code=1 duration_ms=1200"  # 로그 키는 JSON 키가 아니다
    (flag,) = spec_interface_flags(scenario, [sample, log], _iface())
    assert flag == FLAG_SPEC_INTERFACE + "CSV·payment_time (확인 필요)"
    assert spec_interface_flags("매장별 매출 요약", ['{"store_id": "S001"}'], _iface()) == []
    assert spec_interface_flags(scenario, [sample], None) == []


def test_check_feature_sends_back_a_draft_that_redefines_the_output() -> None:
    d = _fdraft()
    d["requirements"][1]["text"] = "집계 결과를 CSV 파일로 출력한다"
    with_iface = check_feature(_out(d), dataclasses.replace(_ctx(), interface=_iface()))
    assert any("재료 인터페이스에 없는 형식·칸 이름" in p for p in with_iface)
    assert not any(
        "재료 인터페이스" in p for p in check_feature(_out(d), _ctx())
    )  # 인터페이스 없으면 예전 그대로


def test_the_common_task_key_changes_only_with_an_interface() -> None:
    """인터페이스는 사례 파일 밖이라 — 지문에 넣지 않으면 인터페이스 전에 만든 명세를 다시 쓴다."""
    inc = _batch()
    plain = feature_ref(inc)
    assert plain == feature_ref(inc, None) and "+iface#" not in plain
    keyed = feature_ref(inc, _iface())
    assert keyed.startswith(plain + "+iface#") and len(keyed) == len(plain) + len("+iface#") + 8


def test_the_flag_lists_only_the_first_few_names() -> None:
    from task_spec.spec.interface_terms import FLAG_MAX_NAMES

    many = " ".join(f"col_{i}" for i in range(FLAG_MAX_NAMES + 3))
    (flag,) = spec_interface_flags(many, [], _iface())
    assert flag.endswith(f"col_{FLAG_MAX_NAMES - 1} 외 3개 (확인 필요)")


def test_schedule_rewordings_are_caught_but_the_brief_sentence_is_not() -> None:
    caught = [
        ("r1", "배치를 스케줄을 등록해 매일 돌린다"),
        ("r2", "스케줄러를 등록한다"),
        ("r3", "스케줄 작업 등록까지 한다"),
        ("r4", "매일 자동 실행되도록 설정한다"),
        ("r5", "cronjob 으로 돌린다"),
    ]
    (problem,) = spec_interface_problems(caught, [], _iface())
    assert "r1, r2, r3, r4, r5" in problem
    allowed = [
        ("r1", "스케줄러가 매일 실행 날짜로 명령을 부른다"),
        ("r2", "실패하면 exit_code 1 로 끝난다"),
    ]
    assert spec_interface_problems(allowed, [], _iface()) == []


def test_only_raw_record_lines_count_in_materials() -> None:
    """1단계 자료의 JSON 로그({"level":…})는 재료 칸이 없어 보지 않는다 — 재료 칸이 있는 줄(원시 데이터 예시)의 다른 키만 표시."""
    log = '{"level": "WARN", "msg": "retry", "job_run": 3}'
    record = '{"payment_id": "P1", "receive_time": "2026-10-05 09:00:00"}'
    assert spec_interface_flags("매장별 요약", [log], _iface()) == []
    (flag,) = spec_interface_flags("매장별 요약", [f"{log}\n{record}"], _iface())
    assert flag == FLAG_SPEC_INTERFACE + "receive_time (확인 필요)"


def test_build_common_picks_the_material_and_its_interface_for_feature_tasks() -> None:
    from task_spec.spec.build import material_for

    catalog = _catalog()
    inc, iface = material_for(catalog, "data.eng", "p-any", "feature")
    assert inc is not None and inc.incident_id == BATCH and iface == _iface()
    plain, none = material_for(catalog, "data.eng", "p-any", "incident")  # 이슈형은 인터페이스 없음
    assert plain is not None and none is None


def test_the_spec_node_keys_feature_tasks_with_the_interface() -> None:
    from task_spec.nodes.spec import feature_key

    ctx = dataclasses.replace(_ctx(), incident=_batch(), interface=_iface())
    assert feature_key(_batch(), ctx) == feature_ref(_batch(), _iface())
    assert feature_key(_batch(), None) == feature_ref(_batch())


async def test_interview_tasks_on_a_public_material_get_its_interface(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """인터뷰 과제도 공개 재료(batch-rerun)로 만들면 명세가 같은 인터페이스를 따른다 — 예전에는 일반 dict 로 넘겨 빠졌다."""
    import pytest

    from task_spec.interview import task as task_module
    from task_spec.interview.task import (
        OntologyTaskMaker,
        TaskGenerationFailed,
        TaskRequest,
        coding_template,
    )
    from tests.interview.test_task import PROFILE

    seen: dict[str, object] = {}

    async def fake_build(*_: object, **kw: object):  # type: ignore[no-untyped-def]
        seen.update(kw)
        from task_spec.spec.build import CommonDraft

        return CommonDraft(task=None, ctx=None)

    monkeypatch.setattr(task_module, "build_common", fake_build)
    maker = OntologyTaskMaker(
        ontology=None, llm=None, prompts=None, interfaces=_catalog().interfaces
    )  # type: ignore[arg-type]
    req = TaskRequest(
        profile=PROFILE.model_copy(update={"sub": "data.eng"}),
        matches=[],
        coding_incident=_batch(),
        template=coding_template("data.eng"),
        task_id="t",
    )
    with pytest.raises(
        TaskGenerationFailed
    ):  # 가짜 build_common 은 맥락을 만들지 않는다 — 넘긴 카탈로그만 본다
        await maker.feature(req)
    assert interface_for(seen["incidents"], _batch()) == _iface()  # type: ignore[arg-type]


async def test_draft_feature_task_flags_a_scenario_that_names_another_format() -> None:
    import json

    from task_spec.llm import Completion, FakeBackend
    from task_spec.spec.feature import draft_feature_task
    from tests.spec.test_feature import _client
    from tests.spec.test_feature_seed import _REG, _seed_ctx, _v6draft

    draft = _v6draft()
    draft["scenario"] = draft["scenario"] + " 재무팀은 요약을 CSV 로 내려받아 정산과 맞춰 본다."
    fake = FakeBackend().push(Completion(json.dumps(draft, ensure_ascii=False)))
    ctx = dataclasses.replace(_seed_ctx(), interface=_iface())
    res = await draft_feature_task(ctx, llm=_client(fake), prompts=_REG)
    assert any(f.startswith(FLAG_SPEC_INTERFACE) and "CSV" in f for f in res.flags)
    fake2 = FakeBackend().push(Completion(json.dumps(draft, ensure_ascii=False)))
    plain = await draft_feature_task(_seed_ctx(), llm=_client(fake2), prompts=_REG)
    assert not any(
        f.startswith(FLAG_SPEC_INTERFACE) for f in plain.flags
    )  # 인터페이스 없으면 표시 없음
