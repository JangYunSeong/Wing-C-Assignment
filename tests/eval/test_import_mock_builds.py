"""목 빌드 적재 — 수리 기록 읽기. 가상 데이터만."""

from __future__ import annotations

import json
from pathlib import Path

from eval.starter.import_mock_builds import repairs_of

REPAIR = {
    "gate": "⑤",
    "target": "gen_accepted",
    "reason": "d1: 가상 " * 80,
    "result": "regenerated",
}


def _write(p: Path, body: dict[str, object]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body, ensure_ascii=False), "utf-8")


def test_repairs_from_source_alpha_success_loop(tmp_path: Path) -> None:
    run, builds = tmp_path / "runs" / "x", tmp_path / "build"
    _write(run / "report.json", {"status": "review", "source": "alpha-x/abc"})
    _write(builds / "alpha-x/abc/loop-1/report.json", {"status": "failed", "repairs": []})
    _write(builds / "alpha-x/abc/loop-2/report.json", {"status": "review", "repairs": [REPAIR]})
    got = repairs_of(run, builds)
    assert [r.target for r in got] == ["gen_accepted"] and len(got[0].reason) == 300


def test_repairs_in_run_report_win_and_missing_source_is_empty(tmp_path: Path) -> None:
    run = tmp_path / "x"
    _write(run / "report.json", {"status": "review", "repairs": []})
    assert repairs_of(run, tmp_path) == []
    _write(run / "report.json", {"status": "review", "source": "gone/abc"})
    assert repairs_of(run, tmp_path) == []
