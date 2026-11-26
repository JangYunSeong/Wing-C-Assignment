"""확인된 사례 → 공고의 기능형 공통 과제 (#266 D2a). 포크 interview_coding.py·generate·questions_for 를 옮겼다.

포크와 다른 점:
- 코딩 템플릿은 직군 기본 템플릿(TEMPLATE_FOR_SUB) 하나 — TaskItem 에 template_id·feature_seed 를 더하지 않고, 기능형 재료는
  사례의 feature 칸에 둔다(coding_incident). D2b 승인 때 이 사례를 co: 로 등록하면 빌더가 지금 방식대로 DB 에서 재료를 찾는다.
- 기준 답안은 인터뷰 공고 자신의 기능형 생성 입력(seed_block — DraftContext.with_known_answers)에만 들어가고,
  지원자에게 보이는 글(확장 슬롯·짚은 질문의 답 포함)로 새는지 결정적으로 검사한다. 보이는 글에 남은 가명화 토큰도 표시한다.
- 무거운 생성기(build_common → draft_feature_task → 요청 글 → 슬롯)는 TaskMaker 로 묶어 테스트에서 갈아 끼운다.
  단계(feature·request·slots)로 나눠 두어 끊긴 단계부터 이을 수 있다 (#280).
- 코딩 템플릿이 없는 직군은 사례형(incident) — 명세가 곧 과제다 (2026-11-26 사용자). 공고 경로(nodes/spec.py)처럼
  build_common(task_kind="incident") → draft_task, 요청 글(문제 분석 판)은 기능형만이고 슬롯은 같다.
  코딩 재료·재현 분석·함정 틀·빌드는 없다.

오류 구분: 고른 역량·직군으로는 만들 수 없음 = TaskGenerationFailed (422, 선택을 바꿔야 함),
모델 출력을 쓰지 못함 = OutputRejected·LLM 오류 (503, 다시 시도). 메시지는 둘 다 이 모듈의 고정 문구다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import yaml

from task_spec.interview.errors import OutputRejected
from task_spec.interview.frame_leaks import FLAG_FRAME_LEAK, frame_leak_flags
from task_spec.llm import LLMClient
from task_spec.nodes.pseudonymize.detectors import TOKEN_RE
from task_spec.nodes.pseudonymize.interview import has_contact_pattern
from task_spec.ontology.queries import evidence
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    CompetencyEvidence,
    FeatureSeed,
    MaterialInterface,
    PostingCompetency,
    PostingProfile,
    Provenance,
    PublicIncident,
    TaskItem,
    TaskKind,
    TrapFrame,
)
from task_spec.schemas.interview_incident import InterviewFeatureSeedInput
from task_spec.schemas.interview_state import ANSWER_MAX, GeneralQuestion, TargetedQuestion
from task_spec.schemas.pseudo_text import PseudonymizedText
from task_spec.schemas.template import TemplateManifest
from task_spec.spec.analysis_request import apply_analysis, draft_analysis_request
from task_spec.spec.build import build_common
from task_spec.spec.common import (
    FLAG_COMPANY_INCIDENT,
    FLAG_COMPANY_INCIDENT_LEGACY,
    plan_common,
)
from task_spec.spec.draft import DraftContext, apply, draft_task
from task_spec.spec.feature import apply_feature, draft_feature_task, without_public
from task_spec.spec.fix_markers import marker_note, split_markers
from task_spec.spec.incidents import IncidentCatalog
from task_spec.spec.slots import build_slots
from task_spec.starter.templates import TEMPLATE_FOR_SUB, Template, load_template

FEATURE_SEED_PROMPT = "interview_feature_seed"
TARGETED_PROMPT = "interview_targeted_answer"
FLAG_ANSWER_LEAK = (
    "지원자에게 표시하는 글이 기준 답안 문장을 포함함 — 채점 근거 누설이 없는지 승인자 확인 필요"
)
FLAG_TOKEN_VISIBLE = "지원자에게 표시하는 글에 가명화 토큰([ORG_1] 같은)이 있음 — 지원자에게 그대로 표시하므로 소재를 변경하거나 삭제해 주세요"
FLAG_CONTACT_PATTERN = (
    "답할 범위 초안에 쓰는 과제 글에 링크·연락처 형식이 있음 — URL·이메일, 전화번호처럼 보이는 숫자(10자리 타임스탬프 포함)를"
    " 지우거나 형식이 아닌 값(예: <이미지 주소>)으로 바꿔 주세요"
)
FLAG_INTERVIEW_INCIDENT = "채용담당자 인터뷰 사례 기반 — 이 공고 담당자가 확인한 사례(공유 승인 전). 원리만 차용하고 소재는 변경함, 승인자 검토 필요"
# 글이 바뀔 때마다 다시 계산하는 표시 — 함정 틀 공개 중립성(#318 검사 3)도 과제 글에서 계산한다
_COMPUTED_FLAGS = (FLAG_ANSWER_LEAK, FLAG_TOKEN_VISIBLE, FLAG_FRAME_LEAK, FLAG_CONTACT_PATTERN)
# 답할 범위 초안(TARGETED_PROMPT)이 과제에서 LLM 에 다시 보내는 칸 — 초안 입력과 형식 표시가 같은 목록을 쓴다
TARGETED_TASK_FIELDS: dict[str, Any] = {
    "scenario": True,
    "requirements": {"__all__": {"text"}},
    "facts": {"__all__": {"topic", "ask_when", "answer", "answer_hint"}},
    "clarifications": {"__all__": {"topic", "ask_when", "match_terms", "answer", "answer_hint"}},
    "artifacts": {"__all__": {"title", "content"}},
}
# 표시에 항목을 적을 때만 더하는 id 칸 — 초안 입력에는 넣지 않는다 (답에 자료 id 를 쓰지 않게, mentions_artifact_id)
_TARGETED_ITEM_IDS = {
    "requirements": "req_id",
    "facts": "fact_id",
    "clarifications": "clarification_id",
    "artifacts": "artifact_id",
}
# 2026-11-23 문구를 바꾸기 전 글 — 이미 저장된 인터뷰 과제의 옛 표시도 다시 계산할 때 지운다 (새로 붙이지 않는다)
_COMPUTED_FLAGS_LEGACY = (
    "기준 답안 문장이 지원자에게 보이는 글에 들어감 — 채점 근거가 새지 않았는지 승인자 확인 필요",
    "지원자에게 보이는 글에 가명화 토큰([ORG_1] 같은)이 있음 — 지원자에게 그대로 보이므로 소재를 바꾸거나 지워 주세요",
)
_LEAK_WINDOW = 14  # 기준 답안의 이 길이(공백·문장부호 뺀 글자) 이상이 그대로 보이면 샌 것으로 본다
_SECTION_HEAD = re.compile(
    r"(?m)^\s*\[(?!(?:PERSON|CONTACT|LINK|SCHOOL|ORG|PRODUCT|COURSE)_\d+\])[^\]]+\]\s*"
)
_ARTIFACT_ID = re.compile(r"(?<![A-Za-z0-9])[aA][0-9]+(?![A-Za-z0-9])")
# 지원자에게 보이지 않는 칸 — 채점·승인용이거나 id·열거값. 그 밖의 글은 전부 지원자에게 보이거나 물으면 주는 글로 본다
# (확장 슬롯·2단계 디버깅·면접 확인 질문·연결 문구 포함. 새 칸이 생기면 기본은 검사 대상)
_HIDDEN_KEYS = frozenset(
    (
        # 과제 뼈대 (결정적 값·id·열거값)
        "task_id parent_task_id slot_id track task_kind target_competencies ai_axes claims_to_verify"
        " tech_env difficulty_target constraints source_incident provenance_id review_flags request_mentions"
        # 채점·승인용 (숨은 요구와 그 답)
        " invariant variable traps observables behavioral_anchors acceptance_hidden accepted_designs"
        # 항목 안의 id·참조
        " artifact_id role req_id req_ids ac_id fact_id covers clarification_id reveals trap_ids reveal_after"
    ).split()
)
# 목록 항목의 id 칸 — 표시에 항목을 적을 때와, 고친 목록을 예전 항목과 짝지을 때 쓴다
_ID_KEYS = (
    "task_id",
    "slot_id",
    "artifact_id",
    "req_id",
    "ac_id",
    "fact_id",
    "clarification_id",
    "trap_id",
    "design_id",
    "question_id",
)


class TaskGenerationFailed(ValueError):
    """고른 역량·직군으로는 과제를 만들 수 없다 (템플릿 없음·근거 부족·역량 누락·확인된 답안 없음) — 다시 시도가 아니라
    선택을 바꿔야 한다 (422). 메시지는 고정 문구라 화면에 그대로 보여도 된다."""


def task_kind_for(sub: str) -> TaskKind:
    """인터뷰 과제 종류 — 직군 기본 코딩 템플릿이 있으면 기능형, 없으면 사례형(명세가 곧 과제, 2026-11-26 사용자).
    인터뷰를 시작할 때 정해 상태에 둔다 — 예전에는 인터뷰를 다 한 뒤 ⑦ 역량 고르기에서야 '템플릿 없음'으로 막혔다."""
    return "feature" if sub in TEMPLATE_FOR_SUB else "incident"


def interview_template(kind: TaskKind, sub: str) -> Template | None:
    """과제 종류의 템플릿 — 기능형은 직군 기본 코딩 템플릿(읽지 못하면 TaskGenerationFailed), 사례형은 없다."""
    return coding_template(sub) if kind == "feature" else None


def coding_template(sub: str) -> Template:
    """기능형 과제의 직군 기본 코딩 템플릿 (포크는 세 템플릿 중 고르게 했다). 없는 직군은 task_kind_for 가 사례형으로 정하므로
    여기까지 오지 않는다 — 오면 기능형으로는 만들 수 없다는 뜻이다."""
    template_id = TEMPLATE_FOR_SUB.get(sub)
    if template_id is None:
        raise TaskGenerationFailed(
            "이 직군에는 코딩 과제 템플릿이 없어 인터뷰 과제를 만들 수 없습니다"
        )
    try:
        return load_template(template_id)
    except (
        OSError,
        ValueError,
        yaml.YAMLError,
    ):  # 묶음에 템플릿이 없거나 깨짐 — 경로·내용은 싣지 않는다
        raise TaskGenerationFailed(
            "직군 기본 코딩 템플릿을 읽지 못했습니다. 관리자에게 알려 주세요"
        ) from None


def template_json(template: Template) -> str:
    m = template.manifest
    return json.dumps(
        {
            "template_id": m.template_id,
            "interfaces": m.interfaces,
            "contract_guide": m.contract_guide,
            "stack": m.stack.model_dump() if m.stack else None,
            "failure_reasons": [r.model_dump() for r in m.failure_reasons],
            "harness_params": [p.model_dump() for p in m.harness_params],
        },
        ensure_ascii=False,
    )


async def prepare_coding_incident(
    incident: PublicIncident,
    template: Template,
    *,
    interview: PseudonymizedText,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
) -> tuple[PublicIncident, list[Provenance], list[str]]:
    """확인된 사례·기준 답안 → 템플릿이 시험할 수 있는 기능형 재료와 코드가 고친 것(검토 표시). interview 는 가명화 모듈이 만든 사례 블록.
    코드가 풀 수 있는 것은 고친다 (설계 검사 #332 S-F2 — 예전에는 모두 OutputRejected 로 멈추고 '이어서 하기'가 같은 호출을 되풀이):
    템플릿 밖 실패 사유가 섞였어도 맞는 사유가 남으면 그것만, 모르는 조절값 이름은 뺀다 (템플릿 필수 조절값과 합쳐지므로 필수는 남는다).
    맞는 사유가 하나도 없거나 조절값 칸을 비워 보내면(None = 템플릿 필수값 — 성능 사례가 아니면 풀 크기 같은 값을 잘못 요구해 계약이
    막힌다, 2026-11-10) OutputRejected (다시 시도)."""
    if not incident.known_incident_cases or any(
        not case.answer_text.strip() for case in incident.known_incident_cases
    ):
        raise TaskGenerationFailed("인터뷰 사례와 확인된 기준 답안이 필요합니다")
    manifest = template.manifest
    result = await llm.call(
        assemble(
            prompts.get(FEATURE_SEED_PROMPT),
            InterviewFeatureSeedInput(interview=interview, template_json=template_json(template)),
            ontology_version=ontology_version,
        ),
        FeatureSeed,
    )
    seed, notes = fit_seed_to_template(result.parsed, manifest)
    seed = seed.model_copy(update={"reviewed": False, "starter": None, "task_shape": "code"})
    return (
        incident.model_copy(update={"feature": seed, "templates": [manifest.template_id]}),
        [result.provenance],
        notes,
    )


FLAG_SEED_FIT = "기능형 재료 맞춤: "  # 코드가 템플릿에 맞춰 고친 것 (막지 않음)


def fit_seed_to_template(
    seed: FeatureSeed, manifest: TemplateManifest
) -> tuple[FeatureSeed, list[str]]:
    """LLM 재료를 템플릿이 시험할 수 있게 — 고칠 수 없으면 OutputRejected. 고친 것은 검토 표시 문장으로 (prepare_coding_incident)."""
    codes = {r.code for r in manifest.failure_reasons}
    params = {p.name for p in manifest.harness_params}
    update: dict[str, Any] = {}
    notes: list[str] = []
    if outside := [r for r in seed.reasons if r not in codes]:
        kept = [r for r in seed.reasons if r in codes]
        if not kept:  # 비우면 '템플릿 사유 전체'가 되어 뜻이 바뀐다
            raise OutputRejected("선택한 코딩 환경에서 검증할 수 없는 실패 조건입니다")
        update["reasons"] = kept
        notes.append(f"{FLAG_SEED_FIT}템플릿에 없는 실패 사유 {', '.join(outside)} 를 뺌")
    if seed.harness is None:
        raise OutputRejected("검증에 필요한 실행 조건을 코딩 환경과 맞춰 주세요")
    if unknown := [h for h in seed.harness if h not in params]:
        update["harness"] = [h for h in seed.harness if h in params]
        notes.append(f"{FLAG_SEED_FIT}템플릿에 없는 조절값 {', '.join(unknown)} 를 뺌")
    # 공개된 업무 요구에 나온 말은 공개 쪽 금지 낱말이 될 수 없다 — 두면 명세 검사가 고칠 수 없는 재요청을 낸다 (설계 검사 #332 S-F6.
    # 프롬프트 지시만으로 막고 있었다). 낱말 목록은 이 재료를 쓰는 모든 검사가 그대로 받는다
    for name in ("leak_terms", "stage2_cause_terms", "trigger_terms"):
        terms = list(getattr(seed, name))
        kept_terms = without_public(terms, seed.business_need)
        if dropped := [t for t in terms if t not in kept_terms]:
            update[name] = kept_terms
            notes.append(
                f"{FLAG_SEED_FIT}업무 요구에 나온 말을 {name} 에서 뺌 — {', '.join(dropped)}"
            )
    # 정답 장치 표지는 코드 꼴만 (#337 1절) — v1 은 이 칸을 설명하지 않아 문장을 썼고, 문장은 코드에 걸리지 않아 ③ 의 무력화 판정이
    # 조용히 꺼졌다. 막지 않는다 (출력 모델 검증으로 막으면 형식 재시도 한 번 뒤 재료 생성 전체가 실패)
    markers, bad_markers = split_markers(seed.fix_markers)
    if bad_markers:
        update["fix_markers"] = markers
        notes.append(f"{FLAG_SEED_FIT}{marker_note(len(bad_markers), len(markers))}")
    return (seed.model_copy(update=update) if update else seed), notes


def ready_coding_incident(incident: PublicIncident, template: Template) -> PublicIncident:
    """공개·회사 사례 (D2c) — 기능형 재료가 이미 있고 이 직군 템플릿이 시험할 수 있어야 한다 (LLM 없이 그대로, 포크 public_fixed 와 같음).
    기준 답안은 싣지 않는다 — 다른 공고의 인터뷰 사례를 쓰더라도 채점 근거가 생성 입력으로 가지 않게 (회사 사례는 등록 때 이미 뺀다)."""
    if incident.feature is None or template.manifest.template_id not in incident.templates:
        raise TaskGenerationFailed(
            "이 사례에는 직군 코딩 템플릿으로 시험할 기능형 재료가 없습니다. 다른 사례를 골라 주세요"
        )
    return incident.model_copy(update={"known_incident_cases": []})


def questions_for(task: TaskItem) -> tuple[list[GeneralQuestion], list[TargetedQuestion]]:
    """자료에 걸린 일반 질문 2개와, 숨은 조건마다 담당자가 답할 범위를 정할 짚은 질문 (포크 그대로)."""
    artifacts = task.artifacts
    if not artifacts:
        return [], []
    keys = [a for a in artifacts if a.role == "key"]
    others = [a for a in artifacts if a.role != "key"]
    groups = [[*(keys[i : i + 1] or keys[:1]), *others[i * 2 : i * 2 + 2]] for i in range(2)]
    general = [
        GeneralQuestion(question=q, artifact_ids=[a.artifact_id for a in group])
        for q, group in zip(
            ("원인을 어떻게 찾나요?", "어떤 자료를 먼저 봐야 하나요?"), groups, strict=True
        )
        if group
    ]
    hidden = [o.value for o in task.observables if o.kind == "first_mention_of"]
    if not hidden:
        hidden = [t.description for t in task.traps[:1]]
    hidden = [h[:ANSWER_MAX] for h in hidden]
    targeted = [
        TargetedQuestion(
            question_id=f"q{i}",
            question=f"{condition}에 대해 지원자가 직접 묻는다면 어떤 범위까지 알려주어야 하나요?",
            hidden_condition=condition,
        )
        for i, condition in enumerate(hidden[:20], 1)
    ]
    return general, targeted


def hidden_requirements(task: TaskItem, targeted: Iterable[TargetedQuestion]) -> list[str]:
    return [x for x in (task.invariant, *(q.hidden_condition for q in targeted)) if x]


def evaluation_criteria(task: TaskItem) -> list[str]:
    """평가 기준 = 행동 기준 수준 1·3·5 (과제를 고치면 다시 만든다)."""
    anchors = task.behavioral_anchors
    if anchors is None:
        return []
    return [f"수준 {level}: {getattr(anchors, f'level_{level}').behavior}" for level in (1, 3, 5)]


def _item_id(item: Any) -> tuple[str, Any] | None:
    if isinstance(item, dict):
        for key in _ID_KEYS:
            if item.get(key) is not None:
                return (key, item[key])
    return None


def visible_texts(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """지원자에게 보이거나 물으면 주는 글 — (칸 경로, 글). 과제 JSON 을 재귀로 돌며 채점·승인용 칸(_HIDDEN_KEYS)만 뺀다.
    JSON 문자열이 아니라 값을 보므로 줄바꿈·따옴표 이스케이프가 끼어도 놓치지 않는다."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in _HIDDEN_KEYS:
                yield from visible_texts(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            ident = _item_id(item)
            yield from visible_texts(item, f"{path}[{ident[1] if ident else i}]")


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", _SECTION_HEAD.sub("", text))


def _windows(text: str) -> set[str]:
    body = _norm(text)
    return {body[i : i + _LEAK_WINDOW] for i in range(len(body) - _LEAK_WINDOW + 1)}


def _answer_windows(incident: PublicIncident) -> set[str]:
    return set().union(*(_windows(case.answer_text) for case in incident.known_incident_cases))


def _shown(task: TaskItem, targeted: Iterable[TargetedQuestion]) -> list[tuple[str, str]]:
    """과제의 보이는 글 + 짚은 질문의 답(지원자가 정확히 물으면 그대로 준다). 질문·숨은 조건 글은 담당자용이라 뺀다."""
    return [
        *visible_texts(task.model_dump(mode="json")),
        *((f"targeted_questions[{q.question_id}].answer", q.answer) for q in targeted if q.answer),
    ]


def answer_leak_flags(
    task: TaskItem, incident: PublicIncident, targeted: Iterable[TargetedQuestion] = ()
) -> list[str]:
    """기준 답안(채점 근거) 문장이 지원자에게 보이는 글에 그대로 들어갔는지 — 프롬프트 지시만 믿지 않는다.

    기준 답안을 공백·문장부호를 빼고 _LEAK_WINDOW 글자씩 잘라, 같은 조각이 보이는 글에 있으면 그 칸을 표시한다.
    """
    windows = _answer_windows(incident)
    if not windows:
        return []
    leaked = [name for name, text in _shown(task, targeted) if windows & _windows(text)]
    return [f"{FLAG_ANSWER_LEAK} ({', '.join(leaked[:5])})"] if leaked else []


def token_flags(task: TaskItem, targeted: Iterable[TargetedQuestion] = ()) -> list[str]:
    """보이는 글의 가명화 토큰 — 공통 과제는 지원자에게 그대로 나가고 되돌리는 단계가 없다."""
    found = [name for name, text in _shown(task, targeted) if TOKEN_RE.search(text)]
    return [f"{FLAG_TOKEN_VISIBLE} ({', '.join(found[:5])})"] if found else []


def targeted_draft_texts(
    task: TaskItem, targeted: Iterable[TargetedQuestion]
) -> list[tuple[str, str]]:
    """답할 범위 초안이 LLM 에 다시 보내는 글 — (칸 경로, 글). 과제 칸(TARGETED_TASK_FIELDS)과 짚은 질문의 숨은 조건."""
    include = {
        key: fields
        if fields is True
        else {"__all__": {*fields["__all__"], _TARGETED_ITEM_IDS[key]}}
        for key, fields in TARGETED_TASK_FIELDS.items()
    }
    return [
        *visible_texts(task.model_dump(mode="json", include=include)),
        *(
            (f"targeted_questions[{q.question_id}].hidden_condition", q.hidden_condition)
            for q in targeted
        ),
    ]


def contact_pattern_flags(task: TaskItem, targeted: Iterable[TargetedQuestion] = ()) -> list[str]:
    """답할 범위 초안 입력 중 연락처·링크 형식(가명화 `derived` 와 같은 판정)이 있는 칸. 가명화 입력으로 만든 과제라도 LLM 이
    예시 값(이미지 URL·이메일·타임스탬프)을 지어 쓴다 — 그대로 두면 초안 입력 검사가 같은 글을 매번 거절한다.
    다른 보이는 글(확장 슬롯·2단계)의 예시 값은 다시 LLM 에 가지 않고 지어낸 값이라 표시하지 않는다."""
    found = [
        path for path, text in targeted_draft_texts(task, targeted) if has_contact_pattern(text)
    ]
    return [f"{FLAG_CONTACT_PATTERN} ({', '.join(found[:5])})"] if found else []


def with_content_flags(
    flags: Iterable[str],
    task: TaskItem,
    incident: PublicIncident,
    targeted: Iterable[TargetedQuestion] = (),
    frame: TrapFrame | None = None,
) -> list[str]:
    """글에서 다시 계산하는 표시(기준 답안 누설·토큰·함정 틀 공개 중립성·답 초안 입력 형식)를 지금 글 기준으로 바꾼다 —
    나머지 표시는 그대로.
    frame: 이 공고의 함정 틀 (#318 — 틀이 없거나 예전 과제면 None, 그때는 공개 중립성 표시가 없다)."""
    shown = list(targeted)
    kept = [f for f in flags if not f.startswith((*_COMPUTED_FLAGS, *_COMPUTED_FLAGS_LEGACY))]
    return list(
        dict.fromkeys(
            [
                *kept,
                *answer_leak_flags(task, incident, shown),
                *token_flags(task, shown),
                *frame_leak_flags(task, frame, incident.feature),
                *contact_pattern_flags(task, shown),
            ]
        )
    )


def copies_answer(text: str, incident: PublicIncident) -> bool:
    return bool(_answer_windows(incident) & _windows(text))


def with_interview_flag(task: TaskItem, incident: PublicIncident) -> TaskItem:
    """생성기는 사례 id 가 co: 이면 '공유 승인된 회사 사례' 표시를 붙인다 — 인터뷰 사례는 아직 공유 승인 전이라
    공통 과제 자신의 표시만 바꾼다 (확장 슬롯이 쓴 다른 회사 사례의 표시는 맞는 말이라 그대로)."""
    if task.source_incident != incident.incident_id:
        return task
    company = (FLAG_COMPANY_INCIDENT, FLAG_COMPANY_INCIDENT_LEGACY)  # 생성 중에 문구가 바뀐 초안도
    flags = [FLAG_INTERVIEW_INCIDENT if f in company else f for f in task.review_flags]
    return task.model_copy(update={"review_flags": list(dict.fromkeys(flags))})


def task_frame(task: TaskItem) -> tuple[Any, ...]:
    """담당자가 과제를 고쳐도 바뀌면 안 되는 뼈대 — 트랙·유형·측정 역량·사례(승인 때 co: 로 등록)·기술 환경(직군 템플릿)·
    확장 슬롯 구성(지원자 주장을 슬롯 id·역량으로 배정한다)."""
    return (
        task.track,
        task.task_kind,
        [c.model_dump() for c in task.target_competencies],
        task.source_incident,
        task.tech_env.model_dump(),
        [
            (
                s.task_id,
                s.slot_id,
                s.track,
                s.task_kind,
                [c.competency_id for c in s.target_competencies],
            )
            for s in task.slots
        ],
    )


def pseudonymize_changes(new: Any, old: Any, pseudonymize: Callable[[str], str]) -> Any:
    """담당자가 고친 구조(과제·질문 JSON)에서 바뀐 글만 가명화한다 — 그대로인 글은 저장된 가명화 글이라 다시 넣지 않는다
    (다른 문맥에서 탐지기가 새 토큰을 만들어 검토한 글이 바뀌지 않게). 새로 생긴 항목의 글과 새 dict 키는 전부 가명화한다.
    목록 항목에 id 칸(artifact_id·req_id …)이 있으면 그 id 로 예전 항목과 짝짓는다 — 순서만 바꿔도 다시 넣지 않게."""
    if isinstance(new, str):
        return new if new == old else pseudonymize(new)
    if isinstance(new, dict):
        before = old if isinstance(old, dict) else {}
        return {
            (k if k in before else pseudonymize(k)): pseudonymize_changes(
                v, before.get(k), pseudonymize
            )
            for k, v in new.items()
        }
    if isinstance(new, list):
        before_list = old if isinstance(old, list) else []
        by_id = {ident: item for item in before_list if (ident := _item_id(item)) is not None}

        def partner(i: int, item: Any) -> Any:
            ident = _item_id(item)
            if ident is not None:
                return by_id.get(ident)
            return before_list[i] if i < len(before_list) else None

        return [pseudonymize_changes(v, partner(i, v), pseudonymize) for i, v in enumerate(new)]
    return new


def mentions_artifact_id(text: str) -> bool:
    return bool(_ARTIFACT_ID.search(text))


@dataclass(frozen=True)
class CompetencyChoice:
    available: list[PostingCompetency]  # 수행준거 근거가 있는 공고 역량
    matches: list[CompetencyEvidence]
    suggested: list[str]  # 규칙이 고른 공통 과제 역량


@dataclass
class MadeTask:
    task: TaskItem
    provenance: list[Provenance] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskRequest:
    """공통 과제 한 번 만들기의 입력 — 단계마다 같은 값을 넘긴다 (#280: 단계별로 나눠 돌리고 끊긴 단계부터 잇는다).
    profile·matches 는 선택한 역량만 담는다. 템플릿이 없으면 사례형(명세가 곧 과제)이다."""

    profile: PostingProfile
    matches: list[CompetencyEvidence]
    coding_incident: PublicIncident  # 과제의 재료 — 사례형이면 기능형 칸 없는 사례 그대로
    template: Template | None
    task_id: str
    reviewer_note: str | None = None
    # 이 공고의 함정 틀 (#318) — 명세 초안이 공개 쪽을 받는다 (draft_feature_task v13). coding_incident 는 이미 틀 적용 재료다
    frame: TrapFrame | None = None
    # 확장 슬롯은 공고 전체 역량·근거로 — 공통 과제가 다루지 않은 역량을 슬롯이 먼저 확인한다 (spec/slots.plan_slots). 고른 역량만 넘기면
    # 그 규칙이 성립하지 않았다 (설계 검사 #332 S-F18, 사용자 결정 2026-11-26). None 이면 profile·matches 그대로 (예전 호출)
    slot_profile: PostingProfile | None = None
    slot_matches: list[CompetencyEvidence] | None = None

    @property
    def task_kind(self) -> TaskKind:
        return "feature" if self.template is not None else "incident"


class TaskMaker(Protocol):
    """단계: feature → request → slots. 앞 단계의 MadeTask 를 받아 다음 MadeTask 를 돌려준다.
    근거가 모자라거나 역량이 빠지면 TaskGenerationFailed(422), 모델 출력을 못 쓰면 OutputRejected·LLM 오류(503)."""

    async def competencies(self, profile: PostingProfile) -> CompetencyChoice: ...

    async def feature(self, req: TaskRequest) -> MadeTask:
        """공통 과제 뼈대(build_common) → 초안 (기능형 draft_feature_task · 사례형 draft_task). 단계 이름은 API 그대로."""
        ...

    async def request(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        """요청 글 (draft_analysis_request)."""
        ...

    async def slots(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        """확장 슬롯 (build_slots)."""
        ...


def kept_competencies(profile: PostingProfile, matches: list[CompetencyEvidence]) -> list[str]:
    """고른 역량(profile)으로 공통 과제 뼈대가 담을 역량 — 주 역량을 먼저 담고, 보조 역량은 2개를 채울 때만 (spec/common.plan_common).
    역량 선택(⑦)이 같은 규칙으로 미리 본다 (설계 검사 #332 S-F7)."""
    return [c.competency_id for c in plan_common(profile, matches).competencies]


def _require_all_selected(task: TaskItem, req: TaskRequest) -> None:
    if {c.competency_id for c in task.target_competencies} != {
        c.competency_id for c in req.profile.competencies
    }:
        raise TaskGenerationFailed(
            "선택한 역량 일부가 공통 과제에 포함되지 않았습니다. 역량을 다시 골라 주세요"
        )


async def make_task(tasks: TaskMaker, req: TaskRequest) -> MadeTask:
    """세 단계를 차례로 — 한 요청 안에서 끝까지 만들 때."""
    made = await tasks.feature(req)
    made = await tasks.request(req, made)
    return await tasks.slots(req, made)


@dataclass(frozen=True)
class OntologyTaskMaker:
    """실제 생성기 묶음 — 포크 generate 와 같은 순서: build_common → draft_feature_task → 요청 글 → 슬롯."""

    ontology: OntologyRepository
    llm: LLMClient
    prompts: PromptRegistry
    # 재료 id → 재료가 정한 공개 인터페이스 (#256) — 공개 재료로 만드는 인터뷰 과제도 명세가 계약과 같은 형식·칸 이름을 쓰게
    interfaces: Mapping[str, MaterialInterface] = field(default_factory=dict)

    async def competencies(self, profile: PostingProfile) -> CompetencyChoice:
        matches = await evidence(
            self.ontology,
            profile.competency_ids,
            sub=profile.sub or "",
            industry_id=profile.industry_id or "",
            secondary_sub=profile.secondary_sub,
            in_pilot=profile.in_pilot,
        )
        supported = {e.competency_id for e in matches if any(u.n_criteria for u in e.ncs_units)}
        plan = plan_common(profile, matches)
        return CompetencyChoice(
            available=[c for c in profile.competencies if c.competency_id in supported],
            matches=matches,
            suggested=[c.competency_id for c in plan.competencies],
        )

    async def _context(self, req: TaskRequest) -> tuple[TaskItem, DraftContext, list[str]]:
        """공통 과제 뼈대·계산 맥락·표시 — 온톨로지 조회뿐이라 결정적이다. 맥락(DraftContext)은 단계 사이에 넘기지 않고
        단계마다 다시 만든다 (끊긴 단계부터 이을 때 저장해 둘 수 없는 값)."""
        catalog = IncidentCatalog(
            interfaces=self.interfaces
        )  # 그 재료 하나 + 인터페이스 (회사 사례·인터뷰 재료는 없음)
        catalog[req.profile.sub or ""] = req.coding_incident
        built = await build_common(
            req.profile,
            req.matches,
            task_id=req.task_id,
            ontology_version=req.profile.ontology_version,
            ontology=self.ontology,
            incidents=catalog,
            task_kind=req.task_kind,
        )
        task, ctx = built.task, built.ctx
        if task is None or ctx is None:
            raise TaskGenerationFailed("선택 역량의 과제 근거가 부족합니다")
        stack = req.template.manifest.stack if req.template is not None else None
        if stack is not None:
            env = task.tech_env.model_copy(
                update={
                    "languages": ["Java" if stack.language.lower() == "java" else stack.language],
                    "frameworks": [stack.framework] if stack.framework else [],
                    "basis": "직군 기본 코딩 템플릿",
                }
            )
            task = task.model_copy(update={"tech_env": env})
            ctx.env = env
        # 인터뷰 공고 자신의 생성 — 확인된 기준 답안을 기능형 재료 블록에 넣는다 (다른 공고가 이 사례를 쓸 때는 넣지 않는다)
        ctx.with_known_answers = True
        ctx.trap_frame = req.frame
        return task, ctx, built.flags

    async def feature(self, req: TaskRequest) -> MadeTask:
        base, ctx, flags = await self._context(req)
        # 과제 역량은 결정적 뼈대가 정하고 기능형 초안은 바꾸지 않는다 — LLM 전에 본다 (설계 검사 #332 S-F7: 몇 분짜리 초안을 다 돈 뒤에야
        # 거절했고, '이어서 하기'는 같은 선택으로 같은 실패를 되풀이했다)
        _require_all_selected(base, req)
        # 공고 경로(nodes/spec.py)와 같은 갈래 — 사례형은 기준 답안을 넣지 않는 사례 블록(incident_block)으로 만든다
        if req.task_kind == "feature":
            result = await draft_feature_task(
                ctx, llm=self.llm, prompts=self.prompts, reviewer_note=req.reviewer_note
            )
        else:
            result = await draft_task(
                ctx, llm=self.llm, prompts=self.prompts, reviewer_note=req.reviewer_note
            )
        if result.output is None:
            raise OutputRejected("과제 초안을 만들지 못했습니다")
        task = (apply_feature if req.task_kind == "feature" else apply)(base, result)
        if task.behavioral_anchors is None:
            raise OutputRejected("평가 행동 기준이 생성되지 않았습니다")
        _require_all_selected(task, req)
        return MadeTask(task=task, provenance=list(result.provenance), flags=list(flags))

    async def request(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        if (
            req.task_kind != "feature"
        ):  # 요청 글(문제 분석 판)은 기능형 명세 칸 — 공고 경로도 기능형만 (nodes/spec.py)
            return made
        _, ctx, _ = await self._context(req)
        # 반려 사유·팀 맥락(수정 요청)을 요청 글에도 — 일반 파이프라인처럼 세 생성기 모두에 (설계 검사 #332 S-F17: 기능형 초안에만 넘겨
        # 요청 글·사실 때문에 반려해도 다시 만들기에 반영되지 않았다)
        analysis = await draft_analysis_request(
            made.task, ctx, llm=self.llm, prompts=self.prompts, reviewer_note=req.reviewer_note
        )
        return MadeTask(
            task=apply_analysis(made.task, analysis),
            provenance=[*made.provenance, *analysis.provenance],
            flags=list(made.flags),
        )

    async def slots(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        _, ctx, _ = await self._context(req)
        slots = await build_slots(
            req.slot_profile or req.profile,
            req.slot_matches if req.slot_matches is not None else req.matches,
            made.task,
            ctx,
            ontology=self.ontology,
            llm=self.llm,
            prompts=self.prompts,
            reviewer_note=req.reviewer_note,  # 요청 글과 같은 까닭 (설계 검사 #332 S-F17)
        )
        return MadeTask(
            task=made.task.model_copy(update={"slots": slots.slots}),
            provenance=[*made.provenance, *slots.provenance],
            flags=[*made.flags, *slots.flags],
        )
