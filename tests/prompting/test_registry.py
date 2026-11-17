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
    reg = PromptRegistry(REPO_PROMPTS)
    assert f"extract_claims@{reg.current_version('extract_claims')}" in refs
    # 승격 후에도 이전 버전은 그대로 읽힌다 (vN 은 고치지 않는다)
    assert reg.get("extract_claims", 1).ref == "extract_claims@1"
    assert reg.get("extract_claims", 2).ref == "extract_claims@2"


def test_task_without_current_is_skipped_but_its_files_are_checked(tmp_path: Path) -> None:
    """평가 전 새 작업(CURRENT 없음)은 운영 검증에서 건너뛰고 판 파일 형식만 본다 (2026-11-16 사용자 — 새 단계는 CURRENT 가 생길 때만)."""
    reg = _write(tmp_path, GOOD)
    (tmp_path / "demo" / "CURRENT").unlink()
    assert reg.validate_all() == [] and not reg.has_current("demo")
    (tmp_path / "demo" / "v1.md").write_text(GOOD.replace("{{text}}", ""), encoding="utf-8")
    with pytest.raises(PromptError, match="빠졌습니다"):
        PromptRegistry(tmp_path).validate_all()  # 형식이 깨진 판은 CURRENT 가 없어도 기동 실패


def test_analysis_request_current_is_v3_by_user_decision() -> None:
    """draft_analysis_request — v3 는 흘림 기준 미달이지만 사용자 결정 (나)로 CURRENT (2026-11-17, PROMOTIONS.md — 불변식 7 의 사용자 예외).
    흘림은 명세 승인 화면 표시로 사람이 거른다. 예전 판은 그대로 남는다."""
    reg = PromptRegistry(REPO_PROMPTS)
    assert reg.current_version("draft_analysis_request") == 3
    assert (REPO_PROMPTS / "draft_analysis_request" / "PROMOTIONS.md").is_file()
    assert reg.get("draft_analysis_request", 1).ref == "draft_analysis_request@1"
    assert reg.get("draft_analysis_request", 2).ref == "draft_analysis_request@2"


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
