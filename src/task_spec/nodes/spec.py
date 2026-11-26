"""④ 명세 초안 (대조는 nodes/compare.py) → 사람 승인(interrupt) → 확정.

승인은 approval 노드의 interrupt() 로만 받는다 (불변식 9). 자동 승인 경로 없음.
공통 과제: 결정적 골격(spec/common.py) → 서술(spec/draft.py, draft_task LLM). 입력은 공고·온톨로지만.
공고에 채용담당자 인터뷰로 승인된 공통 과제가 있으면 만들지 않고 그 과제를 쓴다 [확정: 2026-11-19 사용자].
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from langgraph.graph import END
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from task_spec.llm import LLMClient
from task_spec.nodes.compare import compare_claims
from task_spec.ontology.relatedness import Relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptError, PromptRegistry
from task_spec.schemas import (
    ApprovalDecision,
    CommonRef,
    NodeError,
    PostingProfile,
    Provenance,
    PublicIncident,
    RunState,
    SlotAssignment,
    TaskItem,
    TaskKind,
    TaskSpec,
)
from task_spec.spec.analysis_request import TASK as ANALYSIS_TASK
from task_spec.spec.analysis_request import apply_analysis, draft_analysis_request
from task_spec.spec.build import build_common
from task_spec.spec.company_incidents import (
    FLAG_COMPANY_REUSED,
    CompanyIncident,
    CompanyIncidentRepo,
    CompanyResolver,
    company_open,
    feature_fit,
    is_company_incident,
    pick_company_incident,
)
from task_spec.spec.draft import (
    FLAG_DRAFT_FAILED,
    DraftContext,
    apply,
    draft_task,
    safe_reviewer_note,
)
from task_spec.spec.draft import TASK as DRAFT_TASK
from task_spec.spec.feature import TASK as FEATURE_TASK
from task_spec.spec.feature import apply_feature, draft_feature_task, uses_seed
from task_spec.spec.incidents import feature_incident, feature_ref, incident_ref, with_incident
from task_spec.spec.kind import choose_task_kind
from task_spec.spec.questions import TASK as Q_TASK
from task_spec.spec.questions import build_slot_verifications
from task_spec.spec.slots import TASK as SLOT_TASK
from task_spec.spec.slots import build_slots
from task_spec.spec.store import (
    CommonKey,
    CommonTaskRepo,
    StoredCommon,
    is_interview_ref,
    replaced_common,
)
from task_spec.spec.summary import approval_summary

ApprovalRoute = Literal["finalize", "draft_spec", "__end__"]


FLAG_NO_DRAFTER = "draft_task 미연결 — 결정적 골격만 (제목·상황·행동 기준 없음)"
SKELETON_REF = "skeleton"
FLAG_REUSED_DRAFT = "이 공고의 공통 과제로 아직 승인하지 않은 초안을 재사용함 — 이 명세를 승인하면 그 판을 공고 공통 과제로 승인함"
FLAG_FEATURE_DEFERRED = (
    "기능 개발형 대상 공고. 기능형 기본값이 비활성이라(평가 전 — feature_task_default) 이슈형으로 생성, "
    "승인자가 수정 요청에서 유형을 기능형으로 변경할 수 있음 — "
)
FLAG_FEATURE_SKELETON = (
    "기능 개발형 — 서술(draft_feature_task) 미연결, 결정적 골격만 "
    "(공개 요구·숨은 요구·2단계 디버깅 없음)"
)
FLAG_INTERVIEW_COMMON = (
    "채용담당자 인터뷰로 승인한 이 공고의 공통 과제를 그대로 사용함 — 과제를 수정하려면 인터뷰 화면에서 "
    "(이 명세에서는 경험 검증만 다시 생성할 수 있음)"
)
FLAG_INTERVIEW_SWITCHED = "이 공고의 공통 과제 변경(인터뷰 과제 승인·교체)에 따라 지금 인터뷰 과제로 다시 생성함 — 이전 과제: "
# 실행 만들 때 담당자가 고른 문제 [확정: 2026-11-20 사용자 — 같은 공고에 문제가 여럿이면 실행 만들 때 고른다]
FLAG_CHOSEN_COMMON = (
    "실행을 생성할 때 담당자가 선택한 문제(승인한 공통 과제)를 그대로 사용함 — 과제는 이 명세에서 수정하지 않음 "
    "(경험 검증만 다시 생성할 수 있음): "
)
FLAG_CHOSEN_GONE = "실행을 생성할 때 선택한 문제를 저장소에서 발견하지 못함 — 다른 과제로 변경하지 않고 공통 과제 없이 유지함: "


def feature_key(inc: PublicIncident, ctx: DraftContext | None) -> str:
    """기능형 판 키의 재료 지문 — 재료 인터페이스(#256)가 있으면 그 지문도 (인터페이스 전에 만든 명세를 다시 쓰지 않게)."""
    return feature_ref(inc, ctx.interface if ctx is not None else None)


def _uses_incident(prompts: PromptRegistry, task: str = DRAFT_TASK) -> bool:
    return "incident" in prompts.get(task).input_model.model_fields


def _has_prompt(prompts: PromptRegistry | None, task: str) -> bool:
    if prompts is None:
        return False
    try:
        prompts.get(task)
    except PromptError:
        return False
    return True


async def choose_kind(
    profile: PostingProfile,
    posting_id: str,
    ontology_version: str,
    *,
    store: CommonTaskRepo | None,
    requested: TaskKind | None,
    current: TaskKind | None,
    rule_feature_on: bool,
) -> tuple[TaskKind, list[str]]:
    """공통 과제 유형 (spec/kind.py 우선순위): 승인자 지정 → (revise 중) 지금 명세의 유형 →
    이 공고에서 승인된 판의 유형 → 기본값 규칙.

    revise 에 유형이 없으면 직전 명세의 유형을 유지한다 — 기능형으로 바꾼 뒤 문구만 고치거나 확장 문항만 다시 만들 때
    유형이 기본값으로 되돌아가지 않게.

    기본값 규칙이 feature 인데 기본값이 꺼져 있으면(rule_feature_on=False — 평가 전이거나 기능형 서술 미연결)
    incident 로 만든다 (지금 동작 유지) — 명세 단위 flag. 승인자 지정·승인된 판이 feature 면 그대로 feature
    (기능형 서술이 없으면 골격만, 과제 flag).
    """
    if requested is not None:
        return requested, []
    if current is not None:
        return current, []
    if store is not None:
        approved = await store.approved_kind(posting_id, ontology_version)
        if approved is not None:
            return approved, []
    choice = choose_task_kind(profile)
    if choice.kind == "feature" and not rule_feature_on:
        return "incident", [FLAG_FEATURE_DEFERRED + choice.basis]
    return choice.kind, []


def make_draft_spec(
    ontology: OntologyRepository | None,
    rel: Relatedness | None = None,
    llm: LLMClient | None = None,
    prompts: PromptRegistry | None = None,
    store: CommonTaskRepo | None = None,
    incidents: dict[str, PublicIncident] | None = None,
    feature_default: bool = False,
    company_incidents: CompanyIncidentRepo | None = None,
    company_of: CompanyResolver | None = None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    """공통 과제 = 결정적 골격 + 서술(LLM). 공고·온톨로지만 쓴다 — 지원자 문서·주장을 읽지 않는다 (불변식 3).

    사례: 같은 회사에서 공유 승인된 회사 결정적 사건(company_incidents, 회사 = company_of(공고))을 공개 사례보다 먼저 쓴다
    [확정: 2026-11-19 사용자] — 공고마다 처음 정한 사례를 계속 쓴다 (spec/company_incidents.py).

    공고 단위 저장소(store)가 있으면 같은 (공고, 온톨로지 버전, 프롬프트) 의 판을 재사용한다 — 승인된 판 우선.
    revise 로 돌아오면 재사용하지 않고 승인자 수정 요청을 넣어 새 판을 만든다. 경험 검증만 다시 만들면
    (revise_target=verification) 저장소를 다시 보지 않고 명세의 공통 과제를 그대로 쓴다 (#234).
    """
    drafter_ready = llm is not None and prompts is not None
    # 기능형 서술(draft_feature_task) — 프롬프트가 있으면 승인자 지정·승인된 판에 쓴다.
    # 기본값 규칙으로 기능형을 고르는 것은 feature_default(설정 feature_task_default, 평가 PR-4 뒤 켬)까지 막는다
    feature_ready = drafter_ready and _has_prompt(prompts, FEATURE_TASK)

    async def draft_spec(state: RunState) -> dict[str, Any]:
        state, lowered = _apply_downgrade(state, rel)
        revision = state.spec.revision + 1 if state.spec is not None else 1
        spec_id = f"{state.run_id}-r{revision}"
        common: list[TaskItem] = []
        verification: list[TaskItem] = []
        assignments: list[SlotAssignment] = []
        refs: list[CommonRef] = []
        flags: list[str] = []
        update: dict[str, Any] = {}
        p = state.posting_profile
        if p is not None:
            revise = (
                state.approval if state.approval and state.approval.status == "revise" else None
            )
            redo_common = revise is not None and revise.revise_target != "verification"
            # 담당자가 고른 문제가 있으면 그것 (실행을 만든 뒤 교체돼도 이 지원자에게 고른 것 그대로) — 없으면 공고의 인터뷰 과제
            chosen = (
                await store.get(state.common_id)
                if store is not None and state.common_id is not None
                else None
            )
            if state.common_id is not None and chosen is None:
                return _out(
                    state,
                    spec_id=spec_id,
                    revision=revision,
                    common=[],
                    verification=[],
                    refs=[],
                    assignments=[],
                    flags=[FLAG_CHOSEN_GONE + state.common_id],
                    update={
                        "errors": [
                            NodeError(
                                node="draft_spec",
                                code="chosen_common_gone",
                                message="실행 만들 때 고른 문제가 저장소에 없음 — 다른 과제로 바꾸지 않음",
                                ref_id=state.common_id,
                            )
                        ]
                    },
                    lowered=lowered,
                )
            interview = chosen or (
                await store.interview_task(state.posting_id, state.ontology_version)
                if store is not None
                else None
            )
            if interview is not None:
                picked = _interview_common(state, interview, revise, chosen=chosen is not None)
                common.append(picked.task)
                # 경험 검증(슬롯 배정·면접 질문)은 주장과 과제 슬롯만 쓴다 — P1 골격의 서술 입력(ctx)이 없어도 만든다
                refs.append(picked.ref)
                flags = picked.flags
                update = picked.update
                made = await _verification(
                    state,
                    picked.task,
                    spec_id=spec_id,
                    redo=picked.redo_verify,
                    llm=llm if drafter_ready else None,
                    prompts=prompts,
                    rel=rel,
                    revise=revise,
                    slots_approved=True,
                )
                verification, assignments = made.tasks, made.assignments
                update = made.merged(update)
                return _out(
                    state,
                    spec_id=spec_id,
                    revision=revision,
                    common=common,
                    verification=verification,
                    refs=refs,
                    assignments=assignments,
                    flags=flags,
                    update=update,
                    lowered=lowered,
                )
            # 회사 결정적 사건 후보 — 공통 과제가 이미 있는 공고는 그 사례 그대로 (공개 사례로 시작했으면 바꾸지 않는다, 불변식 3).
            # 경험 검증만 다시 만들면(공통 과제 유지, #234) 고르지 않는다 — 유지한 과제의 사례 표시만 다시 센다
            keeping = (
                revise is not None
                and not redo_common
                and state.spec is not None
                and bool(state.spec.common_tasks)
            )
            company_pool, posting_used = (
                ([], [])
                if keeping
                else await _company_pool(
                    company_incidents, company_of, store, state.posting_id, p.sub or ""
                )
            )
            open_to_company = company_open(posting_used, company_pool)
            # v2 기능형은 그 직군 사례(공개·회사)에 기능형 재료(feature)가 있어야 한다 — 없으면 기본값 규칙에서 뺀다
            seed_ok = bool(
                (inc0 := feature_incident(incidents, p.sub or "", p.posting_id)) and inc0.feature
            ) or (
                open_to_company and any(feature_fit(c.incident, p.sub or "") for c in company_pool)
            )
            seed_needed = feature_ready and prompts is not None and uses_seed(prompts)
            kind, kind_flags = await choose_kind(
                p,
                state.posting_id,
                state.ontology_version,
                store=store,
                requested=revise.task_kind if revise is not None and redo_common else None,
                current=(
                    state.spec.common_tasks[0].task_kind
                    if revise is not None and state.spec is not None and state.spec.common_tasks
                    else None
                ),
                rule_feature_on=feature_default and feature_ready and (seed_ok or not seed_needed),
            )
            catalog = incidents
            if keeping:
                kind_flags = [*kind_flags, *await _kept_company_flags(store, state)]
            elif store is not None and (company_pool or posting_used):
                # 공유 사례가 없어도 부른다 — 그 공고가 쓰던 회사 사례가 보관됐으면 바뀐다고 표시해야 한다
                pick = await pick_company_incident(
                    company_pool,
                    kind=kind,
                    sub=p.sub or "",
                    posting_id=state.posting_id,
                    prev=posting_used,
                    common=store,
                )
                if pick.incident is not None:
                    catalog = with_incident(incidents, pick.incident)
                kind_flags = [*kind_flags, *pick.flags]
            built = await build_common(
                p,
                state.evidence,
                task_id=f"{spec_id}-common-1",
                ontology_version=state.ontology_version,
                ontology=ontology,
                rel=rel,
                incidents=catalog,
                task_kind=kind,
            )
            flags = [*built.flags, *kind_flags]
            task = built.task
            if task is not None:
                llm_ready = built.ctx is not None and drafter_ready  # 경험 검증 질문(LLM)용
                draft_name = FEATURE_TASK if kind == "feature" else DRAFT_TASK
                use_llm = llm_ready and (kind == "incident" or feature_ready)
                if use_llm and prompts is not None:
                    prompt_ref = prompts.get(draft_name).ref  # 유형마다 프롬프트가 달라 다른 판
                else:
                    # 유형이 다르면 다른 판 (같은 공고라도)
                    prompt_ref = f"{SKELETON_REF}+feature" if kind == "feature" else SKELETON_REF
                use_slots = use_llm and ontology is not None and _has_prompt(prompts, SLOT_TASK)
                if use_slots and prompts is not None:
                    # 슬롯 프롬프트가 바뀌어도 새 판 — 공통 과제와 슬롯은 함께 승인된다
                    prompt_ref = f"{prompt_ref}+{prompts.get(SLOT_TASK).ref}"
                # 문제 분석 판(요청 글·사실, analysis-stage.md §5) — CURRENT 가 있을 때만 (평가 전에는 건너뜀, 2026-11-16 사용자).
                # 판이 바뀌면 공통 과제도 새 판 — 예전 판(요청 글 없음)과 섞이지 않게
                use_analysis = use_llm and kind == "feature" and _has_prompt(prompts, ANALYSIS_TASK)
                if use_analysis and prompts is not None:
                    prompt_ref = f"{prompt_ref}+{prompts.get(ANALYSIS_TASK).ref}"
                inc = built.ctx.incident if built.ctx is not None else None
                if (
                    use_llm
                    and prompts is not None
                    and inc is not None
                    and kind == "feature"
                    and inc.feature is not None
                    and uses_seed(prompts)
                ):
                    # 기능형 재료 지문 — 재료를 고치면 기능형 새 판 (이슈형 판은 그대로)
                    prompt_ref = f"{prompt_ref}+{feature_key(inc, built.ctx)}"
                elif (
                    use_llm
                    and prompts is not None
                    and inc is not None
                    and _uses_incident(prompts, draft_name)
                ):
                    # 사례 내용이 바뀌어도 새 판 — 고치기 전 사례로 만든 초안을 다시 쓰지 않게
                    prompt_ref = f"{prompt_ref}+{incident_ref(inc)}"
                key = CommonKey(state.posting_id, state.ontology_version, prompt_ref)
                note, note_flags = safe_reviewer_note(revise.comment if revise else None)
                flags = [*flags, *note_flags]  # 수정 요청을 보내지 못했으면 명세 단위로 알린다
                redo_verify = revise is None or revise.revise_target != "common"
                # 경험 검증만 다시 만들면 명세의 공통 과제를 그대로 쓴다 (#234) — 저장소에서 다시 고르면
                # 그 사이 승인된 판·다른 실행의 새 판·바뀐 프롬프트의 새 판으로 바뀌어 승인자가 본 과제가 사라진다
                prev = state.spec if revise is not None and not redo_common else None
                kept = prev.common_tasks[0] if prev is not None and prev.common_tasks else None
                reused = (
                    await store.reusable(key)
                    if store is not None and not redo_common and kept is None
                    else None
                )
                if (
                    reused is not None
                    and reused.status != "approved"
                    and FLAG_DRAFT_FAILED in reused.task.review_flags
                ):
                    # 서술 생성 실패(공급자 일시 장애 등)로 골격만 남은 초안은 다시 쓰지 않는다 — 이 실행이 다시 만든다. 예전에는 한 번의
                    # 장애가 승인자가 수정 요청할 때까지 그 공고의 모든 지원자 명세를 골격으로 만들었다 (설계 검사 #332 S-F9).
                    # 승인된 판은 사람이 정한 것이라 그대로 쓴다
                    reused = None
                if kept is not None and prev is not None:
                    # 이 판에서 공통 과제를 만들지 않는다 (LLM 호출 없음) — provenance 는 이미 State 에 있다
                    task = kept
                    refs.extend(r.model_copy(update={"reused": True}) for r in prev.common_refs)
                elif reused is not None:
                    task = reused.task
                    if reused.status != "approved":
                        task = task.model_copy(
                            update={"review_flags": [*task.review_flags, FLAG_REUSED_DRAFT]}
                        )
                    update = {"provenance": reused.provenance}
                    refs.append(
                        CommonRef(
                            common_id=reused.id,
                            revision=reused.revision,
                            status=reused.status,
                            reused=True,
                            prompt_ref=prompt_ref,
                        )
                    )
                else:
                    if not use_llm:
                        no_draft = FLAG_FEATURE_SKELETON if kind == "feature" else FLAG_NO_DRAFTER
                        task = task.model_copy(
                            update={"review_flags": [*task.review_flags, no_draft]}
                        )
                    else:
                        assert built.ctx is not None and llm is not None and prompts is not None
                        if kind == "feature":
                            res = await draft_feature_task(
                                built.ctx,
                                llm=llm,
                                prompts=prompts,
                                reviewer_note=note if redo_common else None,
                            )
                            task = apply_feature(task, res)
                        else:
                            res = await draft_task(
                                built.ctx,
                                llm=llm,
                                prompts=prompts,
                                reviewer_note=note if redo_common else None,
                            )
                            task = apply(task, res)
                        update = {"provenance": res.provenance, "errors": res.errors}
                        if use_analysis and task.title:
                            ares = await draft_analysis_request(
                                task,
                                built.ctx,
                                llm=llm,
                                prompts=prompts,
                                reviewer_note=note if redo_common else None,
                            )
                            task = apply_analysis(task, ares)
                            update = {
                                "provenance": [*update["provenance"], *ares.provenance],
                                "errors": [*update["errors"], *ares.errors],
                            }
                        if use_slots and task.title:
                            assert ontology is not None
                            sres = await build_slots(
                                p,
                                state.evidence,
                                task,
                                built.ctx,
                                ontology=ontology,
                                llm=llm,
                                prompts=prompts,
                                reviewer_note=note if redo_common else None,
                            )
                            task = task.model_copy(
                                update={
                                    "slots": sres.slots,
                                    "review_flags": [*task.review_flags, *sres.flags],
                                }
                            )
                            update = {
                                "provenance": [*update["provenance"], *sres.provenance],
                                "errors": [*update["errors"], *sres.errors],
                            }
                    if store is not None:
                        saved = await store.save_draft(key, task, update.get("provenance", []))
                        task = saved.task
                        refs.append(
                            CommonRef(
                                common_id=saved.id,
                                revision=saved.revision,
                                status=saved.status,
                                reused=False,
                                prompt_ref=prompt_ref,
                            )
                        )
                common.append(task)
                made = await _verification(
                    state,
                    task,
                    spec_id=spec_id,
                    redo=redo_verify,
                    llm=llm if llm_ready else None,
                    prompts=prompts,
                    rel=rel,
                    revise=revise,
                    slots_approved=(refs[-1].status == "approved") if refs else True,
                )
                verification, assignments = made.tasks, made.assignments
                update = made.merged(update)
        return _out(
            state,
            spec_id=spec_id,
            revision=revision,
            common=common,
            verification=verification,
            refs=refs,
            assignments=assignments,
            flags=flags,
            update=update,
            lowered=lowered,
        )

    return draft_spec


async def _company_pool(
    company_incidents: CompanyIncidentRepo | None,
    company_of: CompanyResolver | None,
    store: CommonTaskRepo | None,
    posting_id: str,
    sub: str,
) -> tuple[list[CompanyIncident], list[str]]:
    """(그 회사·직군의 공유된 회사 사례, 그 공고의 공통 과제가 쓴 사례 id — 승인된 것 먼저).

    공유 사례가 없어도(전부 보관 등) 공고가 쓴 사례는 읽는다 — 쓰던 회사 사례가 사라졌으면 바뀐다고 표시하려고."""
    if company_incidents is None or store is None:
        return [], []
    used = await store.posting_incidents(posting_id)
    company = await company_of(posting_id) if company_of is not None and sub else None
    pool = await company_incidents.shared(company, sub) if company else []
    return pool, used


async def _kept_company_flags(store: CommonTaskRepo | None, state: RunState) -> list[str]:
    """경험 검증만 다시 만들어 공통 과제를 유지할 때 — 그 과제의 회사 사례를 다른 공고가 쓰는지만 다시 센다."""
    inc = (
        state.spec.common_tasks[0].source_incident
        if state.spec and state.spec.common_tasks
        else None
    )
    if store is None or inc is None or not is_company_incident(inc):
        return []
    n = (await store.incident_usage([inc], exclude_posting=state.posting_id)).get(inc, 0)
    return [FLAG_COMPANY_REUSED.format(id=inc, n=n)] if n else []


def _out(
    state: RunState,
    *,
    spec_id: str,
    revision: int,
    common: list[TaskItem],
    verification: list[TaskItem],
    refs: list[CommonRef],
    assignments: list[SlotAssignment],
    flags: list[str],
    update: dict[str, Any],
    lowered: list[NodeError] | None,
) -> dict[str, Any]:
    """draft_spec 출력 — 명세 + State 갱신 (공통 과제 경로마다 같다)."""
    d = state.approval
    if (
        d is not None
        and d.status == "revise"
        and d.revise_target == "common"
        and d.reassign
        and state.spec is not None
    ):
        # 공통 과제만 다시 만들면 직전 배정을 그대로 둔다 — 함께 온 슬롯 배정 바꾸기는 반영하지 않고
        # 기록만 (#249, 예전에는 기록 없이 버렸다). 공통 과제를 못 만든 판도 배정을 바꾸지 않으므로 같다
        update["errors"] = [*update.get("errors", []), *_reassign_ignored(state, d)]
    spec = TaskSpec(
        spec_id=spec_id,
        run_id=state.run_id,
        posting_id=state.posting_id,
        ontology_version=state.ontology_version,
        revision=revision,
        common_tasks=common,
        verification_tasks=verification,
        common_refs=refs,
        slot_assignments=assignments,
        review_flags=flags,
    )
    out: dict[str, Any] = {"spec": spec, "approval": None, **update}
    if lowered is not None:
        out["comparison"] = state.comparison
        out["errors"] = [*lowered, *out.get("errors", [])]
    return out


@dataclass(frozen=True)
class _Verification:
    tasks: list[TaskItem]
    assignments: list[SlotAssignment]
    provenance: list[Provenance] | None = (
        None  # None = 새로 만들지 않음 (update 를 건드리지 않는다)
    )
    errors: list[NodeError] = field(default_factory=list)

    def merged(self, update: dict[str, Any]) -> dict[str, Any]:
        if self.provenance is None:
            return update
        return {
            **update,
            "provenance": [*update.get("provenance", []), *self.provenance],
            "errors": [*update.get("errors", []), *self.errors],
        }


async def _verification(
    state: RunState,
    task: TaskItem,
    *,
    spec_id: str,
    redo: bool,
    llm: LLMClient | None,
    prompts: PromptRegistry | None,
    rel: Relatedness | None,
    revise: ApprovalDecision | None,
    slots_approved: bool,
) -> _Verification:
    """경험 검증 = 검증할 주장 → 확장 슬롯 배정(규칙) + 면접 질문(LLM) [확정: 2026-11-03].

    다시 만들지 않으면(revise_target=common) 직전 배정·질문을 유지 (공통 과제 id 만 갱신). llm=None 이면 만들지 않는다.
    """
    if not redo and state.spec is not None:
        kept = [
            v.model_copy(update={"parent_task_id": task.task_id})
            for v in state.spec.verification_tasks
        ]
        return _Verification(kept, list(state.spec.slot_assignments))
    if llm is not None and prompts is not None and _has_prompt(prompts, Q_TASK):
        assignments, tasks, vprov, verrs = await build_slot_verifications(
            state,
            task,
            spec_id=spec_id,
            llm=llm,
            prompts=prompts,
            rel=rel,
            reassign=revise.reassign if revise is not None else None,
            slots_approved=slots_approved,
        )
        return _Verification(tasks, assignments, vprov, verrs)
    return _Verification([], [])


@dataclass(frozen=True)
class _Picked:
    task: TaskItem
    ref: CommonRef
    flags: list[str]
    update: dict[str, Any]
    redo_verify: bool


def _interview_common(
    state: RunState,
    interview: StoredCommon,
    revise: ApprovalDecision | None,
    *,
    chosen: bool = False,
) -> _Picked:
    """채용담당자 인터뷰로 승인된 공고 공통 과제를 그대로 쓴다 [확정: 2026-11-19 사용자] — 만들지도 고치지도 않는다.

    - 유형·본문·슬롯은 그 과제 것 (승인자 유형 지정·수정 요청의 공통 과제 부분은 반영하지 않고 기록만 — API 가 먼저 막는다).
    - 경험 검증만 다시 만드는 수정 요청(#234)도 저장소를 다시 본다: 인터뷰 과제는 채용담당자가 명시적으로 교체할 때만
      바뀌고, 바뀐 뒤에는 이전 과제를 가리키는 명세를 승인할 수 없다 (API 409·finalize). 과제가 바뀌었으면 배정·질문도 다시.
    - 이전 과제는 명세의 저장 참조(common_refs)로 비교한다. provenance 는 과제가 이 실행에 새로 들어올 때, State 에 아직 없는
      것만 싣는다 (State 는 누적).
    """
    prev = state.spec.common_refs[0].common_id if state.spec and state.spec.common_refs else None
    new_to_run = prev != interview.id  # 첫 초안·공통 과제가 없던 명세·다른 과제를 가리키던 명세
    # chosen: 실행 만들 때 담당자가 고른 문제 — 인터뷰 과제와 같이 그대로 쓰고 고치지 않는다 (실행 동안 바뀌지 않는다)
    flags = [FLAG_CHOSEN_COMMON + interview.id] if chosen else [FLAG_INTERVIEW_COMMON]
    if prev is not None and new_to_run and not chosen:
        flags.append(FLAG_INTERVIEW_SWITCHED + prev)
    seen = {p.provenance_id for p in state.provenance}
    prov = [p for p in interview.provenance if p.provenance_id not in seen] if new_to_run else []
    update: dict[str, Any] = {"provenance": prov} if prov else {}
    redo_common = revise is not None and revise.revise_target != "verification"
    if redo_common:
        assert revise is not None
        what = "실행 만들 때 고른 문제" if chosen else "인터뷰로 승인된 공통 과제"
        where = (
            "다른 문제로 바꾸려면 실행을 새로 만든다" if chosen else "과제는 인터뷰 화면에서 고친다"
        )
        update["errors"] = [
            NodeError(
                node="draft_spec",
                code="chosen_common_locked" if chosen else "interview_common_locked",
                message=f"{what}는 지원자 명세에서 고치지 않음 — 수정 요청의 공통 과제 부분"
                f"({revise.revise_target}{', 유형 바꾸기' if revise.task_kind else ''})은 반영하지 않음. "
                f"{where}",
                ref_id=interview.id,
            )
        ]
    redo_verify = revise is None or revise.revise_target != "common" or new_to_run
    ref = CommonRef(
        common_id=interview.id,
        revision=interview.revision,
        status=interview.status,
        reused=True,
        prompt_ref=interview.prompt_ref,
    )
    return _Picked(interview.task, ref, flags, update, redo_verify)


def _reassign_ignored(state: RunState, d: ApprovalDecision) -> list[NodeError]:
    """공통 과제만 다시 만드는(revise_target=common) 수정 요청의 슬롯 배정 바꾸기 → 반영하지 않았다는 기록 (#249).

    결정 검증기로 막지 않는다 — 저장된 체크포인트의 결정은 노드 입력(State)을 만들 때 mode="after" 검증기가
    다시 돌아 깨진다. 화면은 common 일 때 배정 바꾸기를 숨긴다. 승인자 입력이므로 아는 주장 id 만 ref_id 로 남긴다.
    """
    known = {c.claim_id for c in state.claims}
    return [
        NodeError(
            node="draft_spec",
            code="reassign_ignored",
            message="공통 과제만 다시 만드는 수정 요청이라 슬롯 배정 바꾸기를 반영하지 않음 (배정 유지) — "
            "경험 검증만 또는 전부로 다시 요청하세요",
            ref_id=cid if cid in known else None,
        )
        for cid in d.reassign
    ]


def _apply_downgrade(
    state: RunState, rel: Relatedness | None
) -> tuple[RunState, list[NodeError] | None]:
    """revise 의 겹침 낮추기·되돌리기·올리기 → 대조를 다시 계산.

    - 낮추기: 겹침 → 관련 경험 (검증 후보에서도 그 연결을 뺀다). 승인자가 올린 역량을 낮추면 올림만 취소 (원래 자리로).
    - 되돌리기: 낮춘 역량을 대조 규칙대로.
    - 올리기 [2026-11-06]: 선택만 일치(select_only) 역량 → 겹침 (출처 promoted). 낮춘 역량은 바로 올리지 않는다
      (되돌리기 restore 를 먼저 — 같은 결정에 같은 id 를 두 목록에 넣을 수 없다).
    낮춤·올림은 대조(State)에 누적된다 — 다음 revise 에서 다시 보내지 않아도 유지.
    이미 낮춘·올린 역량을 다시 보내면 조용히 무시. 그 밖에 맞지 않는 id 는 무시하고 기록만
    (승인자 입력이므로 공고 역량 id 일 때만 ref_id 로 남긴다).
    """
    d = state.approval
    if (
        d is None
        or d.status != "revise"
        or not (d.downgrade_overlap or d.restore_overlap or d.promote_overlap)
        or state.comparison is None
    ):
        return state, None
    cmp = state.comparison
    overlap = {m.competency_id for m in cmp.overlap}
    lowered = set(cmp.downgraded)
    raised = set(cmp.promoted)
    unsure = {m.competency_id for m in [*cmp.related, *cmp.posting_only] if m.select_only}
    posting = set(state.posting_profile.competency_ids) if state.posting_profile else set()

    def ignored(code: str, message: str, c: str) -> NodeError:
        return NodeError(
            node="draft_spec", code=code, message=message, ref_id=c if c in posting else None
        )

    down = list(dict.fromkeys(d.downgrade_overlap))
    back = list(dict.fromkeys(d.restore_overlap))
    up = list(dict.fromkeys(d.promote_overlap))
    notes = (
        [
            ignored("downgrade_ignored", "겹침이 아닌 역량은 낮출 수 없음", c)
            for c in down
            if c not in overlap and c not in lowered
        ]
        + [
            ignored("restore_ignored", "낮추지 않은 역량은 되돌릴 수 없음", c)
            for c in back
            if c not in lowered
        ]
        + [
            ignored(
                "promote_ignored", "낮춘 역량은 올릴 수 없음 — 되돌리기(restore)를 먼저 쓰세요", c
            )
            for c in up
            if c in lowered
        ]
        + [
            ignored("promote_ignored", "선택만 일치가 아닌 역량은 올릴 수 없음", c)
            for c in up
            if c not in lowered and c not in unsure and c not in raised
        ]
    )
    # 올린 역량을 낮추면 올림 취소 (낮춘 목록에는 넣지 않는다 — 원래 자리가 겹침이 아니므로)
    keep_up = [
        c
        for c in [*cmp.promoted, *(c for c in up if c in unsure and c not in lowered)]
        if c not in down
    ]
    keep_down = [
        c
        for c in [*cmp.downgraded, *(c for c in down if c in overlap and c not in raised)]
        if c not in back
    ]
    new = compare_claims(state, rel, keep_down, keep_up)
    return state.model_copy(update={"comparison": new}), notes


async def approval(state: RunState) -> Command[ApprovalRoute]:
    """사람 승인. interrupt() 이전에 부작용을 두지 않는다 (resume 시 노드가 처음부터 다시 돈다).

    잘못된 결정이 들어오면 사유를 담아 다시 interrupt 한다.
    """
    if state.spec is None:
        raise RuntimeError("approval: spec 이 없습니다")
    payload: dict[str, Any] = {
        "run_id": state.run_id,
        "summary": approval_summary(state).model_dump(mode="json"),
        "spec": state.spec.model_dump(mode="json"),
        "provenance": [p.model_dump(mode="json", by_alias=True) for p in state.provenance],
        "error": None,
    }
    while True:
        raw = interrupt(payload)
        try:
            decision = ApprovalDecision.model_validate(raw)
            break
        except ValidationError as exc:
            payload = {**payload, "error": _summarize(exc)}

    goto: dict[str, ApprovalRoute] = {
        "approved": "finalize",
        "revise": "draft_spec",
        "rejected": END,
    }
    return Command(update={"approval": decision}, goto=goto[decision.status])


def make_finalize(
    store: CommonTaskRepo | None = None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    """승인된 명세 확정. 공통 과제 판도 승인 상태로 바꾼다 (이전 승인 판은 superseded).

    인터뷰 과제는 다시 승인하지 않는다 — 채용담당자 승인 기록을 덮지 않고, 교체된 과제를 되살리지 않게 [확정: 2026-11-19 사용자].
    대신 명세의 공통 과제가 그 공고의 지금 인터뷰 과제인지 확인한다 (API 가 승인 전에 409 로 막는다).
    """

    async def finalize(state: RunState) -> dict[str, Any]:
        if state.spec is None or state.approval is None or state.approval.status != "approved":
            raise RuntimeError("finalize: 승인되지 않은 명세는 확정할 수 없습니다")
        if store is not None:
            ids = [r.common_id for r in state.spec.common_refs]
            gone = await store.missing(ids)
            if gone:  # API 가 승인 전에 막지만 (409), 그 사이 지워진 경우에도 사유를 남긴다
                raise CommonTaskMissing(gone)
            stale = await replaced_common(store, state.spec, state.common_id)
            if stale:  # API 가 승인 전에 막는다 (409). 확인 뒤 확정 전에 교체되면 교체 직전에 확정된 명세와 같다
                raise CommonTaskNotCurrent(stale)
            for r in state.spec.common_refs:
                # 인터뷰 과제·고른 문제는 이미 승인된 과제 — 다시 승인하면 원래 승인 기록이 덮인다
                if not is_interview_ref(r.prompt_ref) and r.common_id != state.common_id:
                    await store.approve(r.common_id, state.approval.reviewer, state.approval.at)
        return {"spec": state.spec.model_copy(update={"approval": state.approval})}

    return finalize


finalize = make_finalize()


class CommonTaskNotCurrent(RuntimeError):
    """명세가 가리키는 공통 과제를 이 공고가 이제 쓰지 않는다 (인터뷰 과제 승인·교체) — 경험 검증 다시 만들기로 명세를 새로 만든다."""

    def __init__(self, common_ids: list[str]) -> None:
        self.common_ids = common_ids
        super().__init__(
            f"이 공고의 공통 과제가 바뀌었습니다 (이 명세의 과제: {', '.join(common_ids)}) — "
            "경험 검증 다시 만들기(revise_target=verification)로 지금 과제를 반영한 명세를 만드세요"
        )


class CommonTaskMissing(RuntimeError):
    """명세가 가리키는 공통 과제 판이 저장소에 없다 (DB 정리 등) — 같은 공고로 명세를 다시 만든다."""

    def __init__(self, common_ids: list[str]) -> None:
        self.common_ids = common_ids
        super().__init__(
            f"공통 과제가 저장소에 없습니다: {', '.join(common_ids)} — 같은 공고로 명세를 다시 만드세요"
        )


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'decision'}: {e['msg']}" for e in exc.errors()
    )
