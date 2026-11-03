"""경험 검증 확장 문항 — 공통 과제 안에 붙는 지원자별 문항 [제안: 2026-11-03 사용자 결정 ①].

- 공통 과제의 상황을 이어 쓰되, 그 지원자의 주장(가명화본) 하나를 확인한다. 최대 2개, 문항당 30분.
- 채점은 비교 점수에 넣지 않는다 — 확인됨/의심됨 플래그 + 면접 질문 (context.md §3). 공통 과제 본체는 바꾸지 않는다 (불변식 3).

주장 고르기 [확정: 2026-11-03 — 방식 B]
  compare 의 claims_to_verify(점수 순)에서 공통 과제 역량과 겹치면 W_COMMON_OVERLAP 만큼 깎고(빼지 않음),
  문항끼리 역량이 겹치지 않게 최대 2개.
기술 환경 [확정]: 주장에 나온 언어 ∩ 공고 언어 → 없으면 공고 언어. 근거는 basis 에.
난이도: 그 역량의 공고 요구 수준 (공고 밖 역량이면 medium). 지원자 역할로 바꾸지 않는다.
검증: 인용 id ⊆ 입력 수행준거(역량마다 1개 이상), 관찰 행동은 first_mention_of 만(함정 없음), 기술 환경,
  원문 베끼기 금지(가명화 토큰·인용의 수치·인용과 긴 문자열 겹침), 핵심 개념이 보이는 곳에 드러나지 않음.
  어긋나면 1회 재요청 → 그래도면 고쳐 쓰고 review_flags.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.nodes.pseudonymize import (
    PseudonymizedNotAvailable,
    derive_pseudonymized,
    load_pseudonymized,
)
from task_spec.nodes.pseudonymize.detectors import TOKEN_RE
from task_spec.ontology.queries import criteria_detail
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    NcsCriterion,
    NodeError,
    Provenance,
    PseudonymizedText,
    RunState,
    TargetCompetency,
    TaskItem,
    TechEnv,
    VerifyTarget,
    VerifyTaskInput,
    VerifyTaskOutput,
)
from task_spec.schemas.domain import Anchor, BehavioralAnchors
from task_spec.spec.common import DIFFICULTY, LEVEL_RANK
from task_spec.spec.draft import DIFFICULTY_KO, NONE, _lang_patterns, _names, _norm

logger = logging.getLogger(__name__)
TASK = "verify_task"
MAX_ITEMS = 2
TIME_LIMIT_MIN = 30
W_COMMON_OVERLAP = 1.5
MAX_CRITERIA = 8
_NUM = re.compile(r"\d{2,}")
COPY_SPAN = 12  # 인용과 이만큼(공백 제외 글자) 겹치면 베끼기

FLAG_VERIFY_FAILED = "확장 문항 생성 실패 — 이 주장은 면접에서 직접 확인 필요"
FLAG_VERIFY_INVALID = "확장 문항 자동 검증 실패 (재요청 후에도) — 고쳐 쓴 항목 확인 필요: "
FLAG_NOT_SCORED = "확장 문항 — 비교 점수에 넣지 않음 (확인됨/의심됨 + 면접 질문)"


@dataclass
class VerifyContext:
    claim_id: str
    claim_text: PseudonymizedText
    quote: str
    targets: list[TargetCompetency]
    criteria: list[NcsCriterion]
    env: TechEnv
    difficulty: str
    common_task: str
    sub_label: str
    industry_label: str
    ontology_version: str
    language_labels: list[str] = field(default_factory=list)

    @property
    def criterion_ids(self) -> set[str]:
        return {c.criterion_id for c in self.criteria}


@dataclass
class VerifyResult:
    output: VerifyTaskOutput | None = None
    anchors_ok: bool = True
    provenance_id: str | None = None
    provenance: list[Provenance] = field(default_factory=list)
    errors: list[NodeError] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    first_problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 결정적 부분


def pick_targets(targets: list[VerifyTarget], common_ids: set[str]) -> list[VerifyTarget]:
    """공통 과제 역량과 겹치면 감점(빼지 않음), 문항끼리 역량이 겹치지 않게 최대 MAX_ITEMS."""

    def adjusted(t: VerifyTarget) -> float:
        return t.score - (W_COMMON_OVERLAP if set(t.competency_ids) & common_ids else 0.0)

    chosen: list[VerifyTarget] = []
    used: set[str] = set()
    for t in sorted(targets, key=lambda t: (-adjusted(t), t.claim_id)):
        if set(t.competency_ids) & used:
            continue
        chosen.append(t)
        used |= set(t.competency_ids)
        if len(chosen) == MAX_ITEMS:
            break
    return chosen


def verify_tech_env(
    claim_text: str, posting_languages: list[str], language_labels: list[str]
) -> TechEnv:
    """주장에 나온 언어 ∩ 공고 언어 → 없으면 공고 언어."""
    posting = {n.lower(): n for n in posting_languages}
    found = [
        posting[name.lower()]
        for name, rx in _lang_patterns(language_labels).items()
        if name.lower() in posting and rx.search(claim_text)
    ]
    found = list(dict.fromkeys(found))
    if found:
        return TechEnv(languages=found, basis="지원자 주장의 언어 ∩ 공고 언어")
    return TechEnv(languages=list(posting_languages), basis="공고 언어 (주장과 겹치는 언어 없음)")


def item_difficulty(levels: list[str]) -> str:
    rank = max((LEVEL_RANK.get(lv, 0) for lv in levels), default=-1)
    return DIFFICULTY.get(rank, "medium" if rank < 0 else "low")


# ---------------------------------------------------------------- LLM 입력·검증


def build_input(ctx: VerifyContext, note: str | None = None) -> VerifyTaskInput:
    comps = "\n".join(
        f"- {t.competency_id} {t.label} (공고 요구 수준: {t.level or '공고 밖'})"
        for t in ctx.targets
    )
    crit = "\n".join(
        f"[{c.criterion_id}] {c.unit_name} > {c.element_name}: {c.text}" for c in ctx.criteria
    )
    env = (
        ", ".join(ctx.env.languages + ctx.env.frameworks) or "(언어 없음 — 특정 언어에 묶이지 않게)"
    )
    return VerifyTaskInput(
        sub=ctx.sub_label,
        industry=ctx.industry_label,
        common_task=ctx.common_task,
        claim=ctx.claim_text,
        competencies=comps,
        ncs_criteria=crit,
        difficulty=DIFFICULTY_KO.get(ctx.difficulty, ctx.difficulty),
        time_limit_min=TIME_LIMIT_MIN,
        tech_env=env,
        revision_note=note or NONE,
    )


def _copied(text: str, quote: str) -> list[str]:
    """지원자 원문 베끼기: 가명화 토큰, 인용의 2자리 이상 수치, 인용과 COPY_SPAN 글자 이상 겹침."""
    out: list[str] = []
    if TOKEN_RE.search(text):
        out.append("가명화 토큰")
    nums = sorted({n for n in _NUM.findall(quote) if n in text})
    if nums:
        out.append("인용의 수치 " + ", ".join(nums))
    q, t = _norm(quote), _norm(text)
    if any(q[i : i + COPY_SPAN] in t for i in range(0, max(0, len(q) - COPY_SPAN + 1))):
        out.append(f"인용과 {COPY_SPAN}자 이상 같은 문장")
    return out


def check(out: VerifyTaskOutput, ctx: VerifyContext) -> list[str]:
    problems: list[str] = []
    allowed = ctx.criterion_ids
    cited: set[str] = set()
    for name in ("level_1", "level_3", "level_5"):
        a: Anchor = getattr(out.behavioral_anchors, name)
        bad = [i for i in a.criterion_ids if i not in allowed]
        if bad:
            problems.append(f"{name} 가 목록에 없는 수행준거 id 를 인용: {', '.join(bad)}")
        cited |= set(a.criterion_ids) & allowed
    for t in ctx.targets:
        if t.ncs_criterion_ids and not cited & set(t.ncs_criterion_ids):
            problems.append(
                f"행동 기준이 역량 {t.competency_id} 의 수행준거를 하나도 인용하지 않음"
            )
    if any(o.kind != "first_mention_of" for o in out.observables):
        problems.append("확장 문항에는 함정이 없으므로 관찰 행동은 first_mention_of 만 씀")
    tech_ok = _names(ctx.env.languages + ctx.env.frameworks)
    langs = _names(ctx.language_labels)
    extra = [x for x in out.tech_used if x.strip().lower() in langs - tech_ok]
    if extra:
        problems.append(f"기술 환경 밖 언어 요구: {', '.join(extra)}")
    visible = "\n".join([out.title, out.scenario, *out.deliverables])
    mentioned = [
        n
        for n, rx in _lang_patterns(ctx.language_labels).items()
        if n.lower() not in tech_ok and rx.search(visible)
    ]
    if mentioned:
        problems.append(f"본문에 기술 환경 밖 언어가 나옴: {', '.join(sorted(mentioned))}")
    copied = _copied(visible + "\n" + "\n".join(out.follow_up_questions), ctx.quote)
    if copied:
        problems.append("지원자 원문을 그대로 씀 (표면을 바꿔야 함): " + ", ".join(copied))
    nv = _norm(visible)
    revealed = [
        o.value
        for o in out.observables
        if o.kind == "first_mention_of" and len(_norm(o.value)) >= 2 and _norm(o.value) in nv
    ]
    if revealed:
        problems.append(f"핵심 개념이 제목·상황·제출물에 드러남: {', '.join(revealed)}")
    return problems


def repair(out: VerifyTaskOutput, ctx: VerifyContext) -> tuple[VerifyTaskOutput, bool]:
    allowed = ctx.criterion_ids
    levels: dict[str, Anchor] = {}
    for name in ("level_1", "level_3", "level_5"):
        a: Anchor = getattr(out.behavioral_anchors, name)
        ids = [i for i in a.criterion_ids if i in allowed]
        if ids:
            levels[name] = Anchor(behavior=a.behavior, criterion_ids=ids)
    obs = [o for o in out.observables if o.kind == "first_mention_of"] or list(out.observables)
    upd: dict[str, object] = {"observables": obs}
    ok = len(levels) == 3
    if ok:
        upd["behavioral_anchors"] = BehavioralAnchors(**levels)
    return out.model_copy(update=upd), ok


async def draft_verify(
    ctx: VerifyContext,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    reviewer_note: str | None = None,
) -> VerifyResult:
    template = prompts.get(TASK)
    res = VerifyResult()
    note = f"승인자 수정 요청: {reviewer_note}" if reviewer_note else None
    best: tuple[VerifyTaskOutput, list[str], str] | None = None
    for attempt in range(2):
        prompt = assemble(template, build_input(ctx, note), ontology_version=ctx.ontology_version)
        try:
            r = await llm.call(prompt, VerifyTaskOutput)
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(
                    node=TASK,
                    code=type(exc).__name__,
                    message="확장 문항 생성 실패",
                    ref_id=ctx.claim_id,
                )
            )
            continue
        res.provenance.append(r.provenance)
        problems = check(r.parsed, ctx)
        logger.info(
            "verify_task attempt=%d problems=%d latency_ms=%d",
            attempt + 1,
            len(problems),
            r.provenance.latency_ms,
        )
        if best is None or len(problems) < len(best[1]):
            best = (r.parsed, problems, r.provenance.provenance_id)
        if not problems:
            break
        if attempt == 0:
            res.first_problems = list(problems)
            res.errors.append(
                NodeError(
                    node=TASK,
                    code="verify_invalid",
                    message=f"자동 검증 실패 {len(problems)}건 — 재요청",
                    ref_id=ctx.claim_id,
                )
            )
            lines = "\n".join(f"- {p}" for p in problems)
            note = "\n".join(
                x for x in (note, f"이전 답의 문제 (고쳐서 다시 쓰세요):\n{lines}") if x
            )
    if best is None:
        res.flags.append(FLAG_VERIFY_FAILED)
        return res
    out, problems, res.provenance_id = best
    if problems:
        out, res.anchors_ok = repair(out, ctx)
        res.flags.append(FLAG_VERIFY_INVALID + "; ".join(problems))
    res.output = out
    return res


def apply_verify(base: TaskItem, res: VerifyResult) -> TaskItem:
    out = res.output
    if out is None:
        return base.model_copy(update={"review_flags": [*base.review_flags, *res.flags]})
    return base.model_copy(
        update={
            "title": out.title,
            "scenario": out.scenario,
            "deliverables": list(out.deliverables),
            "observables": list(out.observables),
            "behavioral_anchors": out.behavioral_anchors if res.anchors_ok else None,
            "follow_up_questions": list(out.follow_up_questions),
            "provenance_id": res.provenance_id,
            "review_flags": [*base.review_flags, *res.flags],
        }
    )


# ---------------------------------------------------------------- 실행 1건 조립


async def build_verifications(
    state: RunState,
    common: TaskItem,
    *,
    spec_id: str,
    ontology: OntologyRepository,
    llm: LLMClient,
    prompts: PromptRegistry,
    sub_label: str,
    industry_label: str,
    language_labels: list[str],
    reviewer_note: str | None = None,
) -> tuple[list[TaskItem], list[Provenance], list[NodeError]]:
    """공통 과제 하나에 붙는 확장 문항들. 지원자 텍스트는 가명화본에서만 (derive_pseudonymized)."""
    p = state.posting_profile
    c = state.comparison
    if p is None or c is None or not c.claims_to_verify:
        return [], [], []
    common_ids = {t.competency_id for t in common.target_competencies}
    picked = pick_targets(c.claims_to_verify, common_ids)
    claims = {x.claim_id: x for x in state.claims}
    posting = {x.competency_id: x for x in p.competencies}
    labels = {r.competency_id: r.label for x in state.candidates for r in x.candidates}
    labels.update({k: v.label for k, v in posting.items()})
    ev = {e.competency_id: e for e in state.evidence}
    posting_langs = [t.label for t in p.tools if t.kind == "language"]
    common_text = f"{common.title or ''}\n{common.scenario or ''}".strip()

    items: list[TaskItem] = []
    prov: list[Provenance] = []
    errors: list[NodeError] = []
    sources: dict[str, PseudonymizedText | None] = {}
    prepared: list[tuple[TaskItem, VerifyContext]] = []  # 근거 조회는 차례로, LLM 생성은 동시에
    for i, vt in enumerate(picked, 1):
        claim = claims.get(vt.claim_id)
        if claim is None:
            continue
        if claim.doc_id not in sources:
            ref = next((d for d in state.pseudo_docs if d.doc_id == claim.doc_id), None)
            try:
                sources[claim.doc_id] = (
                    await asyncio.to_thread(load_pseudonymized, ref) if ref else None
                )
            except PseudonymizedNotAvailable:
                sources[claim.doc_id] = None
        src = sources[claim.doc_id]
        try:
            if src is None:
                raise ValueError("가명화본 없음")
            text = derive_pseudonymized(src, quote=claim.quote, summary=claim.action)
        except ValueError:
            errors.append(
                NodeError(
                    node=TASK,
                    code="claim_not_pseudonymized",
                    message="주장 텍스트 검사 실패",
                    ref_id=claim.claim_id,
                )
            )
            continue
        targets: list[TargetCompetency] = []
        crit_all: list[NcsCriterion] = []
        levels: list[str] = []
        for comp in vt.competency_ids:
            units = [
                u.unit_cl_cd for u in (ev[comp].ncs_units if comp in ev else []) if u.n_criteria > 0
            ]
            crit, _ = await criteria_detail(ontology, units, max_criteria=MAX_CRITERIA)
            pc = posting.get(comp)
            lv = max(pc.levels, key=lambda x: LEVEL_RANK.get(x, 0)) if pc and pc.levels else None
            if pc:
                levels += pc.levels
            targets.append(
                TargetCompetency(
                    competency_id=comp,
                    label=labels.get(comp, comp),
                    level=lv,
                    ncs_unit_ids=units,
                    ncs_criterion_ids=[x.criterion_id for x in crit],
                )
            )
            crit_all += [x for x in crit if x not in crit_all]
        if not crit_all:
            errors.append(
                NodeError(
                    node=TASK,
                    code="no_evidence",
                    message="NCS 근거 없음 — 생략",
                    ref_id=claim.claim_id,
                )
            )
            continue
        env = verify_tech_env(f"{claim.action} {claim.quote}", posting_langs, language_labels)
        base = TaskItem(
            task_id=f"{spec_id}-verify-{i}",
            track="claim_verification",
            target_competencies=targets,
            claims_to_verify=[claim.claim_id],
            parent_task_id=common.task_id,
            tech_env=env,
            difficulty_target=item_difficulty(levels),  # type: ignore[arg-type]
            time_limit_min=TIME_LIMIT_MIN,
            review_flags=[FLAG_NOT_SCORED],
        )
        ctx = VerifyContext(
            claim_id=claim.claim_id,
            claim_text=text,
            quote=claim.quote,
            targets=targets,
            criteria=crit_all,
            env=env,
            difficulty=base.difficulty_target,
            common_task=common_text or "(공통 과제 서술 없음)",
            sub_label=sub_label,
            industry_label=industry_label,
            ontology_version=state.ontology_version,
            language_labels=language_labels,
        )
        prepared.append((base, ctx))
    # 확장 문항끼리는 독립이라 동시에 만든다 (차례로 하면 draft_spec 대기 시간이 문항 수만큼 늘어남)
    results = await asyncio.gather(
        *(
            draft_verify(ctx, llm=llm, prompts=prompts, reviewer_note=reviewer_note)
            for _, ctx in prepared
        )
    )
    for (base, _), res in zip(prepared, results, strict=True):
        items.append(apply_verify(base, res))
        prov += res.provenance
        errors += res.errors
    return items, prov, errors
