"""문제 분석 판 — 요청 글·요청자 지식 생성 (prompts/draft_analysis_request, docs/design/analysis-stage.md §4·§5·§5.1).

승인 전 기능형 명세의 공개 요구·공개 수용 기준·추가 요구를 **배경 글 + 요청 글 + 사실(물으면 주는 답) + 공개 수용 기준 → 요구 연결**로 바꾼다.
배경 글은 README 상황 절 자리 — 지금 상황 글은 요구를 나열하므로 요구 없는 글을 새로 쓴다 (§4.1, 2026-11-16 사용자 (나)).
생성 → 검사(§5.1) → 문제를 알려 1회 재요청 → 코드로 고칠 수 있는 것 고치기 → 다시 검사 — 기능형 초안(spec/draft.draft_task)과 같은 루프.
- 입력에 숨은 요구·허용 설계를 넣지 않는다 (§5). 지원자 문서는 없다 — 공고·재료로 만든 명세만.
- 재요청 사유는 틀림이 확실한 것만 (참조·길이·숨은 요구 금지어). 낱말 겹침으로 본 흘림·사실과 추가 요구 겹침·자료가 요구를 그대로
  말함은 고치지 않고 승인 화면에 표시만 한다 — 흘림은 뜻의 문제라 낱말로 재면 오탐과 놓침이 번갈아 나온다 (v1·v2 평가, 2026-11-16 사용자).
  흘림의 측정은 평가의 열림 측정과 흘림 판정(judge_fact_leak)이 맡는다.
- CURRENT 가 없으면(평가 전) 명세 그래프가 부르지 않는다 — 평가·스모크는 판을 정해 부른다 (불변식 7).
- v3 출력(DraftAnalysisOutputV2): 추가 요구 c# 마다 질문 문장(ask_when)도 쓴다 — 사실과 같은 단계가 써야 두 항목의 조건을 나눈다.
  공개 수용 기준 연결은 생성기(draft_feature_task v11)가 쓴 것이 있으면 그것을 쓴다 (2026-11-16 사용자).
- v4 출력(DraftAnalysisOutputV3): 요청 글은 공식 요구(무엇)를 모두 말하고 사실은 규칙(어떻게)만 — request_mentions 는 코드가
  모든 r# 로 채운다 (자기 보고 없음). 흐림 정도 표시(하나도·모두)는 끈다 — 모두 말하는 것이 설계 (2026-11-16 사용자).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import cast

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    AcceptanceLink,
    ClarificationAsk,
    DraftAnalysisInput,
    DraftAnalysisOutput,
    DraftAnalysisOutputV2,
    DraftAnalysisOutputV3,
    Fact,
    NodeError,
    Provenance,
    TaskItem,
)
from task_spec.schemas.draft import FactDraft as FactDraftT
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
FLAG_LEAK = (
    "요청 글·사실: 묻지 않고 알 수 있을지 모름 — 낱말 겹침으로 본 후보라 오탐이 있음 (확인 필요): "
)
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
        # 생성기가 연결을 쓴 명세(v11)는 [r#] 를 보여 준다 — v3 는 그때 acceptance_links 를 비운다
        acceptance_public="\n".join(
            f"- {a.ac_id} {'[' + ', '.join(a.req_ids) + '] ' if a.req_ids else ''}{a.text}"
            for a in task.acceptance_public
        )
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


def _mentions(out: DraftAnalysisOutput, task: TaskItem) -> list[str]:
    """v4 는 요청 글이 공식 요구를 모두 말하는 설계라 모든 r# — 예전 판은 모델이 적은 것."""
    if isinstance(out, DraftAnalysisOutputV3):
        return [r.req_id for r in task.requirements]
    return list(out.request_mentions)


def _asks(out: DraftAnalysisOutput) -> dict[str, str]:
    if not isinstance(out, DraftAnalysisOutputV2):
        return {}
    return {a.clarification_id: a.ask_when for a in out.clarification_asks}


def preview(task: TaskItem, out: DraftAnalysisOutput) -> TaskItem:
    """초안을 명세에 붙인 모습 (표시 없이) — 참조 검사·반영이 같은 변환을 쓴다.
    공개 수용 기준 연결은 명세에 이미 있으면(생성기 v11) 그대로, 없으면 이 단계가 쓴 것. 추가 요구 질문 문장은 v3 출력만."""
    links = {a.ac_id: list(a.req_ids) for a in out.acceptance_links}
    asks = _asks(out)
    return task.model_copy(
        update={
            "background": out.background,
            "request": out.request,
            "request_mentions": _mentions(out, task),
            "facts": [Fact(**f.model_dump()) for f in out.facts],
            "acceptance_public": [
                a if a.req_ids else a.model_copy(update={"req_ids": links.get(a.ac_id, [])})
                for a in task.acceptance_public
            ],
            "clarifications": [
                c.model_copy(update={"ask_when": asks[c.clarification_id]})
                if c.clarification_id in asks
                else c
                for c in task.clarifications
            ],
        }
    )


def check_analysis_draft(out: DraftAnalysisOutput, task: TaskItem, ctx: DraftContext) -> list[str]:
    """§5.1 재요청 사유 — 틀림이 확실한 것만: 참조(spec/analysis.check_analysis)·길이·숨은 요구 금지어·모호한 답이 구체 답과 같음.
    낱말 겹침으로 재는 흘림(수치·베끼기·모호한 답 흘림·배경 글 누출)은 leak_flags 로 표시만 — v1 평가에서 6건 중 4건이 일반 낱말 오탐,
    "API 호출 시점에 남아요"처럼 낱말을 바꾼 흘림은 못 잡았다 (2026-11-16 사용자)."""
    problems = check_analysis(preview(task, out))
    hidden = _hidden_terms(task, ctx)
    for label, text, limit in (
        ("배경 글", out.background, BACKGROUND_MAX),
        ("요청 글", out.request, REQUEST_MAX),
    ):
        if len(text) > limit:
            problems.append(f"{label}이 김 ({len(text)}자) — {limit}자 안으로 줄인다")
        if bad := find_terms(hidden, text):
            problems.append(f"{label}에 숨은 요구·운영 조건을 드러내는 말: {', '.join(bad)}")
    for f in out.facts:
        if bad := find_terms(hidden, " ".join([f.topic, f.ask_when, f.answer, f.answer_hint])):
            problems.append(f"{f.fact_id} 에 숨은 요구·운영 조건을 드러내는 말: {', '.join(bad)}")
        if f.answer_hint.strip() == f.answer.strip():
            problems.append(f"{f.fact_id} 의 모호한 답이 구체 답과 같음")
    if isinstance(out, DraftAnalysisOutputV2):
        problems += _ask_problems(out, task)
    return problems


def _ask_problems(out: DraftAnalysisOutputV2, task: TaskItem) -> list[str]:
    """추가 요구 질문 문장 — c# 마다 하나, 있는 c# 만. 문장 안의 숨은 요구 말은 보지 않는다 (진행자만 보는 칸 — 사실 ask_when 과 같음)."""
    have = [c.clarification_id for c in task.clarifications]
    asked = [a.clarification_id for a in out.clarification_asks]
    problems = []
    if unknown := [c for c in asked if c not in have]:
        problems.append(f"clarification_asks 에 없는 추가 요구: {', '.join(unknown)}")
    if missing := [c for c in have if c not in asked]:
        problems.append(
            f"추가 요구 {', '.join(missing)} 의 ask_when 이 없음 — c# 마다 구체 답을 줄 질문 문장을 쓴다"
        )
    return problems


def leak_flags(out: DraftAnalysisOutput, task: TaskItem) -> list[str]:
    """낱말 겹침으로 본 흘림 후보 (표시만). 보이는 글 = 제목·배경 글·요청 글 — 예전 상황 글은 요구를 나열하는 일이 많아 넣지 않는다.
    - 보이는 글의 수치 (숫자가 든 낱말을 그대로 보여 줘 "V2X" 같은 이름은 승인자가 바로 가린다)
    - 보이는 글이 공개 수용 기준 문장 / 배경 글이 공식 요구 문장과 낱말이 거의 같음
    - 모호한 답에 구체 답에만 있는 낱말 (값·순서·조건이 여기 든다)
    - 배경 글에 구체 답에만 있는 낱말 — 두 낱말 이상이거나 세 글자 이상일 때만 ("요청한"·"현재" 같은 일반 낱말 오탐, v1 평가)."""
    title, bg, req = task.title or "", out.background, out.request
    hits: list[str] = []
    for label, text in (("배경 글", bg), ("요청 글", req)):
        if nums := [w for w in _WORD.findall(text) if re.search(r"\d", w)]:
            hits.append(f"{label}에 수치 ({', '.join(nums[:3])})")
        for a in task.acceptance_public:
            ws = _words(a.text)
            if ws and sum(bool(find_terms([w], text)) for w in ws) / len(ws) >= COPY_RATIO:
                hits.append(f"{label}이 공개 수용 기준 {a.ac_id} 문장과 거의 같음")
    for r in task.requirements:
        ws = [w for w in _words(r.text) if not find_terms([w], title)]
        if ws and sum(bool(find_terms([w], bg)) for w in ws) / len(ws) >= COPY_RATIO:
            hits.append(f"배경 글이 공식 요구 {r.req_id} 와 거의 같음")
    for f in out.facts:
        own = " ".join([title, req, f.topic, f.ask_when])
        if f.answer_hint.strip() != f.answer.strip() and (
            leaked := _hint_leak(f.answer, f.answer_hint, " ".join([own, bg]))
        ):
            hits.append(f"{f.fact_id} 모호한 답에 구체 답의 말 ({', '.join(leaked[:3])})")
        in_bg = [w for w in _answer_only(f.answer, own) if find_terms([w], bg)]
        if len(in_bg) >= 2 or any(len(w) >= 3 for w in in_bg):
            hits.append(f"배경 글에 {f.fact_id} 답의 말 ({', '.join(in_bg[:3])})")
    return [FLAG_LEAK + "; ".join(hits)] if hits else []


def repair_analysis(out: DraftAnalysisOutput, task: TaskItem) -> DraftAnalysisOutput:
    """코드로 고칠 수 있는 것 — 없는 id 를 가리키는 연결·covers 를 지우고, 사실 id 를 f1 부터 다시 매긴다. 문장은 고치지 않는다."""
    reqs = {r.req_id for r in task.requirements}
    known = reqs | {a.ac_id for a in task.acceptance_public}
    facts: list[FactDraftT] = []
    for f in out.facts:
        covers = [c for c in dict.fromkeys(f.covers) if c in known]
        if covers:
            facts.append(f.model_copy(update={"fact_id": f"f{len(facts) + 1}", "covers": covers}))
    acs = {a.ac_id for a in task.acceptance_public}
    links: list[AcceptanceLink] = []
    for a in out.acceptance_links:
        rs = [r for r in dict.fromkeys(a.req_ids) if r in reqs]
        if a.ac_id in acs and rs and a.ac_id not in {x.ac_id for x in links}:
            links.append(a.model_copy(update={"req_ids": rs}))
    upd = {
        "request_mentions": [r for r in dict.fromkeys(out.request_mentions) if r in reqs],
        "facts": facts or out.facts,
        "acceptance_links": links or out.acceptance_links,
    }
    if isinstance(
        out, DraftAnalysisOutputV2
    ):  # 없는 추가 요구를 가리키는 문장·같은 c# 두 번째는 지운다 (빠진 문장은 못 고침)
        have = {c.clarification_id for c in task.clarifications}
        asks: list[ClarificationAsk] = []
        for ca in out.clarification_asks:
            if ca.clarification_id in have and ca.clarification_id not in {
                x.clarification_id for x in asks
            }:
                asks.append(ca)
        upd["clarification_asks"] = asks
    return out.model_copy(update=upd)


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
    """흐림 정도 — 기준값이 [미정]이라 끝값(하나도 / 모두)만 표시 (§5.1). v4 는 모두 말하는 것이 설계라 표시하지 않는다."""
    if isinstance(out, DraftAnalysisOutputV3):
        return []
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
    shown = [
        *res.flags,
        *leak_flags(out, task),
        *overlap_flags(out, task),
        *artifact_flags(task),
        *blur_flags(out, task),
    ]
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
        full = build_analysis_input(task, seed, note)
        # 판마다 입력 모델이 다르다 (v4 는 업무 필요를 받지 않는다) — 그 판의 칸만 넘긴다
        inp = template.input_model.model_validate(
            {k: getattr(full, k) for k in template.input_model.model_fields}
        )
        prompt = assemble(template, inp, ontology_version=ctx.ontology_version)
        try:
            r = await llm.call(prompt, template.output_model)  # type: ignore[arg-type]
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(node=TASK, code=type(exc).__name__, message="요청 글·사실 생성 실패")
            )
            continue
        res.provenance.append(r.provenance)
        out = cast(DraftAnalysisOutput, r.parsed)
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
