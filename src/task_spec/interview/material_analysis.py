"""재료 입구 분석 (#301 PR 3) — 회사 재료(인터뷰)가 빌드 하네스에서 재현되는지 과제 승인 전에 짚는다 (LLM, prompts/analyze_material).

2026-11-24 세션 유지 알파: 재료의 주장('흔한 구현은 운영 조건에서 깨진다')이 하네스에서 재현되는지 아무도 빌드 전에 보지 않아, 재현되지
않는 재료가 과제 승인까지 가고 빌드 마지막 ③ 에서야 드러났다. 분석은 승인을 막지 않는다 — 결과는 과제 검토 표시로만 (LLM 오판으로
담당자 진행이 멈추지 않게). 재현 가능(yes)이고 이름이 템플릿과 맞고 재료가 고른 사유와 같으면 표시하지 않는다.
프롬프트에 CURRENT 가 없으면(평가 전) 부르는 쪽이 건너뛴다 (PromptRegistry.has_current). 공개 재료는 인터뷰를 거치지 않아 여기서 보지
않는다 — 평가(eval/interview/run_material_analysis.py)로 따로 한 번 본다. 공유 승인된 회사 사례(co:)는 다시 쓸 때마다 지금 템플릿으로
본다 (2026-11-25 #308 — 재현 여부가 템플릿 판에 따라 달라지고, #311 전에 등록한 재료는 분석된 적이 없다). 그 재료에는 등록한 인터뷰의
가명화 토큰이 남아 있어 입력은 `InterviewPseudonymizer.shared_json`, 입력 칸은 등록 규칙의 LLM 입력 칸만 (material_block(shared=True)).
"""

from __future__ import annotations

from collections.abc import Sequence
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
from task_spec.schemas.interview_state import ReinforceAnswer, ReinforceQuestion
from task_spec.schemas.pseudo_text import PseudonymizedText
from task_spec.spec.company_incidents import LLM_FIELDS
from task_spec.starter.templates import Template

TASK = "analyze_material"
FLAG = "재료 재현 분석: "  # 과제 검토 표시 머리 (막지 않음)
FAILED = FLAG + "자동 분석 실패 — 빌드에서 재현 여부 확인"
# 재현 가능 판정이어도 빠진 정보가 있으면 내는 표시 머리 — 판정 표시와 갈라 센다 (평가 eval/interview/run_material_analysis.py)
GAPS = FLAG + "재현 가능 판정 · 빠진 정보 확인 필요"
_VERDICT = {"no": "빌드 하네스로 재현 불가 가능성 있음", "unsure": "재현 여부 판단 불가"}
# 보강 질문 (#308 검사 5) — 인터뷰 사례의 빠진 정보를 ⑧ 에서 한 번에 최대 둘 묻는다 (담당자 부담). 모르면 '모름'
MAX_REINFORCE = 2
UNKNOWN = "모름"
REINFORCED = FLAG + "보강 답 반영 — 답 {answered}개 · 모름 {unknown}개"


def material_block(incident: PublicIncident, *, shared: bool = False) -> dict[str, Any]:
    """분석 입력의 재료 칸 — 기전을 따지는 데 쓰는 것만. 제목·조직·출처 같은 식별 정보는 넣지 않는다.

    shared=True(공유 승인된 회사 사례를 다른 인터뷰에서 다시 쓸 때)면 등록 규칙이 LLM 입력으로 승인한 칸만 담는다
    (spec.company_incidents.LLM_FIELDS). notes 는 'LLM 에 보내지 않는 칸'이라 등록 때 연락처·링크도 보지 않는다 — 넣으면 새로
    나가는 글이 생긴다 (#308). '수치 미확인' 같은 메모의 신호는 reviewed=false 검토 표시가 승인자에게 따로 보인다."""
    fields = ("symptom", "root_cause", "naive_fix", "why_naive_fails", "notes")
    block: dict[str, Any] = {
        name: getattr(incident, name) for name in fields if not shared or name in LLM_FIELDS
    }
    f = incident.feature
    if f is not None:
        # 기능형 재료 하위 칸 — 등록 규칙은 LLM_FIELDS 의 "feature" 한 칸으로 하위 전체를 본다 (scan_incident). 위 칸을 더할 때는
        # LLM_FIELDS 안인지 본다 (test_a_shared_block_holds_only_what_the_registration_rule_sends 가 고정)
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
    """과제 검토 표시 — 재현 가능이 아님 · 재현 가능이어도 빠진 정보 · 템플릿에 없는 이름 · 재료가 고른 사유와 다름. 이름 대조는 코드가 한다.

    빠진 정보는 판정과 함께 보인다 (2026-11-25 #308 팩트체크): 세션 유지 재료 분석이 gaps 에 'SELECT·UPDATE 가 커넥션 하나를 쥐는가'를
    냈고, 떼어 둔 결제 대행 표본도 gaps 에 '외부 결제 API 를 하네스가 어떻게 흉내 내는가'를 냈는데 판정이 yes 라 둘 다 숨었다.
    yes 오판은 재료의 사고 대신 처방의 부작용으로 함정이 바뀌어도 빌드가 통과할 수 있어, 승인자가 기전과 빠진 정보를 함께 본다."""
    m = template.manifest
    out: list[str] = []
    params = ", ".join(a.split_params) or "없음"
    gaps = f" · 빠진 정보: {'; '.join(a.gaps)}" if a.gaps else ""
    if a.reproducible != "yes":
        out.append(
            f"{FLAG}{_VERDICT[a.reproducible]} — {a.mechanism} (사유 {a.reason}, 갈림 조절값 {params}){gaps}"
        )
    elif a.gaps:
        out.append(f"{GAPS} — {a.mechanism} (사유 {a.reason}, 갈림 조절값 {params}){gaps}")
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


# ── 보강 질문 (#308 검사 5) ──────────────────────────────────────────


def reinforce_questions(gaps: Sequence[str]) -> list[ReinforceQuestion]:
    """재료 분석이 짚은 빠진 정보 → ⑧ 보강 질문. 첫 판은 gaps 글 그대로, 앞의 MAX_REINFORCE 개만.
    gaps 는 LLM 이 쓴 글이라 부르는 쪽이 가명화 검사(derived)를 거친 것만 넘긴다."""
    return [
        ReinforceQuestion(question_id=f"r{i}", question=gap)
        for i, gap in enumerate(gaps[:MAX_REINFORCE], 1)
    ]


def reinforce_lines(answers: Sequence[ReinforceAnswer]) -> list[dict[str, str]]:
    """재료 분석 입력에 붙이는 보강 문답 (재료 블록의 reinforcement 칸) — 모르면 '모름'."""
    return [{"question": a.question, "answer": a.answer or UNKNOWN} for a in answers]


def reinforce_block(answers: Sequence[ReinforceAnswer]) -> str:
    """기능형 재료(interview_feature_seed) 입력에 붙이는 보강 문답 — 재료를 정리할 때 담당자가 채운 빠진 정보를 함께 본다."""
    lines = ["재료 재현 분석이 짚은 빠진 정보에 담당자가 답한 것 (보강 질문):"]
    for a in answers:
        lines += [f"- 빠진 정보: {a.question}", f"  담당자 답: {a.answer or UNKNOWN}"]
    return "\n".join(lines)


def reinforced_flag(answers: Sequence[ReinforceAnswer]) -> str:
    """보강 답을 넣고 다시 만든 과제의 검토 표시 — 승인자가 문답(인터뷰 상태의 reinforce_answers)을 함께 보게."""
    answered = sum(1 for a in answers if a.answer)
    return REINFORCED.format(answered=answered, unknown=len(answers) - answered)
