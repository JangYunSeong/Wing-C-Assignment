"""CURRENT 프롬프트마다 렌더된 출력 스키마({{output_schema}} 에 들어가는 글)의 해시를 고정한다 (#240).

출력 모델(schemas/ 의 클래스·필드·설명)이 바뀌면 vN 파일이 같아도 모델이 받는 프롬프트가 바뀐다. provenance
prompt_sha256(파일 + 렌더된 출력 스키마)이 그것을 덮지만, 바뀐 줄 모르고 지나가지 않게 여기서 한 번 멈춘다.
판 번호는 넣지 않는다 — 출력 모델·schema_style 이 같은 CURRENT 올림은 그대로 통과한다. 갱신 방법은 실패 메시지에.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from task_spec.prompting import PromptRegistry

REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
GOLDEN = Path(__file__).with_name("output_schema_golden.json")
UPDATE_ENV = "UPDATE_OUTPUT_SCHEMA_GOLDEN"
UPDATE_CMD = f"{UPDATE_ENV}=1 uv run pytest tests/prompting/test_output_schema_golden.py -q"


def rendered_output_schemas() -> dict[str, dict[str, str]]:
    """작업 → CURRENT 판의 출력 모델·렌더 방식·렌더된 글의 sha256 (CURRENT 가 없는 작업은 운영에서 안 쓰므로 뺀다)."""
    reg = PromptRegistry(REPO_PROMPTS)
    out: dict[str, dict[str, str]] = {}
    for d in sorted(p for p in REPO_PROMPTS.iterdir() if p.is_dir()):
        if not reg.has_current(d.name):
            continue
        tpl = reg.get(d.name)
        out[d.name] = {
            "output_schema": tpl.meta.output_schema,
            "schema_style": tpl.meta.schema_style,
            "sha256": hashlib.sha256(tpl.output_schema_text.encode()).hexdigest(),
        }
    return out


def _short(entry: dict[str, str] | None) -> str:
    if entry is None:
        return "(없음)"
    return f"{entry['output_schema']}·{entry['schema_style']}·{entry['sha256'][:12]}"


def test_rendered_output_schema_of_current_prompts_is_pinned() -> None:
    got = rendered_output_schemas()
    if os.environ.get(UPDATE_ENV) == "1":
        GOLDEN.write_text(
            json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8"
        )
    want = json.loads(GOLDEN.read_text("utf-8"))
    changed = [
        f"  - {task}: {_short(want.get(task))} → {_short(got.get(task))}"
        for task in sorted(want.keys() | got.keys())
        if want.get(task) != got.get(task)
    ]
    assert not changed, "\n".join(
        [
            "CURRENT 프롬프트에 들어가는 출력 스키마({{output_schema}})가 골든과 다르다 (작업: 골든 → 지금):",
            *changed,
            "출력 모델(schemas/ 의 클래스·필드·설명)이나 schema_style·CURRENT 가 바뀌었거나, pydantic 이 스키마를 다르게 만든다.",
            "vN 파일이 같아도 모델이 받는 프롬프트가 바뀌고 provenance prompt_sha256(파일 + 렌더된 출력 스키마)도 달라진다 (#240).",
            "1) 의도한 변경인지 확인한다. 지시문과 어긋나면 새 vN 을 만든다 (기존 vN 은 고치지 않는다 — prompt-registry 스킬).",
            f"2) 의도한 변경이면 골든을 갱신해 같은 커밋에 넣는다: {UPDATE_CMD}",
        ]
    )


def test_golden_catches_an_output_model_change(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import Field

    from task_spec import schemas

    class VerifyOverlapOutput(schemas.VerifyOverlapOutput):
        note: str | None = Field(default=None, description="가상 설명")

    monkeypatch.setattr(schemas, "VerifyOverlapOutput", VerifyOverlapOutput)
    monkeypatch.delenv(UPDATE_ENV, raising=False)  # 갱신 모드로 돌려도 바뀐 값을 골든에 쓰지 않게
    with pytest.raises(AssertionError, match=UPDATE_ENV) as ei:
        test_rendered_output_schema_of_current_prompts_is_pinned()
    assert "  - verify_overlap: VerifyOverlapOutput·json·" in str(ei.value)
