"""공통 과제 서술 (draft_task LLM) — 결정적 골격 위에 제목·상황·산출물·함정·행동 기준을 채운다.

입력은 공고·온톨로지 텍스트만 (불변식 3). 골격 값(역량·수준·기술 환경·난이도·시간)은 LLM 이 바꾸지 않는다.

검증 [확정: 2026-11-02]
- 행동 기준(1·3·5)이 인용한 수행준거 id 는 입력 목록 안에서만. 고른 역량마다 적어도 하나는 인용.
- observables 의 catches_trap 값은 traps 의 trap_id 중 하나.
- 과제가 요구하는 언어(tech_used 중 온톨로지 언어)·본문에 나오는 언어는 기술 환경(공고 언어) 안에서만.
어긋나면 사유를 붙여 한 번 다시 요청. 그래도 어긋나면 문제가 적은 쪽을 고쳐 쓰고(잘못된 id 제거)
review_flags 에 남긴다. LLM 이 실패하면 골격만 두고 review_flags 에 남긴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    DraftTaskInput,
    DraftTaskOutput,
    NcsCriterion,
    NcsKsa,
    NodeError,
    Provenance,
    TargetCompetency,
    TaskItem,
    TechEnv,
)
from task_spec.schemas.domain import Anchor, BehavioralAnchors

TASK = "draft_task"
NONE = "(없음)"
DIFFICULTY_KO = {
    "high": "high — 공고가 '수행' 수준을 요구. 실무자가 혼자 원인을 찾고 설계까지 하는 수준",
    "medium": "medium — 공고가 '활용' 수준을 요구. 주어진 구조 안에서 도구·방법을 골라 적용하는 수준",
    "low": "low — 공고가 '이해' 수준 이하. 개념을 설명하고 간단히 적용하는 수준",
}
FLAG_DRAFT_FAILED = "과제 서술 생성 실패 — 결정적 골격만 (제목·상황·행동 기준 없음)"
FLAG_DRAFT_INVALID = "과제 서술 자동 검증 실패 (재요청 후에도) — 고쳐 쓴 항목 확인 필요: "
FLAG_NOTE_DROPPED = (
    "승인자 수정 요청에 이름·학교·회사·연락처 같은 개인정보 후보가 있어 LLM 에 보내지 않음 — "
    "개인정보 없이 다시 적어 주세요"
)
FLAG_NOTE_TOO_LONG = "승인자 수정 요청이 너무 길어(1000자 초과) LLM 에 보내지 않음"
MAX_NOTE = 1000


@dataclass
class DraftContext:
    sub_label: str
    industry_label: str
    targets: list[TargetCompetency]
    criteria: list[NcsCriterion]
    ksa: list[NcsKsa]
    env: TechEnv
    difficulty: str
    time_limit_min: int
    ontology_version: str
    language_labels: list[str] = field(default_factory=list)  # 온톨로지의 언어 전체 (본문 검사용)

    @property
    def criterion_ids(self) -> set[str]:
        return {c.criterion_id for c in self.criteria}


@dataclass
class DraftResult:
    output: DraftTaskOutput | None = None
    anchors_ok: bool = True  # False = 재요청 후에도 근거 id 가 비어 행동 기준을 비운다
    provenance_id: str | None = None  # 채택한 답의 provenance
    provenance: list[Provenance] = field(default_factory=list)
    errors: list[NodeError] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def build_input(ctx: DraftContext, note: str | None = None) -> DraftTaskInput:
    comps = "\n".join(
        f"- {t.competency_id} {t.label} (공고 요구 수준: {t.level or '미상'})" for t in ctx.targets
    )
    crit = "\n".join(
        f"[{c.criterion_id}] {c.unit_name} > {c.element_name}: {c.text}" for c in ctx.criteria
    )
    ksa = "\n".join(f"- {k.kind}: {k.text}" for k in ctx.ksa) or NONE
    env = (
        ", ".join(ctx.env.languages + ctx.env.frameworks)
        or "(공고에 언어 없음 — 특정 언어에 묶이지 않게)"
    )
    return DraftTaskInput(
        sub=ctx.sub_label,
        industry=ctx.industry_label,
        difficulty=DIFFICULTY_KO.get(ctx.difficulty, ctx.difficulty),
        time_limit_min=ctx.time_limit_min,
        tech_env=env,
        competencies=comps,
        ncs_criteria=crit,
        ksa=ksa,
        revision_note=note or NONE,
    )


def _lang_patterns(labels: list[str]) -> dict[str, re.Pattern[str]]:
    """언어 이름 → 본문 검색 정규식. 한 글자(C·R)는 로그 식별자와 겹쳐 '… 언어' 일 때만 잡는다."""
    out: dict[str, re.Pattern[str]] = {}
    for label in labels:
        for part in re.sub(r"\(.*?\)", "", label).split("/"):
            part = part.strip()
            if not part:
                continue
            if len(part) == 1:
                out[part] = re.compile(rf"(?<![A-Za-z0-9+#&_-]){re.escape(part)}\s?언어")
            else:
                out[part] = re.compile(
                    rf"(?<![A-Za-z0-9+#&]){re.escape(part)}(?![A-Za-z0-9+#&])",
                    0 if len(part) <= 2 else re.IGNORECASE,
                )
    return out


def _names(labels: list[str]) -> set[str]:
    """'Shell/Bash' → {'shell/bash', 'shell', 'bash'} (소문자)."""
    out: set[str] = set()
    for x in labels:
        out.add(x.strip().lower())
        out |= {p.strip().lower() for p in re.sub(r"\(.*?\)", "", x).split("/") if p.strip()}
    return out


def _allowed_tech(env: TechEnv) -> set[str]:
    return _names(env.languages + env.frameworks)


def check(out: DraftTaskOutput, ctx: DraftContext) -> list[str]:
    """어긋난 점 목록 (비면 통과). 문장은 그대로 재요청 사유가 된다 — 온톨로지·공고 값만 담는다."""
    problems: list[str] = []
    allowed = ctx.criterion_ids
    anchors = out.behavioral_anchors
    cited: set[str] = set()
    for name in ("level_1", "level_3", "level_5"):
        a: Anchor = getattr(anchors, name)
        bad = [i for i in a.criterion_ids if i not in allowed]
        if bad:
            problems.append(f"{name} 가 목록에 없는 수행준거 id 를 인용: {', '.join(bad)}")
        cited |= set(a.criterion_ids) & allowed
    for t in ctx.targets:
        if t.ncs_criterion_ids and not cited & set(t.ncs_criterion_ids):
            problems.append(
                f"행동 기준이 역량 {t.competency_id} 의 수행준거를 하나도 인용하지 않음"
            )
    trap_ids = [t.trap_id for t in out.traps]
    if len(set(trap_ids)) != len(trap_ids):
        problems.append("trap_id 가 겹침")
    for o in out.observables:
        if o.kind == "catches_trap" and o.value not in trap_ids:
            problems.append(f"catches_trap 값 {o.value} 가 traps 의 trap_id 가 아님")
    tech_ok = _allowed_tech(ctx.env)
    langs = _names(ctx.language_labels)
    # 언어만 검사한다 (도구·개념 이름은 온톨로지 언어 목록에 없으므로 통과)
    extra = [x for x in out.tech_used if x.strip().lower() in langs - tech_ok]
    if extra:
        problems.append(f"기술 환경 밖 언어 요구: {', '.join(extra)}")
    text = "\n".join([out.title, out.scenario, *out.deliverables])
    env_langs = {p.lower() for p in _lang_patterns(ctx.env.languages)}
    mentioned = [
        name
        for name, rx in _lang_patterns(ctx.language_labels).items()
        if name.lower() not in env_langs and name.lower() not in tech_ok and rx.search(text)
    ]
    if mentioned:
        problems.append(f"본문에 기술 환경 밖 언어가 나옴: {', '.join(sorted(mentioned))}")
    return problems


def repair(out: DraftTaskOutput, ctx: DraftContext) -> tuple[DraftTaskOutput, bool]:
    """재요청 후에도 어긋나면 잘못된 id·값만 걷어낸다. 행동 기준 근거가 비면 (출력, False)."""
    allowed = ctx.criterion_ids
    trap_ids = {t.trap_id for t in out.traps}
    tech_ok = _allowed_tech(ctx.env)
    upd: dict[str, object] = {
        "observables": [
            o for o in out.observables if o.kind != "catches_trap" or o.value in trap_ids
        ],
        "tech_used": [
            x
            for x in out.tech_used
            if x.strip().lower() not in _names(ctx.language_labels) - tech_ok
        ],
    }
    levels: dict[str, Anchor] = {}
    for name in ("level_1", "level_3", "level_5"):
        a: Anchor = getattr(out.behavioral_anchors, name)
        ids = [i for i in a.criterion_ids if i in allowed]
        if ids:
            levels[name] = Anchor(behavior=a.behavior, criterion_ids=ids)
    anchors_ok = len(levels) == 3
    if anchors_ok:
        upd["behavioral_anchors"] = BehavioralAnchors(**levels)
    return out.model_copy(update=upd), anchors_ok


def apply(task: TaskItem, res: DraftResult) -> TaskItem:
    """골격 TaskItem 에 서술을 붙인다. 서술이 없으면 flags 만 붙인다."""
    out = res.output
    if out is None:
        return task.model_copy(update={"review_flags": [*task.review_flags, *res.flags]})
    return task.model_copy(
        update={
            "title": out.title,
            "scenario": out.scenario,
            "deliverables": list(out.deliverables),
            "invariant": out.invariant,
            "variable": list(out.variable),
            "traps": list(out.traps),
            "observables": list(out.observables),
            "behavioral_anchors": out.behavioral_anchors if res.anchors_ok else None,
            "provenance_id": res.provenance_id,
            "review_flags": [*task.review_flags, *res.flags],
        }
    )


@lru_cache(maxsize=1)
def _detectors() -> Detectors:
    return Detectors(load_dictionaries(), use_kiwi=True)


def safe_reviewer_note(comment: str | None) -> tuple[str | None, list[str]]:
    """승인자 수정 요청은 지원자 문서가 아니지만 지원자를 보고 쓰므로, 가명화와 같은 로컬 탐지기
    (이름·학교·회사·연락처·주소 등)로 검사해 하나라도 걸리면 보내지 않는다 (privacy-auditor W1)."""
    if not comment or not comment.strip():
        return None, []
    text = comment.strip()
    if len(text) > MAX_NOTE:
        return None, [FLAG_NOTE_TOO_LONG]
    if _detectors().detect(text):
        return None, [FLAG_NOTE_DROPPED]
    return text, []


async def draft_task(
    ctx: DraftContext,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    reviewer_note: str | None = None,
) -> DraftResult:
    template = prompts.get(TASK)
    res = DraftResult()
    note = f"승인자 수정 요청: {reviewer_note}" if reviewer_note else None
    best: tuple[DraftTaskOutput, list[str], str] | None = None
    for attempt in range(2):
        prompt = assemble(template, build_input(ctx, note), ontology_version=ctx.ontology_version)
        try:
            r = await llm.call(prompt, DraftTaskOutput)
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(node=TASK, code=type(exc).__name__, message="과제 서술 생성 실패")
            )
            continue
        res.provenance.append(r.provenance)
        problems = check(r.parsed, ctx)
        if best is None or len(problems) < len(best[1]):
            best = (r.parsed, problems, r.provenance.provenance_id)
        if not problems:
            break
        if attempt == 0:
            res.errors.append(
                NodeError(
                    node=TASK,
                    code="draft_invalid",
                    message=f"자동 검증 실패 {len(problems)}건 — 재요청",
                )
            )
            lines = "\n".join(f"- {p}" for p in problems)
            note = "\n".join(
                x for x in (note, f"이전 답의 문제 (고쳐서 다시 쓰세요):\n{lines}") if x
            )
    if best is None:
        res.flags.append(FLAG_DRAFT_FAILED)
        return res
    out, problems, res.provenance_id = best
    if problems:
        out, res.anchors_ok = repair(out, ctx)
        res.flags.append(FLAG_DRAFT_INVALID + "; ".join(problems))
    res.output = out
    return res
