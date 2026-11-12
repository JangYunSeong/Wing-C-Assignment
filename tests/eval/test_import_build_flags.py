"""적재가 빌드 단계 검토 표시·수리 기록을 run_alpha 보고서(`build.*`)에서도 읽는다 — 가상 보고서만."""

from __future__ import annotations

import json
from pathlib import Path

from eval.starter.import_mock_builds import build_flags_of, merged_flags, repairs_of


def test_run_alpha_report_build_flags_and_repairs_are_read(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    report = {
        "build": {
            "status": "review",
            "review_flags": ["숨은 케이스 hc2 는 … 채점에서 뺐다", "운영 값 x=8 … 승인자 확인"],
            "repairs": [
                {
                    "gate": "③",
                    "target": "gen_hidden",
                    "reason": "흔한 구현이 hc2 통과",
                    "result": "regenerated",
                }
            ],
        }
    }
    (run / "report.json").write_text(json.dumps(report, ensure_ascii=False), "utf-8")
    flags = build_flags_of(run, tmp_path)
    assert flags == report["build"]["review_flags"]
    assert [r.gate for r in repairs_of(run, tmp_path)] == ["③"]
    task_flags = ["공개 사례 기반 — 승인자 검토 필요", flags[0]]
    assert merged_flags(task_flags, flags) == [
        *task_flags,
        flags[1],
    ]  # 같은 글은 한 번, 명세 표시가 앞


def test_report_without_build_section_has_no_build_flags(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text("{}", "utf-8")
    assert build_flags_of(run, tmp_path) == [] and repairs_of(run, tmp_path) == []
