"""채점용 이미지의 Node grade (task_spec/starter/grade/image_grade_node.py) — 제출물 정리·결과 모양. 가상 데이터만, node 없이."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_spec.starter.grade import image_grade_node as g


def _w(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


def test_prepare_drops_candidate_deps_and_reserved_and_restores_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub, tpl, ov, work = (tmp_path / n for n in ("submission", "template", "overlay", "work"))
    _w(sub / "src/App.tsx", "app")
    _w(sub / "package.json", "지원자가 바꾼 것")
    _w(sub / "node_modules/evil/index.js", "지원자 의존성")
    _w(sub / "dist/index.html")
    _w(sub / "src/__grading__/cases/hc1.test.tsx", "지원자가 둔 가짜 숨은 테스트")
    _w(tpl / "package.json", "템플릿 원본")
    (tpl / "node_modules").mkdir(parents=True)
    _w(ov / "src/__grading__/cases/hc1.test.tsx", "진짜 숨은 테스트")
    for name, val in (("SUBMISSION", sub), ("OVERLAY", ov), ("WORK", work)):
        monkeypatch.setattr(g, name, val)
    g.prepare(
        {"reserved": ["src/__grading__/**"], "locked": ["package.json"], "template_dir": str(tpl)}
    )
    assert (work / "src/App.tsx").read_text() == "app"
    assert (work / "package.json").read_text() == "템플릿 원본"
    assert (work / "src/__grading__/cases/hc1.test.tsx").read_text() == "진짜 숨은 테스트"
    assert not (work / "dist").exists()
    assert (work / "node_modules").is_symlink()
    assert (work / "node_modules").resolve() == (tpl / "node_modules").resolve()


def test_result_maps_config_cases_to_judged_files() -> None:
    cfg = {
        "common_id": "c",
        "revision": 1,
        "cases": [
            {
                "id": "hc1",
                "kind": "hidden",
                "trap_id": "t1",
                "intended_reason": "stale_response_shown",
            },
            {"id": "hc2", "kind": "hidden", "trap_id": "t1"},
            {"id": "pt1", "kind": "public"},
        ],
    }
    tests = [
        {
            "group": "hidden",
            "name": "hc1",
            "result": "ASSERT",
            "reason": "stale_response_shown",
            "message": "m",
        },
        {"group": "public", "name": "pt1", "result": "PASS", "reason": None, "message": ""},
        {
            "group": "existing",
            "name": "App.test.tsx",
            "result": "HARNESS_ERR",
            "reason": None,
            "message": "",
        },
    ]
    r = g.result_json(cfg, "ok", tests, [], 1.0)
    by = {c["id"]: c for c in r["cases"]}
    assert by["hc1"]["passed"] is False and by["hc1"]["reason"] == "stale_response_shown"
    assert by["hc2"]["reason"] == "not_run" and by["pt1"]["passed"] is True
    assert r["summary"]["existing_failed"] == ["App.test.tsx"] and r["summary"]["hidden_total"] == 2
    broken = g.result_json(
        cfg,
        "compile_error",
        [],
        [{"path": "src/App.tsx", "line": 1, "message": "TS1", "detail": ""}],
        1.0,
    )
    assert all(c["reason"] == "compile_error" for c in broken["cases"])
    assert broken["compile_errors"][0]["path"] == "src/App.tsx"
