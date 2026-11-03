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
from task_spec.ontology.relatedness import Relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptError, PromptRegistry
from task_spec.schemas import (
    ApprovalDecision,
    ApprovalSummary,
    CommonRef,
    ComparisonBrief,
    OutsideBrief,
    PublicIncident,
    RunState,
    TaskBrief,
    TaskItem,
    TaskSpec,
    VerifyBrief,
)
from task_spec.spec.build import build_common
from task_spec.spec.draft import TASK as DRAFT_TASK
from task_spec.spec.draft import apply, draft_task, safe_reviewer_note
from task_spec.spec.slots import TASK as SLOT_TASK
from task_spec.spec.slots import build_slots
from task_spec.spec.store import CommonKey, CommonTaskRepo
from task_spec.spec.verify import build_verifications

ApprovalRoute = Literal["finalize", "draft_spec", "__end__"]


FLAG_NO_DRAFTER = "draft_task 미연결 — 결정적 골격만 (제목·상황·행동 기준 없음)"
SKELETON_REF = "skeleton"
FLAG_REUSED_DRAFT = "이 공고의 공통 과제가 아직 승인되지 않은 초안을 재사용함 — 이 명세를 승인하면 그 판이 공고 공통 과제로 승인됨"


def _has_prompt(prompts: PromptRegistry | None, task: str) -> bool:
    if prompts is None:
        return False
    try:
        prompts.get(task)
    except PromptError:
        return False
    return True


def make_draft_spec(
    ontology: OntologyRepository | None,
    rel: Relatedness | None = None,
    llm: LLMClient | None = None,
    prompts: PromptRegistry | None = None,
    store: CommonTaskRepo | None = None,
    incidents: dict[str, PublicIncident] | None = None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    """공통 과제 = 결정적 골격 + 서술(LLM). 공고·온톨로지만 쓴다 — 지원자 문서·주장을 읽지 않는다 (불변식 3).

    공고 단위 저장소(store)가 있으면 같은 (공고, 온톨로지 버전, 프롬프트) 의 판을 재사용한다 — 승인된 판 우선.
    revise 로 돌아오면 재사용하지 않고 승인자 수정 요청을 넣어 새 판을 만든다. 경험 검증 과제는 PR-4b.
    """
    drafter_ready = llm is not None and prompts is not None

    async def draft_spec(state: RunState) -> dict[str, Any]:
        revision = state.spec.revision + 1 if state.spec is not None else 1
        spec_id = f"{state.run_id}-r{revision}"
        common: list[TaskItem] = []
        verification: list[TaskItem] = []
        refs: list[CommonRef] = []
        flags: list[str] = []
        update: dict[str, Any] = {}
        p = state.posting_profile
        if p is not None:
            built = await build_common(
                p,
                state.evidence,
                task_id=f"{spec_id}-common-1",
                ontology_version=state.ontology_version,
                ontology=ontology,
                rel=rel,
                incidents=incidents,
            )
            flags = built.flags
            task = built.task
            if task is not None:
                use_llm = built.ctx is not None and drafter_ready
                prompt_ref = prompts.get(DRAFT_TASK).ref if use_llm and prompts else SKELETON_REF
                use_slots = use_llm and ontology is not None and _has_prompt(prompts, SLOT_TASK)
                if use_slots and prompts is not None:
                    # 슬롯 프롬프트가 바뀌어도 새 판 — 공통 과제와 슬롯은 함께 승인된다
                    prompt_ref = f"{prompt_ref}+{prompts.get(SLOT_TASK).ref}"
                key = CommonKey(state.posting_id, state.ontology_version, prompt_ref)
                revise = (
                    state.approval if state.approval and state.approval.status == "revise" else None
                )
                note, note_flags = safe_reviewer_note(revise.comment if revise else None)
                flags = [*flags, *note_flags]  # 수정 요청을 보내지 못했으면 명세 단위로 알린다
                redo_common = revise is not None and revise.revise_target != "verification"
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
                        task = task.model_copy(
                            update={"review_flags": [*task.review_flags, FLAG_NO_DRAFTER]}
                        )
                    else:
                        assert built.ctx is not None and llm is not None and prompts is not None
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
                # 경험 검증 확장 문항 (지원자별, 공통 과제 안에 붙음). revise_target=common 이면 직전 것을 유지
                if not redo_verify and state.spec is not None:
                    verification = [
                        v.model_copy(update={"parent_task_id": task.task_id})
                        for v in state.spec.verification_tasks
                    ]
                elif built.ctx is not None and use_llm and ontology is not None:
                    assert llm is not None and prompts is not None
                    verification, vprov, verrs = await build_verifications(
                        state,
                        task,
                        spec_id=spec_id,
                        ontology=ontology,
                        llm=llm,
                        prompts=prompts,
                        sub_label=built.ctx.sub_label,
                        industry_label=built.ctx.industry_label,
                        language_labels=built.ctx.language_labels,
                        reviewer_note=note if revise is not None else None,
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
            review_flags=flags,
        )
        return {"spec": spec, "approval": None, **update}

    return draft_spec


def approval_summary(state: RunState) -> ApprovalSummary:
    """승인자가 먼저 볼 것. 원문·가명화 매핑·주장 인용은 넣지 않는다 (인증 전 — privacy-auditor W1)."""
    spec = state.spec
    assert spec is not None

    def brief(t: TaskItem) -> TaskBrief:
        return TaskBrief(
            task_id=t.task_id,
            track=t.track,
            title=t.title,
            competencies=[c.label for c in t.target_competencies],
            difficulty=t.difficulty_target,
            time_limit_min=t.time_limit_min,
            n_traps=len(t.traps),
            has_anchors=t.behavioral_anchors is not None,
            review_flags=t.review_flags,
        )

    tasks = [brief(t) for t in [*spec.common_tasks, *spec.verification_tasks]]
    slots = [brief(x) for t in spec.common_tasks for x in t.slots]
    c = state.comparison
    comparison = (
        ComparisonBrief(
            overlap=[m.label for m in c.overlap],
            posting_only=[m.label for m in c.posting_only],
            applicant_only=[
                OutsideBrief(label=m.label, relatedness=m.relatedness) for m in c.applicant_only
            ],
            uncovered_claims=len(c.uncovered),
            claims_to_verify=[
                VerifyBrief(claim_id=v.claim_id, score=v.score, reasons=v.reasons)
                for v in c.claims_to_verify
            ],
        )
        if c is not None
        else None
    )
    return ApprovalSummary(
        review_flags=spec.review_flags,
        tasks=tasks,
        slots=slots,
        common=spec.common_refs,
        total_time_min=sum(t.time_limit_min for t in tasks),
        comparison=comparison,
    )


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
