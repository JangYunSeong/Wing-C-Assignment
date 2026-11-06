"""기능 개발형 공통 과제 서술 (draft_feature_task LLM) — 1단계 구현 → 1단계 제출 뒤 2단계 디버깅 [확정: 2026-11-06 사용자].

입력은 공고·온톨로지·공개 사례뿐 (불변식 3). 골격 값(역량·수준·기술 환경·난이도·시간)은 LLM 이 바꾸지 않는다.
생성 루프·재요청·고쳐 쓰기 정책은 이슈형과 같다 (spec/draft.py draft_task) — 검사·고쳐 쓰기·자료 섞기만 이 모듈 것.

검증 (설계 docs/design/feature-task-backend.md §6.3·§4.3)
- 이슈형 공통 규칙: 수행준거 id·역량마다 인용, 기술 환경 밖 언어, 1단계 자료 규칙, 원 사례 베끼기·고유어.
- id 참조: 숨김 수용 기준·추가 요구 reveals·허용 설계·2단계 trap_ids·catches_trap 는 traps 안에서만.
  숨은 요구마다 숨김 수용 기준 1개 이상·허용 설계 2개 이상. asks_clarification 은 clarifications 안에서만.
- 숨은 요구 노출 금지: 먼저 말할 개념(1·2단계 first_mention_of)·원 사례 원인 용어가 지원자에게 보이는 곳
  (제목·상황·공개 요구·공개 수용 기준·산출물·자료 제목·추가 요구 답·2단계 상황)에 나오지 않는다.
- 구현 방법 지시 금지 (1차, 금지어): 제목·공개 요구·공개 수용 기준에 구현 수단 이름을 쓰지 않는다.
- 2단계: 자료 id 가 1단계와 겹치지 않음, 2단계 자료 규칙(핵심 1·방해 1 이상), requests_artifact 는 2단계 자료 안에서만.
"""

from __future__ import annotations

import random
import re
from typing import Any, cast

from task_spec.llm import LLMClient
from task_spec.prompting import PromptRegistry
from task_spec.schemas import DebugStage, DraftFeatureOutput, Observable, TaskItem
from task_spec.spec.common import (
    FEATURE_BUILD_MIN,
    FEATURE_DEBUG_MIN,
    FLAG_NO_INCIDENT,
    FLAG_PUBLIC_INCIDENT,
)
from task_spec.spec.draft import (
    _ART_ID,
    DraftContext,
    DraftResult,
    _norm,
    _phrase,
    check,
    check_artifact_list,
    draft_task,
    incident_copied,
    repair,
)

TASK = "draft_feature_task"
# 공개 요구·수용 기준에 나오면 "어떻게"를 알려 주는 말 (구현 수단). 1차 금지어 — 2차는 판정 LLM (평가 PR)
HOWTO_TERMS = (
    "트랜잭션",
    "transaction",
    "락",
    "lock",
    "잠금",
    "비관적",
    "낙관적",
    "멱등",
    "idempotent",
    "idempotency",
    "원자적",
    "atomic",
    "뮤텍스",
    "mutex",
    "세마포어",
    "semaphore",
    "아웃박스",
    "outbox",
    "재시도",
    "retry",
    "커넥션 풀",
    "connection pool",
    "requires_new",
    "for update",
    "버전 컬럼",
    "분산 락",
    "메시지 큐",
    "message queue",
)
_HANGUL = re.compile(r"[가-힣]")


def extra_input() -> dict[str, Any]:
    return {"build_min": FEATURE_BUILD_MIN, "debug_min": FEATURE_DEBUG_MIN}


def _term_rx(term: str) -> re.Pattern[str]:
    """한글 말은 부분 문자열(조사가 붙음), 영문은 단어 경계 (draft._phrase)."""
    return re.compile(re.escape(term)) if _HANGUL.search(term) else _phrase(term)


def _concepts(out: DraftFeatureOutput) -> list[str]:
    obs = [*out.observables, *out.debug_stage.observables]
    vals = [o.value for o in obs if o.kind == "first_mention_of" and len(_norm(o.value)) >= 2]
    return list(dict.fromkeys(vals))


def _dup(label: str, ids: list[str]) -> list[str]:
    return [f"{label} id 가 겹침"] if len(set(ids)) != len(ids) else []


def check_feature(out: DraftFeatureOutput, ctx: DraftContext) -> list[str]:
    """어긋난 점 목록 (비면 통과). 문장은 그대로 재요청 사유가 된다."""
    # 이슈형 공통 규칙 (제목·상황·산출물·1단계 자료 목록의 노출 검사, 1단계 관찰 행동 id 포함)
    problems = check(cast(Any, out), ctx)
    trap_ids = [t.trap_id for t in out.traps]
    traps = set(trap_ids)
    s1 = [a.artifact_id for a in out.artifacts]
    dbg = out.debug_stage
    s2 = [a.artifact_id for a in dbg.artifacts]
    clar = [c.clarification_id for c in out.clarifications]

    problems += _dup("requirements(req_id)", [r.req_id for r in out.requirements])
    problems += _dup("acceptance_public(ac_id)", [a.ac_id for a in out.acceptance_public])
    problems += _dup("acceptance_hidden(ac_id)", [a.ac_id for a in out.acceptance_hidden])
    problems += _dup("clarifications", clar)
    problems += _dup("accepted_designs", [d.design_id for d in out.accepted_designs])

    # id 참조 — 숨은 요구(traps) 안에서만
    for h in out.acceptance_hidden:
        if h.trap_id not in traps:
            problems.append(f"숨김 수용 기준 {h.ac_id} 의 trap_id {h.trap_id} 가 traps 에 없음")
    for c in out.clarifications:
        bad = [t for t in c.reveals if t not in traps]
        if bad:
            problems.append(
                f"추가 요구 {c.clarification_id} 의 reveals 에 없는 trap_id: {', '.join(bad)}"
            )
    for d in out.accepted_designs:
        bad = [t for t in d.trap_ids if t not in traps]
        if bad:
            problems.append(
                f"허용 설계 {d.design_id} 의 trap_ids 에 없는 trap_id: {', '.join(bad)}"
            )
    for t in trap_ids:
        if not any(h.trap_id == t for h in out.acceptance_hidden):
            problems.append(f"숨은 요구 {t} 에 숨김 수용 기준이 없음")
        n = sum(t in d.trap_ids for d in out.accepted_designs)
        if n < 2:
            problems.append(
                f"숨은 요구 {t} 를 만족하는 허용 설계가 {n}개 — 2개 이상 (한 가지 정답으로 감점하지 않게)"
            )
    for o in out.observables:
        if o.kind == "asks_clarification" and o.value not in clar:
            problems.append(f"asks_clarification 값 {o.value} 가 clarifications 의 id 가 아님")
    if out.max_clarifications > len(out.clarifications):
        problems.append("max_clarifications 가 추가 요구 수보다 큼")

    # 2단계
    bad = [t for t in dbg.trap_ids if t not in traps]
    if bad:
        problems.append(f"2단계 trap_ids 에 없는 trap_id: {', '.join(bad)}")
    overlap = sorted(set(s1) & set(s2))
    if overlap:
        problems.append(f"2단계 자료 id 가 1단계 자료와 겹침: {', '.join(overlap)} (번호를 이어서)")
    problems += check_artifact_list(
        dbg.artifacts,
        scenario=dbg.scenario,
        limit=dbg.max_requests,
        min_distractor=1,
        label="2단계 ",
    )
    for o in dbg.observables:
        if o.kind == "requests_artifact" and o.value not in s2:
            problems.append(f"2단계 requests_artifact 값 {o.value} 가 2단계 자료 id 가 아님")
        if o.kind == "catches_trap" and o.value not in traps:
            problems.append(f"2단계 catches_trap 값 {o.value} 가 traps 의 trap_id 가 아님")
        if o.kind == "asks_clarification":
            problems.append(
                "2단계에는 추가 요구(asks_clarification)가 없음 — 1단계 observables 에만"
            )

    # 숨은 요구 노출 — check() 가 보지 않는 곳 + 2단계 개념
    concepts = _concepts(out)
    stage1_only = {o.value for o in out.observables if o.kind == "first_mention_of"}
    extra_visible = "\n".join(
        [
            *(r.text for r in out.requirements),
            *(a.text for a in out.acceptance_public),
            *(c.answer for c in out.clarifications),
        ]
    )
    base_visible = "\n".join(
        [out.title, out.scenario, *out.deliverables, *(a.title for a in out.artifacts)]
    )
    shown = [c for c in concepts if _norm(c) in _norm(extra_visible)]
    shown += [
        c for c in concepts if c not in stage1_only and _norm(c) in _norm(base_visible)
    ]  # 1단계 개념은 check() 가 이미 봤다
    if shown:
        problems.append(
            "먼저 말할 개념이 지원자에게 보이는 곳(공개 요구·수용 기준·추가 요구 답·상황 등)에 나옴: "
            + ", ".join(dict.fromkeys(shown))
        )
    dbg_visible = "\n".join([dbg.scenario, *dbg.deliverables, *(a.title for a in dbg.artifacts)])
    shown2 = [c for c in concepts if _norm(c) in _norm(dbg_visible)]
    if shown2:
        problems.append(
            f"먼저 말할 개념이 2단계 상황·산출물·자료 제목에 나옴 (증상만 써야 함): {', '.join(shown2)}"
        )
    howto_text = "\n".join(
        [out.title, *(r.text for r in out.requirements), *(a.text for a in out.acceptance_public)]
    )
    howto = [w for w in HOWTO_TERMS if _term_rx(w).search(howto_text)]
    if howto:
        problems.append(
            "제목·공개 요구·공개 수용 기준이 구현 방법을 지시함 ("
            + ", ".join(howto)
            + ") — '무엇을'만 쓰고 '어떻게'는 지원자 몫"
        )

    inc = ctx.incident
    if inc is not None:
        raw = "\n".join([extra_visible, dbg.scenario, *dbg.deliverables])
        hidden = [w for w in inc.hidden_terms if _phrase(w).search(raw)]
        if hidden:
            problems.append(
                f"원 사례의 원인 용어가 공개 요구·추가 요구 답·2단계 상황에 나옴: {', '.join(hidden)}"
            )
        dbg_all = "\n".join([dbg.scenario, *(a.content for a in dbg.artifacts), extra_visible])
        copied = incident_copied(dbg_all, inc)
        if copied:
            problems.append(
                f"2단계·공개 요구가 원 사례 문구·수치를 그대로 씀 ({'; '.join(copied)}) — 새로 만들 것"
            )
        leaked = [
            w
            for w in inc.avoid_terms
            if _phrase(w).search(
                "\n".join(
                    [
                        dbg_all,
                        *(d.summary + " " + d.conditions for d in out.accepted_designs),
                        *(h.text for h in out.acceptance_hidden),
                    ]
                )
            )
        ]
        if leaked:
            problems.append(f"원 사례의 고유어가 나옴 (소재를 바꿔야 함): {', '.join(leaked)}")
    return problems


def repair_feature(out: DraftFeatureOutput, ctx: DraftContext) -> tuple[DraftFeatureOutput, bool]:
    """재요청 후에도 어긋나면 잘못된 id 참조만 걷어낸다 (이슈형 repair + 기능형 참조). 문장은 고치지 않는다."""
    fixed_any, anchors_ok = repair(cast(Any, out), ctx)
    fixed = cast(DraftFeatureOutput, fixed_any)
    traps = {t.trap_id for t in fixed.traps}
    clar = {c.clarification_id for c in fixed.clarifications}
    s1 = {a.artifact_id for a in fixed.artifacts}
    dbg = fixed.debug_stage
    s2 = {a.artifact_id for a in dbg.artifacts}

    def keep1(o: Observable) -> bool:
        if o.kind == "asks_clarification":
            return o.value in clar
        if o.kind == "requests_artifact":
            return o.value in s1
        return True

    def keep2(o: Observable) -> bool:
        if o.kind == "catches_trap":
            return o.value in traps
        if o.kind == "requests_artifact":
            return o.value in s2
        return o.kind != "asks_clarification"

    designs = [
        d.model_copy(update={"trap_ids": [t for t in d.trap_ids if t in traps]})
        for d in fixed.accepted_designs
    ]
    dbg_traps = [t for t in dbg.trap_ids if t in traps] or sorted(traps)[:1]
    upd: dict[str, Any] = {
        "acceptance_hidden": [h for h in fixed.acceptance_hidden if h.trap_id in traps],
        "clarifications": [
            c.model_copy(update={"reveals": [t for t in c.reveals if t in traps]})
            for c in fixed.clarifications
        ],
        "max_clarifications": max(1, min(fixed.max_clarifications, len(fixed.clarifications))),
        "accepted_designs": [d for d in designs if d.trap_ids],
        "observables": [o for o in fixed.observables if keep1(o)],
        "debug_stage": dbg.model_copy(
            update={
                "trap_ids": dbg_traps,
                "observables": [o for o in dbg.observables if keep2(o)],
            }
        ),
    }
    return fixed.model_copy(update=upd), anchors_ok


def shuffle_feature(out: DraftFeatureOutput, seed: str) -> DraftFeatureOutput:
    """1·2단계 자료를 단계 안에서 섞고 이어지는 번호로 다시 매긴다 (1단계 a1…aN, 2단계 aN+1…).
    생성 모델이 핵심 자료를 앞에 두는 경향을 지운다 (spec/draft.shuffle_artifacts 와 같은 이유).
    두 단계 id 가 겹치거나 한 단계 안에서 겹치면 섞지 않는다 — 검사가 잡아 재요청한다."""
    s1, s2 = list(out.artifacts), list(out.debug_stage.artifacts)
    ids = [a.artifact_id for a in [*s1, *s2]]
    if len(set(ids)) != len(ids):
        return out
    rnd = random.Random(seed)
    rnd.shuffle(s1)
    rnd.shuffle(s2)
    remap = {a.artifact_id: f"a{i}" for i, a in enumerate([*s1, *s2], 1)}

    def sub(text: str) -> str:
        return _ART_ID.sub(lambda m: remap.get(m.group(0), m.group(0)), text)

    def obs(items: list[Observable]) -> list[Observable]:
        return [
            o.model_copy(update={"value": remap.get(o.value, o.value)})
            if o.kind == "requests_artifact"
            else o
            for o in items
        ]

    anchors = out.behavioral_anchors
    dbg = out.debug_stage
    return out.model_copy(
        update={
            "artifacts": [a.model_copy(update={"artifact_id": remap[a.artifact_id]}) for a in s1],
            "observables": obs(out.observables),
            "scenario": sub(out.scenario),
            "deliverables": [sub(d) for d in out.deliverables],
            "invariant": sub(out.invariant),
            "traps": [
                t.model_copy(
                    update={"description": sub(t.description), "why_ai_fails": sub(t.why_ai_fails)}
                )
                for t in out.traps
            ],
            "clarifications": [
                c.model_copy(update={"answer": sub(c.answer)}) for c in out.clarifications
            ],
            "behavioral_anchors": anchors.model_copy(
                update={
                    k: getattr(anchors, k).model_copy(
                        update={"behavior": sub(getattr(anchors, k).behavior)}
                    )
                    for k in ("level_1", "level_3", "level_5")
                }
            ),
            "debug_stage": dbg.model_copy(
                update={
                    "artifacts": [
                        a.model_copy(update={"artifact_id": remap[a.artifact_id]}) for a in s2
                    ],
                    "observables": obs(dbg.observables),
                    "scenario": sub(dbg.scenario),
                    "deliverables": [sub(d) for d in dbg.deliverables],
                }
            ),
        }
    )


def apply_feature(task: TaskItem, res: DraftResult) -> TaskItem:
    """기능형 골격 TaskItem 에 서술을 붙인다. 2단계 공개 시점·시간은 결정적으로 붙인다."""
    out = cast(DraftFeatureOutput | None, res.output)
    if out is None:
        return task.model_copy(update={"review_flags": [*task.review_flags, *res.flags]})
    dbg = out.debug_stage
    stage = DebugStage(
        time_limit_min=FEATURE_DEBUG_MIN,
        scenario=dbg.scenario,
        trap_ids=list(dbg.trap_ids),
        artifacts=list(dbg.artifacts),
        max_requests=dbg.max_requests,
        deliverables=list(dbg.deliverables),
        observables=list(dbg.observables),
    )
    updated = task.model_copy(
        update={
            "title": out.title,
            "scenario": out.scenario,
            "deliverables": list(out.deliverables),
            "invariant": out.invariant,
            "variable": list(out.variable),
            "traps": list(out.traps),
            "artifacts": list(out.artifacts),
            "max_requests": out.max_requests,
            "observables": list(out.observables),
            "behavioral_anchors": out.behavioral_anchors if res.anchors_ok else None,
            "requirements": list(out.requirements),
            "acceptance_public": list(out.acceptance_public),
            "acceptance_hidden": list(out.acceptance_hidden),
            "clarifications": list(out.clarifications),
            "max_clarifications": out.max_clarifications,
            "accepted_designs": list(out.accepted_designs),
            "debug_stage": stage,
            "provenance_id": res.provenance_id,
            "source_incident": res.incident_id,
            "review_flags": [
                *(
                    FLAG_PUBLIC_INCIDENT if f == FLAG_NO_INCIDENT and res.incident_id else f
                    for f in task.review_flags
                ),
                *res.flags,
            ],
        }
    )
    # model_copy 는 검사를 건너뛴다 — 저장·승인 화면에 올리기 전에 TaskItem 규칙(유형·필드 범위)을 다시 본다
    return TaskItem.model_validate(updated.model_dump())


async def draft_feature_task(
    ctx: DraftContext,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    reviewer_note: str | None = None,
) -> DraftResult:
    """생성 → 검증 → 1회 재요청 → 고쳐 쓰기 (이슈형과 같은 루프, 기능형 검사)."""
    return await draft_task(
        ctx,
        llm=llm,
        prompts=prompts,
        reviewer_note=reviewer_note,
        task=TASK,
        extra_input=extra_input(),
        checker=cast(Any, check_feature),
        repairer=cast(Any, repair_feature),
        shuffler=cast(Any, shuffle_feature),
    )
