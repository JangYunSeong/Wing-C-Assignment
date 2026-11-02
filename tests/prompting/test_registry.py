from __future__ import annotations

from pathlib import Path

import pytest

from task_spec.prompting import PromptError, PromptRegistry

REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"

GOOD = """---
id: demo
version: 1
model: opencode:qwen3.8-flash
params: {temperature: 0}
input_schema: ClaimExtractionInput
output_schema: ClaimExtractionOutput
changed: "테스트"
---

## system
규칙 {{output_schema}}

## user
{{criteria}} {{examples}} {{doc_kind}} {{text}}
"""


def _write(
    root: Path, body: str, name: str = "v1.md", task: str = "demo", current: str = "v1"
) -> PromptRegistry:
    d = root / task
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")
    (d / "CURRENT").write_text(current, encoding="utf-8")
    return PromptRegistry(root)


def test_repo_prompts_all_valid() -> None:
    refs = PromptRegistry(REPO_PROMPTS).validate_all()
    assert "extract_claims@1" in refs


def test_good_template_loads(tmp_path: Path) -> None:
    tpl = _write(tmp_path, GOOD).get("demo")
    assert tpl.ref == "demo@1"
    assert len(tpl.sha256) == 64


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (GOOD.replace("{{text}}", ""), "빠졌습니다"),  # 입력 필드 누락
        (GOOD.replace("{{text}}", "{{text}} {{secret}}"), "알 수 없는"),  # 모르는 변수
        (
            GOOD.replace("model: opencode:qwen3.8-flash", "model: qwen3.8-flash"),
            "머리말",
        ),  # provider 없음
        (GOOD.replace("version: 1", "version: 2"), "버전"),  # 파일명과 불일치
        (GOOD.replace("## user", "## users"), "절"),  # 절 누락
        (GOOD.replace("ClaimExtractionInput", "NoSuchSchema"), "스키마"),
        (GOOD.replace('changed: "테스트"\n', ""), "머리말"),  # 변경 이유 필수
    ],
)
def test_invalid_templates_rejected(tmp_path: Path, body: str, fragment: str) -> None:
    with pytest.raises(PromptError, match=fragment):
        _write(tmp_path, body).get("demo")


def test_bad_current_rejected(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="CURRENT"):
        _write(tmp_path, GOOD, current="latest").get("demo")


def test_missing_version_file(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="v2.md"):
        _write(tmp_path, GOOD, current="v2").get("demo")
