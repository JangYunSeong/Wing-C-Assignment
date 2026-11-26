"""확인된 사례 → 공고의 기능형 공통 과제 (#266 D2a). 포크 interview_coding.py·generate·questions_for 를 옮겼다.

포크와 다른 점:
- 코딩 템플릿은 직군 기본 템플릿(TEMPLATE_FOR_SUB) 하나 — TaskItem 에 template_id·feature_seed 를 더하지 않고, 기능형 재료는
  사례의 feature 칸에 둔다(coding_incident). D2b 승인 때 이 사례를 co: 로 등록하면 빌더가 지금 방식대로 DB 에서 재료를 찾는다.
- 기준 답안은 인터뷰 공고 자신의 기능형 생성 입력(seed_block — DraftContext.with_known_answers)에만 들어가고,
  지원자에게 보이는 글(확장 슬롯·짚은 질문의 답 포함)로 새는지 결정적으로 검사한다. 보이는 글에 남은 가명화 토큰도 표시한다.
- 무거운 생성기(build_common → draft_feature_task → 요청 글 → 슬롯)는 TaskMaker 로 묶어 테스트에서 갈아 끼운다.
  단계(feature·request·slots)로 나눠 두어 끊긴 단계부터 이을 수 있다 (#280).

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
    TrapFrame,
)
from task_spec.schemas.interview_incident import InterviewFeatureSeedInput
from task_spec.schemas.interview_state import ANSWER_MAX, GeneralQuestion, TargetedQuestion
from task_spec.schemas.pseudo_text import PseudonymizedText
from task_spec.spec.analysis_request import apply_analysis, draft_analysis_request
from task_spec.spec.build import build_common
from task_spec.spec.common import (
    FLAG_COMPANY_INCIDENT,
    FLAG_COMPANY_INCIDENT_LEGACY,
    plan_common,
)
from task_spec.spec.draft import DraftContext
from task_spec.spec.feature import apply_feature, draft_feature_task
from task_spec.spec.incidents import IncidentCatalog
from task_spec.spec.slots import build_slots
from task_spec.starter.templates import TEMPLATE_FOR_SUB, Template, load_template

FEATURE_SEED_PROMPT = "interview_feature_seed"
TARGETED_PROMPT = "interview_targeted_answer"
FLAG_ANSWER_LEAK = (
    "지원자에게 표시하는 글이 기준 답안 문장을 포함함 — 채점 근거 누설이 없는지 승인자 확인 필요"
)
FLAG_TOKEN_VISIBLE = "지원자에게 표시하는 글에 가명화 토큰([ORG_1] 같은)이 있음 — 지원자에게 그대로 표시하므로 소재를 변경하거나 삭제해 주세요"
FLAG_INTERVIEW_INCIDENT = "채용담당자 인터뷰 사례 기반 — 이 공고 담당자가 확인한 사례(공유 승인 전). 원리만 차용하고 소재는 변경함, 승인자 검토 필요"
# 글이 바뀔 때마다 다시 계산하는 표시 — 함정 틀 공개 중립성(#318 검사 3)도 과제 글에서 계산한다
_COMPUTED_FLAGS = (FLAG_ANSWER_LEAK, FLAG_TOKEN_VISIBLE, FLAG_FRAME_LEAK)
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


def coding_template(sub: str) -> Template:
    """직군 기본 코딩 템플릿 — 없으면 이 직군은 인터뷰 과제를 만들 수 없다 (포크는 세 템플릿 중 고르게 했다)."""
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
) -> tuple[PublicIncident, list[Provenance]]:
    """확인된 사례·기준 답안 → 템플릿이 시험할 수 있는 기능형 재료. interview 는 가명화 모듈이 만든 사례 블록.
    재료가 템플릿에 없는 실패 조건·실행 조건을 쓰면 OutputRejected (다시 시도)."""
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
    seed = result.parsed
    if not set(seed.reasons) <= {r.code for r in manifest.failure_reasons}:
        raise OutputRejected("선택한 코딩 환경에서 검증할 수 없는 실패 조건입니다")
    if seed.harness is None or not set(seed.harness) <= {p.name for p in manifest.harness_params}:
        raise OutputRejected("검증에 필요한 실행 조건을 코딩 환경과 맞춰 주세요")
    seed = seed.model_copy(update={"reviewed": False, "starter": None, "task_shape": "code"})
    return incident.model_copy(update={"feature": seed, "templates": [manifest.template_id]}), [
        result.provenance
    ]


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


def with_content_flags(
    flags: Iterable[str],
    task: TaskItem,
    incident: PublicIncident,
    targeted: Iterable[TargetedQuestion] = (),
    frame: TrapFrame | None = None,
) -> list[str]:
    """글에서 다시 계산하는 표시(기준 답안 누설·토큰·함정 틀 공개 중립성)를 지금 글 기준으로 바꾼다 — 나머지 표시는 그대로.
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
    """기능형 공통 과제 한 번 만들기의 입력 — 단계마다 같은 값을 넘긴다 (#280: 단계별로 나눠 돌리고 끊긴 단계부터 잇는다).
    profile·matches 는 선택한 역량만 담는다."""

    profile: PostingProfile
    matches: list[CompetencyEvidence]
    coding_incident: PublicIncident
    template: Template
    task_id: str
    reviewer_note: str | None = None
    # 이 공고의 함정 틀 (#318) — 명세 초안이 공개 쪽을 받는다 (draft_feature_task v13). coding_incident 는 이미 틀 적용 재료다
    frame: TrapFrame | None = None


class TaskMaker(Protocol):
    """단계: feature → request → slots. 앞 단계의 MadeTask 를 받아 다음 MadeTask 를 돌려준다.
    근거가 모자라거나 역량이 빠지면 TaskGenerationFailed(422), 모델 출력을 못 쓰면 OutputRejected·LLM 오류(503)."""

    async def competencies(self, profile: PostingProfile) -> CompetencyChoice: ...

    async def feature(self, req: TaskRequest) -> MadeTask:
        """공통 과제 뼈대(build_common) → 기능형 초안(draft_feature_task)."""
        ...

    async def request(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        """요청 글 (draft_analysis_request)."""
        ...

    async def slots(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        """확장 슬롯 (build_slots)."""
        ...


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
            task_kind="feature",
        )
        task, ctx = built.task, built.ctx
        if task is None or ctx is None:
            raise TaskGenerationFailed("선택 역량의 과제 근거가 부족합니다")
        stack = req.template.manifest.stack
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
        result = await draft_feature_task(
            ctx, llm=self.llm, prompts=self.prompts, reviewer_note=req.reviewer_note
        )
        if result.output is None:
            raise OutputRejected("과제 초안을 만들지 못했습니다")
        task = apply_feature(base, result)
        if task.behavioral_anchors is None:
            raise OutputRejected("평가 행동 기준이 생성되지 않았습니다")
        if {c.competency_id for c in task.target_competencies} != {
            c.competency_id for c in req.profile.competencies
        }:
            raise TaskGenerationFailed(
                "선택한 역량 일부가 공통 과제에 포함되지 않았습니다. 역량을 다시 골라 주세요"
            )
        return MadeTask(task=task, provenance=list(result.provenance), flags=list(flags))

    async def request(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        _, ctx, _ = await self._context(req)
        analysis = await draft_analysis_request(made.task, ctx, llm=self.llm, prompts=self.prompts)
        return MadeTask(
            task=apply_analysis(made.task, analysis),
            provenance=[*made.provenance, *analysis.provenance],
            flags=list(made.flags),
        )

    async def slots(self, req: TaskRequest, made: MadeTask) -> MadeTask:
        _, ctx, _ = await self._context(req)
        slots = await build_slots(
            req.profile,
            req.matches,
            made.task,
            ctx,
            ontology=self.ontology,
            llm=self.llm,
            prompts=self.prompts,
        )
        return MadeTask(
            task=made.task.model_copy(update={"slots": slots.slots}),
            provenance=[*made.provenance, *slots.provenance],
            flags=[*made.flags, *slots.flags],
        )
