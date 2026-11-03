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
