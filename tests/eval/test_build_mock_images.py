"""알파 빌드 폴더 → 템플릿 (eval/starter/build_mock_images.template_of). 가상 report 만."""

from __future__ import annotations

import json
from pathlib import Path

from eval.starter import build_mock_images as b


def _run(root: Path, name: str, report: dict[str, object]) -> Path:
    run = root / name
    run.mkdir()
    (run / "report.json").write_text(json.dumps(report), "utf-8")
    return run


def test_template_follows_the_job_family_and_the_pinned_version(tmp_path: Path) -> None:
    """빌드 폴더의 직군으로 템플릿, 직군 없는 손 예시 루프는 Java, 알파가 고정한 판이 있으면 그 판."""
    for sub, ref in (
        ("sw.frontend", "frontend/react-vite-ts@v12"),
        ("sw.backend", "backend/java-spring-boot3-jpa@v2"),
        ("data.eng", "data/python-duckdb@v1"),
    ):
        assert b.template_of(_run(tmp_path, sub, {"sub": sub})).manifest.ref == ref
    assert b.template_of(_run(tmp_path, "hand", {})).manifest.ref.startswith("backend/java")
    pinned = _run(
        tmp_path,
        "pinned",
        {"sub": "sw.backend", "template_pinned": "backend/java-spring-boot3-jpa@v1"},
    )
    assert b.template_of(pinned).manifest.ref == "backend/java-spring-boot3-jpa@v1"
