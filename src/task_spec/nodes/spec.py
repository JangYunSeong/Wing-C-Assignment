"""④ 명세 초안 (대조는 nodes/compare.py) → 사람 승인(interrupt) → 확정.

승인은 approval 노드의 interrupt() 로만 받는다 (불변식 9). 자동 승인 경로 없음.
공통 과제: 결정적 골격(spec/common.py) → 서술(spec/draft.py, draft_task LLM). 입력은 공고·온톨로지만.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
    PublicIncident,
    RunState,
    SlotAssignment,
    TaskItem,
    TaskKind,
    TaskSpec,
)
from task_spec.spec.build import build_common
from task_spec.spec.draft import TASK as DRAFT_TASK
from task_spec.spec.draft import apply, draft_task, safe_reviewer_note
from task_spec.spec.feature import TASK as FEATURE_TASK
from task_spec.spec.feature import apply_feature, draft_feature_task
from task_spec.spec.incidents import incident_ref
from task_spec.spec.kind import choose_task_kind
from task_spec.spec.questions import TASK as Q_TASK
from task_spec.spec.questions import build_slot_verifications
from task_spec.spec.slots import TASK as SLOT_TASK
from task_spec.spec.slots import build_slots
from task_spec.spec.store import CommonKey, CommonTaskRepo
from task_spec.spec.summary import approval_summary

ApprovalRoute = Literal["finalize", "draft_spec", "__end__"]


FLAG_NO_DRAFTER = "draft_task 미연결 — 결정적 골격만 (제목·상황·행동 기준 없음)"
SKELETON_REF = "skeleton"
FLAG_REUSED_DRAFT = "이 공고의 공통 과제가 아직 승인되지 않은 초안을 재사용함 — 이 명세를 승인하면 그 판이 공고 공통 과제로 승인됨"
FLAG_FEATURE_DEFERRED = (
    "기능 개발형 대상 공고지만 기능형 기본값이 꺼져 있어(평가 전 — feature_task_default) 이슈형으로 생성, "
    "승인자가 수정 요청에서 유형을 기능형으로 바꿀 수 있음 — "
)
FLAG_FEATURE_SKELETON = (
    "기능 개발형 — 서술(draft_feature_task) 미연결, 결정적 골격만 "
    "(공개 요구·숨은 요구·2단계 디버깅 없음)"
)


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
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    """공통 과제 = 결정적 골격 + 서술(LLM). 공고·온톨로지만 쓴다 — 지원자 문서·주장을 읽지 않는다 (불변식 3).

    공고 단위 저장소(store)가 있으면 같은 (공고, 온톨로지 버전, 프롬프트) 의 판을 재사용한다 — 승인된 판 우선.
    revise 로 돌아오면 재사용하지 않고 승인자 수정 요청을 넣어 새 판을 만든다. 경험 검증 과제는 PR-4b.
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
                rule_feature_on=feature_default and feature_ready,
            )
            built = await build_common(
                p,
                state.evidence,
                task_id=f"{spec_id}-common-1",
                ontology_version=state.ontology_version,
                ontology=ontology,
                rel=rel,
                incidents=incidents,
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
                inc = built.ctx.incident if built.ctx is not None else None
                if (
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
                reused = (
                    await store.reusable(key) if store is not None and not redo_common else None
                )
                if reused is not None:
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
                # 경험 검증 = 검증할 주장 → 확장 슬롯 배정(규칙) + 면접 질문(LLM) [확정: 2026-11-03].
                # revise_target=common 이면 직전 배정·질문을 유지 (공통 과제 id 만 갱신)
                if not redo_verify and state.spec is not None:
                    verification = [
                        v.model_copy(update={"parent_task_id": task.task_id})
                        for v in state.spec.verification_tasks
                    ]
                    assignments = list(state.spec.slot_assignments)
                elif (
                    llm_ready
                    and llm is not None
                    and prompts is not None
                    and _has_prompt(prompts, Q_TASK)
                ):
                    assignments, verification, vprov, verrs = await build_slot_verifications(
                        state,
                        task,
                        spec_id=spec_id,
                        llm=llm,
                        prompts=prompts,
                        rel=rel,
                        reassign=revise.reassign if revise is not None else None,
                        slots_approved=(refs[-1].status == "approved") if refs else True,
                    )
                    update = {
                        "provenance": [*update.get("provenance", []), *vprov],
                        "errors": [*update.get("errors", []), *verrs],
                    }
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

    return draft_spec


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
    """승인된 명세 확정. 공통 과제 판도 승인 상태로 바꾼다 (이전 승인 판은 superseded)."""

    async def finalize(state: RunState) -> dict[str, Any]:
        if state.spec is None or state.approval is None or state.approval.status != "approved":
            raise RuntimeError("finalize: 승인되지 않은 명세는 확정할 수 없습니다")
        if store is not None:
            for r in state.spec.common_refs:
                await store.approve(r.common_id, state.approval.reviewer, state.approval.at)
        return {"spec": state.spec.model_copy(update={"approval": state.approval})}

    return finalize


finalize = make_finalize()


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'decision'}: {e['msg']}" for e in exc.errors()
    )
