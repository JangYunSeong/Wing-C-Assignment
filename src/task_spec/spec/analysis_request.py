"""문제 분석 판 — 요청 글·요청자 지식 생성 (prompts/draft_analysis_request, docs/design/analysis-stage.md §4·§5·§5.1).

승인 전 기능형 명세의 공개 요구·공개 수용 기준·추가 요구를 **배경 글 + 요청 글 + 사실(물으면 주는 답) + 공개 수용 기준 → 요구 연결**로 바꾼다.
배경 글은 README 상황 절 자리 — 지금 상황 글은 요구를 나열하므로 요구 없는 글을 새로 쓴다 (§4.1, 2026-11-16 사용자 (나)).
생성 → 검사(§5.1) → 문제를 알려 1회 재요청 → 코드로 고칠 수 있는 것 고치기 → 다시 검사 — 기능형 초안(spec/draft.draft_task)과 같은 루프.
- 입력에 숨은 요구·허용 설계를 넣지 않는다 (§5). 지원자 문서는 없다 — 공고·재료로 만든 명세만.
- 사실과 추가 요구가 겹치거나 자료가 요구를 그대로 말하면 고치지 않고 승인 화면에 표시만 한다 (2026-11-16 사용자).
- CURRENT 가 없으면(평가 전) 명세 그래프가 부르지 않는다 — 평가·스모크는 판을 정해 부른다 (불변식 7).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    DraftAnalysisInput,
    DraftAnalysisOutput,
    Fact,
    NodeError,
    Provenance,
    TaskItem,
)
from task_spec.schemas.incident import FeatureSeed
from task_spec.spec.analysis import check_analysis
from task_spec.spec.draft import DraftContext
from task_spec.spec.feature import find_terms
from task_spec.starter.answer_rule import strip_particle

logger = logging.getLogger(__name__)

TASK = "draft_analysis_request"
NONE = "(없음)"
REQUEST_MAX = 450  # 요청 글 300자 안팎 — 넘으면 재요청 (스키마 상한 600 은 여유)
BACKGROUND_MAX = 600  # 배경 글 — 지금 상황 글(요구 목록 포함 ~550자)보다 짧게
COPY_RATIO = 0.7  # 공개 수용 기준 문장의 낱말이 이만큼 요청 글에 있으면 베낀 것
STATES_RATIO = 0.8  # 공식 요구 문장의 낱말이 이만큼 자료 본문에 있으면 자료가 요구를 그대로 말한 것

FLAG_FAILED = "요청 글·사실 생성 실패 — 문제 분석 칸 없이 예전 판 그대로"
FLAG_INVALID = "요청 글·사실 자동 검사 실패 (재요청·고쳐 쓰기 후에도) — 남은 문제 확인 필요: "
FLAG_REPAIRED = "요청 글·사실을 코드가 고침: "
FLAG_OVERLAP = "요청 글·사실: 사실과 추가 요구가 겹침 — 한 질문이 두 항목에 걸리거나 추가 요구가 사실의 답을 흘림 (확인 필요): "
FLAG_ARTIFACT_STATES = (
    "요청 글·사실: 자료가 공식 요구를 거의 그대로 말함 — 묻지 않고 알 수 있음 (확인 필요): "
)
FLAG_NO_MENTION = (
    "요청 글·사실: 요청 글이 공식 요구를 하나도 직접 말하지 않음 (흐림 정도 기준값은 [미정])"
)
FLAG_ALL_MENTIONED = "요청 글·사실: 요청 글이 공식 요구를 모두 직접 말함 — 물을 것이 없음 (흐림 정도 기준값은 [미정])"

_WORD = re.compile(r"[0-9A-Za-z가-힣]+")


@dataclass
class AnalysisResult:
    output: DraftAnalysisOutput | None = None
    provenance_id: str | None = None  # 채택한 답의 provenance
    first_problems: list[str] = field(default_factory=list)  # 첫 답의 검사 실패 사유 (평가용)
    provenance: list[Provenance] = field(default_factory=list)
    errors: list[NodeError] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _seed(ctx: DraftContext) -> FeatureSeed | None:
    return ctx.incident.feature if ctx.incident is not None else None


def build_analysis_input(
    task: TaskItem, seed: FeatureSeed | None, note: str | None
) -> DraftAnalysisInput:
    """공개 칸·추가 요구·재료 업무 필요만 — 숨은 요구(traps·acceptance_hidden)·허용 설계는 넣지 않는다 (§5)."""
    clars = "\n".join(f"- {c.clarification_id} {c.topic} — {c.answer}" for c in task.clarifications)
    return DraftAnalysisInput(
        title=task.title or NONE,
        scenario=task.scenario or NONE,
        requirements="\n".join(f"- {r.req_id} {r.text}" for r in task.requirements) or NONE,
        acceptance_public="\n".join(f"- {a.ac_id} {a.text}" for a in task.acceptance_public)
        or NONE,
        clarifications=clars or NONE,
        business_need=seed.business_need if seed is not None else NONE,
        revision_note=note or NONE,
    )


def _words(text: str) -> list[str]:
    """두 글자 이상 낱말 — 끝 조사를 뗀다 (진행자 낱말 규칙과 같은 정규화)."""
    return list(
        dict.fromkeys(w for w in (strip_particle(x) for x in _WORD.findall(text)) if len(w) >= 2)
    )


def _hidden_terms(task: TaskItem, ctx: DraftContext) -> list[str]:
    """요청 글·사실에 나오면 숨은 요구가 새는 말 — 재료 금지어·원인 개념·운영 조건(업무 필요에 나온 말 제외)·먼저 말할 개념·원 사례 원인 용어."""
    seed, inc = _seed(ctx), ctx.incident
    obs = [*task.observables, *(task.debug_stage.observables if task.debug_stage else [])]
    terms = [o.value for o in obs if o.kind == "first_mention_of"]
    if seed is not None:
        public = seed.business_need
        terms += [*seed.leak_terms, *seed.stage2_cause_terms]
        terms += [t for t in seed.trigger_terms if not find_terms([t], public)]
    if inc is not None:
        terms += inc.hidden_terms
    return list(dict.fromkeys(terms))


def _answer_only(answer: str, context: str) -> list[str]:
    """구체 답에만 있는 낱말 — 주제·조건 문장·요청 글·제목에 없는 말 (값·순서·조건이 여기 든다)."""
    return [w for w in _words(answer) if not find_terms([w], context)]


def _hint_leak(answer: str, hint: str, context: str) -> list[str]:
    leaked = [w for w in _answer_only(answer, context) if find_terms([w], hint)]
    if re.search(r"\d", hint) and re.search(r"\d", answer):
        leaked.append("수치")
    return leaked


def preview(task: TaskItem, out: DraftAnalysisOutput) -> TaskItem:
    """초안을 명세에 붙인 모습 (표시 없이) — 참조 검사·반영이 같은 변환을 쓴다."""
    links = {a.ac_id: list(a.req_ids) for a in out.acceptance_links}
    return task.model_copy(
        update={
            "background": out.background,
            "request": out.request,
            "request_mentions": list(out.request_mentions),
            "facts": [Fact(**f.model_dump()) for f in out.facts],
            "acceptance_public": [
                a.model_copy(update={"req_ids": links.get(a.ac_id, [])})
                for a in task.acceptance_public
            ],
        }
    )


def _visible_problems(
    label: str, text: str, task: TaskItem, hidden: list[str], limit: int
) -> list[str]:
    """지원자가 처음부터 보는 글(배경 글·요청 글) 공통 검사 — 길이·수치·숨은 말·공개 수용 기준 베끼기."""
    problems = []
    if len(text) > limit:
        problems.append(f"{label}이 김 ({len(text)}자) — {limit}자 안으로 줄인다")
    if re.search(r"\d", text):
        problems.append(f"{label}에 수치가 있음 — 수치는 사실의 구체 답에만")
    if bad := find_terms(hidden, text):
        problems.append(f"{label}에 숨은 요구·운영 조건을 드러내는 말: {', '.join(bad)}")
    for a in task.acceptance_public:
        ws = _words(a.text)
        if ws and sum(bool(find_terms([w], text)) for w in ws) / len(ws) >= COPY_RATIO:
            problems.append(f"{label}이 공개 수용 기준 {a.ac_id} 문장을 거의 그대로 씀 — 흐리게")
    return problems


def check_analysis_draft(out: DraftAnalysisOutput, task: TaskItem, ctx: DraftContext) -> list[str]:
    """§5.1 명세 쪽 검사 — 재요청 사유. 참조(spec/analysis.check_analysis) + 배경 글 + 요청 글 + 사실 답."""
    problems = check_analysis(preview(task, out))
    hidden = _hidden_terms(task, ctx)
    bg, req = out.background, out.request
    problems += _visible_problems("배경 글", bg, task, hidden, BACKGROUND_MAX)
    problems += _visible_problems("요청 글", req, task, hidden, REQUEST_MAX)
    # 배경 글에는 공식 요구가 없어야 한다 — 요구 문장을 거의 그대로 쓰거나 사실의 답에만 있는 말(값·순서·조건)을 쓰면 묻지 않고 안다
    for r in task.requirements:
        ws = [w for w in _words(r.text) if not find_terms([w], task.title or "")]
        if ws and sum(bool(find_terms([w], bg)) for w in ws) / len(ws) >= COPY_RATIO:
            problems.append(f"배경 글이 공식 요구 {r.req_id} 를 거의 그대로 말함 — 요구는 사실로만")
    title = task.title or ""
    for f in out.facts:
        own = " ".join([title, req, f.topic, f.ask_when])
        leaked = [w for w in _answer_only(f.answer, own) if find_terms([w], bg)]
        # 두 글자 낱말 하나("요청")는 소재 말일 때가 많다 — 두 낱말 이상이거나 세 글자 이상 낱말일 때만 (2026-11-16 스모크)
        if len(leaked) >= 2 or any(len(w) >= 3 for w in leaked):
            problems.append(
                f"배경 글이 {f.fact_id} 의 답을 흘림 ({', '.join(leaked[:3])}) — 배경(누가 무엇 때문에 곤란한가)만"
            )
    # 지원자가 처음부터 보는 글 = 제목·배경 글·요청 글. 예전 상황 글은 요구를 나열하는 일이 많아(제품팀 요청 첫째·둘째…) 넣지 않는다 —
    # 넣으면 상황 글에 있는 말은 다 "보인다"로 쳐 모호한 답의 누출을 놓친다 (2026-11-16 스모크)
    context_base = " ".join([title, bg, req])
    for f in out.facts:
        if bad := find_terms(hidden, " ".join([f.topic, f.ask_when, f.answer, f.answer_hint])):
            problems.append(f"{f.fact_id} 에 숨은 요구·운영 조건을 드러내는 말: {', '.join(bad)}")
        if f.answer_hint.strip() == f.answer.strip():
            problems.append(f"{f.fact_id} 의 모호한 답이 구체 답과 같음")
        elif leaked := _hint_leak(
            f.answer, f.answer_hint, " ".join([context_base, f.topic, f.ask_when])
        ):
            problems.append(
                f"{f.fact_id} 의 모호한 답이 구체 답의 내용을 흘림 ({', '.join(leaked[:3])}) — 그런 규칙이 있다는 것만"
            )
    return problems


def repair_analysis(out: DraftAnalysisOutput, task: TaskItem) -> DraftAnalysisOutput:
    """코드로 고칠 수 있는 것 — 없는 id 를 가리키는 연결·covers 를 지우고, 사실 id 를 f1 부터 다시 매긴다. 문장은 고치지 않는다."""
    reqs = {r.req_id for r in task.requirements}
    known = reqs | {a.ac_id for a in task.acceptance_public}
    facts = []
    for f in out.facts:
        covers = [c for c in dict.fromkeys(f.covers) if c in known]
        if covers:
            facts.append(f.model_copy(update={"fact_id": f"f{len(facts) + 1}", "covers": covers}))
    acs = {a.ac_id for a in task.acceptance_public}
    links = []
    for a in out.acceptance_links:
        rs = [r for r in dict.fromkeys(a.req_ids) if r in reqs]
        if a.ac_id in acs and rs and a.ac_id not in {x.ac_id for x in links}:
            links.append(a.model_copy(update={"req_ids": rs}))
    return out.model_copy(
        update={
            "request_mentions": [r for r in dict.fromkeys(out.request_mentions) if r in reqs],
            "facts": facts or out.facts,
            "acceptance_links": links or out.acceptance_links,
        }
    )


def overlap_flags(out: DraftAnalysisOutput, task: TaskItem) -> list[str]:
    """사실 ↔ 추가 요구 겹침 (표시만, 2026-11-16 사용자). 손 시범: c3 "조회 범위와 정렬 기준" ↔ f4 "조회 순서"가 한 질문에 함께 걸리고,
    c3 의 모호한 답이 f4 의 답("최신순")을 흘렸다.
    - 주제 겹침: 추가 요구의 주제·매칭 낱말 중 두 낱말 이상이 사실의 주제·조건 문장에 있을 때. 과제 말(제목·요청 글, 사실 둘 이상에 나오는 말
      — "요청"·"기록" 같은 소재)은 세지 않는다 — 스모크에서 소재 낱말 하나로 모든 쌍이 걸렸다.
    - 답 흘림: 추가 요구의 모호한 답에 그 사실의 구체 답에만 있는 낱말(다른 사실의 답·그 추가 요구 자신의 주제·답에 없는 말)이 있을 때."""
    fact_words = [set(_words(" ".join([f.topic, f.ask_when, f.answer]))) for f in out.facts]
    shared = {w for w in set().union(*fact_words) if sum(w in ws for ws in fact_words) >= 2}
    domain = " ".join([task.title or "", out.background, out.request, *shared])
    context_base = " ".join(
        [out.request, task.title or ""]
    )  # 상황 글은 넣지 않는다 (check_analysis_draft)
    pairs = []
    for c in task.clarifications:
        cwords = [w for w in [*_words(c.topic), *c.match_terms] if not find_terms([w], domain)]
        own = " ".join([c.topic, *c.match_terms, c.answer])
        for f in out.facts:
            hit = find_terms(cwords, " ".join([f.topic, f.ask_when]))
            hit = hit if len(hit) >= 2 else []
            leak: list[str] = []
            if c.answer_hint:
                others = " ".join(g.answer for g in out.facts if g.fact_id != f.fact_id)
                only = [
                    w
                    for w in _answer_only(f.answer, " ".join([context_base, f.topic, f.ask_when]))
                    if not find_terms([w], " ".join([others, own]))
                ]
                leak = find_terms(only, c.answer_hint)
            if hit or leak:
                why = [*hit, *(f"{c.clarification_id} 모호한 답에 '{w}'" for w in leak)]
                pairs.append(f"{c.clarification_id}↔{f.fact_id} ({', '.join(why[:3])})")
    return [FLAG_OVERLAP + "; ".join(pairs)] if pairs else []


def artifact_flags(task: TaskItem) -> list[str]:
    """자료가 공식 요구를 거의 그대로 말하는지 (표시만) — 자료는 한도 없이 열리므로 묻는 일이 사라진다 (§5.1)."""
    hits = []
    for r in task.requirements:
        ws = [w for w in _words(r.text) if not find_terms([w], task.title or "")]
        for a in task.artifacts:
            if ws and sum(bool(find_terms([w], a.content)) for w in ws) / len(ws) >= STATES_RATIO:
                hits.append(f"{r.req_id}→{a.artifact_id}")
    return [FLAG_ARTIFACT_STATES + ", ".join(hits)] if hits else []


def blur_flags(out: DraftAnalysisOutput, task: TaskItem) -> list[str]:
    """흐림 정도 — 기준값이 [미정]이라 끝값(하나도 / 모두)만 표시 (§5.1)."""
    n, total = len(set(out.request_mentions)), len(task.requirements)
    if total and n == 0:
        return [FLAG_NO_MENTION]
    if total and n >= total:
        return [FLAG_ALL_MENTIONED]
    return []


def apply_analysis(task: TaskItem, res: AnalysisResult) -> TaskItem:
    """기능형 명세에 요청 글·요청 글이 말한 요구·사실·공개 수용 기준 → 요구를 붙이고 표시를 남긴다. 실패면 표시만 (예전 판 그대로)."""
    out = res.output
    if out is None:
        return task.model_copy(update={"review_flags": [*task.review_flags, *res.flags]})
    shown = [*res.flags, *overlap_flags(out, task), *artifact_flags(task), *blur_flags(out, task)]
    done = preview(task, out)
    return done.model_copy(update={"review_flags": [*task.review_flags, *shown]})


async def draft_analysis_request(
    task: TaskItem,
    ctx: DraftContext,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    reviewer_note: str | None = None,
    version: int | None = None,
) -> AnalysisResult:
    """생성 → 검사 → 1회 재요청 → 고쳐 쓰기 → 다시 검사. version 을 주면 그 판(평가·스모크), 안 주면 CURRENT."""
    template = prompts.get(TASK, version)
    seed = _seed(ctx)
    res = AnalysisResult()
    note = f"승인자 수정 요청: {reviewer_note}" if reviewer_note else None
    best: tuple[DraftAnalysisOutput, list[str], str] | None = None
    for attempt in range(2):
        prompt = assemble(
            template, build_analysis_input(task, seed, note), ontology_version=ctx.ontology_version
        )
        try:
            r = await llm.call(prompt, DraftAnalysisOutput)
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(node=TASK, code=type(exc).__name__, message="요청 글·사실 생성 실패")
            )
            continue
        res.provenance.append(r.provenance)
        out = r.parsed
        problems = check_analysis_draft(out, task, ctx)
        logger.info("%s attempt=%d problems=%d", TASK, attempt + 1, len(problems))
        if best is None or len(problems) < len(best[1]):
            best = (out, problems, r.provenance.provenance_id)
        if not problems:
            break
        if attempt == 0:
            res.first_problems = list(problems)
            lines = "\n".join(f"- {p}" for p in problems)
            note = "\n".join(
                x for x in (note, f"이전 답의 문제 (고쳐서 다시 쓰세요):\n{lines}") if x
            )
    if best is None:
        res.flags.append(FLAG_FAILED)
        return res
    out, problems, res.provenance_id = best
    if problems:
        out = repair_analysis(out, task)
        left = check_analysis_draft(out, task, ctx)
        if fixed := [p for p in problems if p not in left]:
            res.flags.append(FLAG_REPAIRED + "; ".join(fixed))
        if left:
            res.flags.append(FLAG_INVALID + "; ".join(left))
    res.output = out
    return res
