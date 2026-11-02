"""④ 대조 (결정적, LLM 없음): 공고 역량 ↔ 지원자 주장에서 고른 역량.

- overlap        공고 역량 중 주장이 연결된 것 (근거 주장 id)
- posting_only   공고 역량 중 주장이 없는 것
- applicant_only 주장에서 고른 역량 중 공고에 없는 것
- uncovered      맞는 역량이 온톨로지에 없는 주장 (온톨로지 공백 후보)
- claims_to_verify 경험 검증 과제 후보 주장 (점수 순, 최대 MAX_VERIFY)

검증 후보 점수 [확정: 2026-11-02] — 공고와 관련 있고, 지원자 기여가 크고, 확인할 거리가 있는 주장 먼저.
  공고 주 역량 +3 · 공고 보조 역량 +2
  공고 밖 역량은 직무 연계(ontology/relatedness.py)로: 강 +2 · 중 +1 · 약 = 후보 제외 (목록엔 남김)
    연계 데이터가 없으면(단위 테스트 그래프) +1, 사유 '직무 연계 확인 불가'
  역할 설계 +2 · 주도 +1.5 · 참여 +0.5 · 판정 안 함 0 (사유에 '역할 확인 필요')
  결과·수치 있음 +1 · 이력서↔자기소개서 어긋남 +2 (T2 구현 후)
역량을 하나도 고르지 못한 주장(uncovered 포함)은 후보에서 뺀다 — 무엇을 검증할지 정할 수 없다.
공통 과제는 이 결과로 바꾸지 않는다 (불변식 3) — draft_spec 이 공고 역량만으로 만든다.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from task_spec.ontology.relatedness import Relatedness
from task_spec.schemas import (
    Claim,
    ClaimCandidates,
    Comparison,
    CompetencyMatch,
    RunState,
    VerifyTarget,
)

MAX_VERIFY = 5
W_POSTING = {True: 3.0, False: 2.0}  # 공고 주 / 보조
W_OUTSIDE = {"strong": 2.0, "medium": 1.0}  # 약 = 제외
W_UNKNOWN = 1.0
W_ROLE = {"design": 2.0, "lead": 1.5, "participate": 0.5}
W_RESULT = 1.0
W_MISMATCH = 2.0
LEVEL_KO = {"strong": "강", "medium": "중", "weak": "약"}
ROLE_KO = {"design": "설계", "lead": "주도", "participate": "참여"}
_NUM = re.compile(r"\d")


def _has_result(c: Claim) -> bool:
    return bool(c.result and c.result.strip()) or bool(_NUM.search(c.quote))


def compare_claims(state: RunState, rel: Relatedness | None = None) -> Comparison:
    p = state.posting_profile
    posting = {x.competency_id: x for x in p.competencies} if p else {}
    claims = {c.claim_id: c for c in state.claims}
    picked: dict[str, ClaimCandidates] = {
        x.claim_id: x for x in state.candidates if x.claim_id in claims
    }
    labels = {r.competency_id: r.label for x in picked.values() for r in x.candidates}
    labels.update({cid: x.label for cid, x in posting.items()})

    by_comp: dict[str, list[str]] = {}
    for cid, x in sorted(picked.items()):
        for comp in x.competency_ids:
            by_comp.setdefault(comp, []).append(cid)

    sub = p.sub if p else None
    levels = {
        k: rel.level(k, sub, posting) for k in by_comp if k not in posting and rel is not None
    }

    def match(comp: str) -> CompetencyMatch:
        pc = posting.get(comp)
        lv = levels.get(comp)
        return CompetencyMatch(
            competency_id=comp,
            label=labels.get(comp, comp),
            in_posting=pc is not None,
            primary=pc.primary if pc else None,
            claim_ids=by_comp.get(comp, []),
            relatedness=lv[0] if lv else None,
            relatedness_reason=lv[1] if lv else None,
        )

    mismatch = {
        cid
        for link in state.links
        if link.relation == "mismatch"
        for cid in (link.resume_claim_id, link.cover_letter_claim_id)
    }

    targets: list[VerifyTarget] = []
    for cid, x in sorted(picked.items()):
        if not x.competency_ids:
            continue
        c = claims[cid]
        score = 0.0
        reasons: list[str] = []
        prim = [k for k in x.competency_ids if k in posting and posting[k].primary]
        supp = [k for k in x.competency_ids if k in posting and not posting[k].primary]
        if prim:
            score += W_POSTING[True]
            reasons.append("공고 주 역량: " + ", ".join(labels.get(k, k) for k in prim))
        elif supp:
            score += W_POSTING[False]
            reasons.append("공고 보조 역량: " + ", ".join(labels.get(k, k) for k in supp))
        elif rel is None:
            score += W_UNKNOWN
            reasons.append("공고 밖 역량 (직무 연계 확인 불가)")
        else:
            outside = [(levels[k], k) for k in x.competency_ids if k in levels]
            best = [(lv, k) for lv, k in outside if lv[0] != "weak"]
            if not best:
                continue  # 직무 연계 약함 → 검증 후보 아님 (applicant_only 목록에는 남음)
            (lv, why), k = max(best, key=lambda b: W_OUTSIDE[b[0][0]])
            score += W_OUTSIDE[lv]
            reasons.append(f"공고 밖 역량 {labels.get(k, k)} — 직무 연계 {LEVEL_KO[lv]} ({why})")
        if c.role in W_ROLE:
            score += W_ROLE[c.role]
            reasons.append(f"역할 {ROLE_KO[c.role]} ({c.role_basis})")
        else:
            reasons.append("역할 확인 필요")
        if _has_result(c):
            score += W_RESULT
            reasons.append("결과·수치 있음")
        if cid in mismatch:
            score += W_MISMATCH
            reasons.append("이력서·자기소개서 어긋남")
        targets.append(
            VerifyTarget(
                claim_id=cid,
                competency_ids=list(x.competency_ids),
                role=c.role,
                score=score,
                reasons=reasons,
            )
        )
    targets.sort(key=lambda t: (-t.score, t.claim_id))

    return Comparison(
        overlap=[match(k) for k in posting if k in by_comp],
        posting_only=[match(k) for k in posting if k not in by_comp],
        applicant_only=[match(k) for k in sorted(by_comp) if k not in posting],
        uncovered=sorted(cid for cid, x in picked.items() if x.uncovered),
        claims_to_verify=targets[:MAX_VERIFY],
    )


def make_compare(
    rel: Relatedness | None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def compare(state: RunState) -> dict[str, Any]:
        return {"comparison": compare_claims(state, rel)}

    return compare
