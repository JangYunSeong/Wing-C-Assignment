"""네 요소 조립: 지시문 + 판단 기준 + 예시 + 입력 (prompt-registry §2).

- 입력은 템플릿 머리말의 input_schema 모델로 검증한다. 지원자 텍스트 필드는 PseudonymizedText 만 통과한다.
- 평가 모드에서 exclude_positions 가 비어 있으면 거부한다 (정답 누설 방지).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from task_spec.prompting.context import (
    CriteriaProvider,
    EmptyCriteria,
    EmptyFewshot,
    FewshotContext,
    FewshotProvider,
)
from task_spec.prompting.registry import _VAR, PromptError, PromptTemplate


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: Literal["system", "user"]
    content: str


class AssembledPrompt(BaseModel):
    """llm.call() 에 넘기는 것. 메시지 본문은 로그·provenance 에 넣지 않는다."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    template: PromptTemplate
    messages: list[Message]
    params: dict[str, Any]
    fewshot_ids: list[str]
    criteria_ids: list[str]
    ontology_version: str

    @property
    def model(self) -> str:
        return self.template.meta.model

    @property
    def output_model(self) -> type[BaseModel]:
        return self.template.output_model


def _render(text: str, values: dict[str, str]) -> str:
    return _VAR.sub(lambda m: values[m.group(1)], text)


def assemble(
    template: PromptTemplate,
    payload: BaseModel,
    *,
    ontology_version: str,
    context: FewshotContext | None = None,
    criteria: CriteriaProvider | None = None,
    fewshot: FewshotProvider | None = None,
) -> AssembledPrompt:
    ctx = context or FewshotContext()
    if ctx.mode == "eval" and not ctx.exclude_positions:
        raise PromptError("평가 모드에서는 exclude_positions 가 필요합니다 (정답 누설 방지)")

    if not isinstance(payload, template.input_model):
        raise PromptError(f"{template.ref}: 입력은 {template.meta.input_schema} 여야 합니다")
    # 재검증: 일반 str 이 섞여 들어온 경우를 다시 막는다 (model_construct 우회 방지)
    template.input_model.model_validate(dict(payload))

    crit = (criteria or EmptyCriteria()).get(template.meta.criteria)
    spec = template.meta.fewshot
    examples = (
        (fewshot or EmptyFewshot()).search(spec.source, spec.k, spec.match, ctx) if spec else []
    )

    values: dict[str, str] = {k: str(v) for k, v in dict(payload).items()}
    values["criteria"] = "\n".join(f"- {c.text}" for c in crit) or "(없음)"
    values["examples"] = "\n\n".join(e.text for e in examples) or "(없음)"
    # provenance prompt_sha256 이 덮는 글과 같은 것 (PromptTemplate.output_schema_text — #240)
    values["output_schema"] = template.output_schema_text

    return AssembledPrompt(
        template=template,
        messages=[
            Message(role="system", content=_render(template.system, values)),
            Message(role="user", content=_render(template.user, values)),
        ],
        params=dict(template.meta.params),
        fewshot_ids=[e.id for e in examples],
        criteria_ids=[c.id for c in crit],
        ontology_version=ontology_version,
    )
