from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from task_spec.nodes.pseudonymize import mint_pseudonymized
from task_spec.prompting import FewshotContext, PromptError, PromptRegistry, assemble
from task_spec.prompting.context import Criterion, Example
from task_spec.schemas import ClaimExtractionInput, PseudonymizedText

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
TEXT = "가상 데이터: [ORG_1] 에서 데이터 파이프라인을 설계했다."


def _payload() -> ClaimExtractionInput:
    return ClaimExtractionInput(doc_kind="resume", text=mint_pseudonymized(TEXT))


def test_assemble_renders_all_variables() -> None:
    p = assemble(PROMPTS.get("extract_claims"), _payload(), ontology_version="onto-v2026.11.02")
    joined = "\n".join(m.content for m in p.messages)
    assert TEXT in joined
    assert "{{" not in joined
    assert '"claims"' in joined  # output_schema 가 들어감
    # provenance prompt_sha256 이 덮는 글이 그대로 들어간다 (#240)
    assert PROMPTS.get("extract_claims").output_schema_text in joined
    assert p.model == "opencode:qwen3.8-flash"
    assert p.ontology_version == "onto-v2026.11.02"


def test_providers_fill_ids() -> None:
    class Crit:
        def get(self, keys: list[str]) -> list[Criterion]:
            return [Criterion(id="spec-decisions-claims#1", text="기준")]

    p = assemble(PROMPTS.get("extract_claims"), _payload(), ontology_version="v", criteria=Crit())
    assert p.criteria_ids == ["spec-decisions-claims#1"]
    assert p.fewshot_ids == []  # v1 은 fewshot 없음


def test_plain_str_cannot_enter_input() -> None:
    with pytest.raises(ValidationError):
        ClaimExtractionInput(doc_kind="resume", text=TEXT)  # type: ignore[arg-type]


def test_pseudonymized_text_cannot_be_constructed_directly() -> None:
    with pytest.raises(TypeError):
        PseudonymizedText(TEXT)


def test_model_construct_bypass_is_rejected() -> None:
    sneaky = ClaimExtractionInput.model_construct(doc_kind="resume", text=TEXT)
    with pytest.raises(ValidationError):
        assemble(PROMPTS.get("extract_claims"), sneaky, ontology_version="v")


def test_eval_mode_requires_exclusions() -> None:
    with pytest.raises(PromptError, match="exclude_positions"):
        assemble(
            PROMPTS.get("extract_claims"),
            _payload(),
            ontology_version="v",
            context=FewshotContext(mode="eval"),
        )
    assemble(
        PROMPTS.get("extract_claims"),
        _payload(),
        ontology_version="v",
        context=FewshotContext(mode="eval", exclude_positions=frozenset({"pos-1"})),
    )


def test_wrong_input_model_rejected() -> None:
    from task_spec.schemas import ClaimExtractionOutput

    with pytest.raises(PromptError):
        assemble(PROMPTS.get("extract_claims"), ClaimExtractionOutput(), ontology_version="v")


def test_example_type_unused_warning_free() -> None:
    assert Example(id="e", text="t").id == "e"
