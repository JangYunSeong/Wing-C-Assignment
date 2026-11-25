"""재료 입구 분석 (#301 PR 3) — 회사 재료(인터뷰)가 빌드 하네스에서 재현되는지 과제 승인 전에 짚는다 (LLM, prompts/analyze_material).

2026-11-24 세션 유지 알파: 재료의 주장('흔한 구현은 운영 조건에서 깨진다')이 하네스에서 재현되는지 아무도 빌드 전에 보지 않아, 재현되지
않는 재료가 과제 승인까지 가고 빌드 마지막 ③ 에서야 드러났다. 분석은 승인을 막지 않는다 — 결과는 과제 검토 표시로만 (LLM 오판으로
담당자 진행이 멈추지 않게). 재현 가능(yes)이고 이름이 템플릿과 맞고 재료가 고른 사유와 같으면 표시하지 않는다.
프롬프트에 CURRENT 가 없으면(평가 전) 부르는 쪽이 건너뛴다 (PromptRegistry.has_current). 공개 재료는 인터뷰를 거치지 않아 여기서 보지
않는다 — 평가(eval/interview/run_material_analysis.py)로 따로 한 번 본다.
"""

from __future__ import annotations

from typing import Any

from task_spec.interview.task import template_json
from task_spec.llm import LLMClient
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    AnalyzeMaterialInput,
    FeatureSeed,
    MaterialAnalysis,
    Provenance,
    PublicIncident,
)
from task_spec.schemas.pseudo_text import PseudonymizedText
from task_spec.starter.templates import Template

TASK = "analyze_material"
FLAG = "재료 재현 분석: "  # 과제 검토 표시 머리 (막지 않음)
FAILED = FLAG + "자동 분석 실패 — 빌드에서 재현 여부 확인"
_VERDICT = {"no": "빌드 하네스로 재현되지 않을 수 있음", "unsure": "재현 여부 판단 불가"}


def material_block(incident: PublicIncident) -> dict[str, Any]:
    """분석 입력의 재료 칸 — 기전을 따지는 데 쓰는 것만. 제목·조직·출처 같은 식별 정보는 넣지 않는다."""
    block: dict[str, Any] = {
        "symptom": incident.symptom,
        "root_cause": incident.root_cause,
        "naive_fix": incident.naive_fix,
        "why_naive_fails": incident.why_naive_fails,
        "notes": incident.notes,
    }
    f = incident.feature
    if f is not None:
        block |= {
            "naive_impl": f.naive_impl,
            "trigger": f.trigger,
            "why_naive_breaks": f.why_naive_breaks,
            "hidden_requirement": f.hidden_requirement,
            "accepted_designs": f.accepted_designs,
            "reasons": f.reasons,
            "harness": f.harness,
            "harness_notes": f.harness_notes,
        }
    return block


async def analyze_material(
    material: PseudonymizedText,
    template: Template,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
) -> tuple[MaterialAnalysis, Provenance]:
    """material 은 가명화한 재료 블록 (material_block 을 저장본 검사로 감싼 것 — 불변식 2)."""
    result = await llm.call(
        assemble(
            prompts.get(TASK),
            AnalyzeMaterialInput(material=material, template_json=template_json(template)),
            ontology_version=ontology_version,
        ),
        MaterialAnalysis,
    )
    return result.parsed, result.provenance


def analysis_flags(a: MaterialAnalysis, template: Template, seed: FeatureSeed | None) -> list[str]:
    """과제 검토 표시 — 재현 가능이 아님 · 템플릿에 없는 이름 · 재료가 고른 사유와 다름. 이름 대조는 코드가 한다."""
    m = template.manifest
    out: list[str] = []
    if a.reproducible != "yes":
        params = ", ".join(a.split_params) or "없음"
        gaps = f" · 빠진 정보: {'; '.join(a.gaps)}" if a.gaps else ""
        out.append(
            f"{FLAG}{_VERDICT[a.reproducible]} — {a.mechanism} (사유 {a.reason}, 갈림 조절값 {params}){gaps}"
        )
    unknown = sorted(
        {
            *([a.reason] if a.reason not in m.reason_codes else []),
            *(p for p in a.split_params if m.param(p) is None),
        }
    )
    if unknown:
        out.append(f"{FLAG}템플릿에 없는 이름 {', '.join(unknown)} — 분석 결과 확인 필요")
    elif seed is not None and seed.reasons and a.reason not in seed.reasons:
        out.append(
            f"{FLAG}재료 사유({', '.join(seed.reasons)})와 분석 사유({a.reason}) 불일치 — {a.mechanism}"
        )
    return out
