"""공개 사례 (incidents/public_v1) — 형식·입력 블록·고유어 검사·source_incident. 사례 파일은 공개 자료 요약."""

from __future__ import annotations

import json
from pathlib import Path

from task_spec.schemas import DraftTaskInputV2, TaskItem
from task_spec.spec.common import FLAG_NO_INCIDENT, FLAG_PUBLIC_INCIDENT
from task_spec.spec.draft import DraftResult, apply, build_input, check
from task_spec.spec.incidents import NONE, incident_block, load_incidents
from tests.spec.test_draft import _ctx, _out

ROOT = Path(__file__).resolve().parents[2] / "incidents" / "public_v1"


def test_repo_incidents_are_valid_one_per_sub() -> None:
    inc = load_incidents(ROOT)
    assert len(inc) == 13 and "sw.game" not in inc  # sw.game 보류
    assert len({i.incident_id for i in inc.values()}) == 13
    assert all(i.avoid_terms for i in inc.values())
    assert inc["sw.qa"].task_pattern == "qa_mutation"
    assert not inc["data.ml"].verified  # 원문 미확인 표시가 유지되는지


def test_missing_dir_means_no_incidents(tmp_path: Path) -> None:
    assert load_incidents(tmp_path / "없음") == {}


def test_block_has_principle_but_no_org_title_url() -> None:
    inc = load_incidents(ROOT)["sw.embedded"]
    block = incident_block(inc)
    assert inc.deep_structure in block and inc.naive_fix in block
    assert inc.url not in block and inc.title not in block and inc.org not in block
    assert incident_block(None) == NONE
    assert "과제 형태: qa_mutation" in incident_block(load_incidents(ROOT)["sw.qa"])


def test_v2_input_gets_incident_and_v1_does_not() -> None:
    inc = load_incidents(ROOT)["sw.backend"]
    ctx = _ctx()
    ctx.incident = inc
    v2 = build_input(ctx, model=DraftTaskInputV2)
    assert isinstance(v2, DraftTaskInputV2) and inc.deep_structure in v2.incident
    v1 = build_input(ctx)
    assert "incident" not in v1.model_dump()


def test_check_flags_source_terms() -> None:
    ctx = _ctx()
    ctx.incident = load_incidents(ROOT)["sw.embedded"]
    leaked = _out(scenario="화성 탐사선처럼 태스크가 리셋된다.")
    assert any("고유어" in p and "화성" in p for p in check(leaked, ctx))
    assert check(_out(), ctx) == []


def test_apply_sets_source_incident_and_swaps_flag() -> None:
    ctx = _ctx()
    task = TaskItem(
        task_id="t",
        track="common",
        target_competencies=ctx.targets,
        tech_env=ctx.env,
        difficulty_target="high",
        time_limit_min=60,
        review_flags=[FLAG_NO_INCIDENT],
    )
    out = apply(task, DraftResult(output=_out(), incident_id="pub:sw.backend:x"))
    assert out.source_incident == "pub:sw.backend:x"
    assert out.review_flags == [FLAG_PUBLIC_INCIDENT]
    plain = apply(task, DraftResult(output=_out()))
    assert plain.source_incident is None and plain.review_flags == [FLAG_NO_INCIDENT]


def test_incident_files_are_plain_json_with_known_keys() -> None:
    keys = set(json.loads((ROOT / "sw.qa.json").read_text("utf-8")))
    assert {"incident_id", "deep_structure", "avoid_terms", "url"} <= keys


def test_hidden_terms_only_blocked_in_visible_text() -> None:
    ctx = _ctx()
    ctx.incident = load_incidents(ROOT)["infra.network"]
    in_trap = _out(
        traps=[{"trap_id": "t1", "description": "SYN cookie 경로", "why_ai_fails": "재시작 권고"}]
    )
    assert check(in_trap, ctx) == []  # 승인자용 함정 설명에는 허용
    shown = _out(scenario="재시작 뒤 SYN cookie 카운터가 늘었다.")
    assert any("원인 용어" in p for p in check(shown, ctx))


def test_block_has_accepted_fixes_but_not_curator_notes() -> None:
    incs = load_incidents(ROOT)
    assert all(i.accepted_fixes for i in incs.values())
    inc = incs["sw.backend"]
    block = incident_block(inc)
    assert "정답으로 인정할 조치" in block and inc.accepted_fixes[0] in block
    assert inc.notes not in block  # 큐레이터 메모(출처 URL 등)는 넣지 않는다


def test_check_flags_copied_incident_text_and_numbers() -> None:
    """원 사례의 오류 문구·풀 지표를 그대로 쓰면 재요청 (2026-11-03 sw.backend 5판 모두)."""
    ctx = _ctx()
    ctx.incident = load_incidents(ROOT)["sw.backend"]
    copied = _out(
        scenario=(
            "배치에서 'Connection is not available, request timed out after 30000ms' 가 난다. "
            "풀 지표는 total=10, active=10, idle=0 이다."
        )
    )
    problems = "\n".join(check(copied, ctx))
    assert "원 사례 문구·수치를 그대로 씀" in problems and "total=10" in problems
    changed = _out(
        scenario="정산 작업에서 '대기 시간 초과' 가 난다. 풀 지표는 사용 8/8, 대기 12 이다."
    )
    assert not any("원 사례" in p for p in check(changed, ctx))
    # 라이브러리 기본 오류 문구 하나만으로는 원 사례가 특정되지 않는다
    generic = _out(
        scenario=(
            "'Connection is not available, request timed out after 30000ms' 가 난다. "
            "풀 지표는 사용 8/8 이다."
        )
    )
    assert not any("원 사례" in p for p in check(generic, ctx))


def test_source_literals_are_not_in_prompt_block() -> None:
    inc = load_incidents(ROOT)["sw.backend"]
    block = incident_block(inc)
    assert inc.source_literals and not any(x in block for x in inc.source_literals)


def test_incident_ref_changes_only_with_content() -> None:
    from task_spec.spec.incidents import incident_ref

    inc = load_incidents(ROOT)["sw.backend"]
    assert incident_ref(inc) == incident_ref(inc.model_copy()) and incident_ref(inc).startswith(
        inc.incident_id + "#"
    )
    edited = inc.model_copy(update={"accepted_fixes": [*inc.accepted_fixes, "새 조치"]})
    assert incident_ref(edited) != incident_ref(inc)


def test_avoid_terms_checked_in_artifacts_by_word_boundary() -> None:
    from tests.spec.test_artifacts import _v3

    ctx = _ctx()
    ctx.incident = load_incidents(ROOT)["sw.backend"]
    out = _v3()
    arts = [a.model_copy() for a in out.artifacts]
    arts[0] = arts[0].model_copy(update={"content": "select next_val from hibernate_sequence"})
    leaked = check(out.model_copy(update={"artifacts": arts}), ctx)
    assert any("고유어" in p and "hibernate_sequence" in p for p in leaked)
    ctx.incident = load_incidents(ROOT)["data.analytics"]  # avoid_terms 에 'ExP'
    arts[0] = arts[0].model_copy(update={"content": "experiment_id=7 expected=0.42"})
    assert not any("고유어" in p for p in check(out.model_copy(update={"artifacts": arts}), ctx))
