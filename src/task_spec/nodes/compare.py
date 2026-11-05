"""④ 대조 (결정적, LLM 없음): 공고 역량 ↔ 지원자 주장. 두 경로를 규칙으로 합친다.

- 선택 경로 (L0): retrieve_t1 이 주장마다 고른 역량 (주장 → 역량)
- 확인 경로 (TD): verify_overlap 이 공고 역량마다 주장 전체를 보고 판정 (공고 역량 → 주장)

합치는 규칙 [확정: 2026-11-04 사용자, 선택만 일치 제외 2026-11-06 사용자] — 공고 역량 하나는 아래 셋 중 하나에만:
- overlap        확인 action → 겹침. 선택도 고르면 출처 both(두 경로 일치), 아니면 verify(공고 역량 확인)
                 확인을 하지 않은 역량(예전 실행·실패·빠짐)은 선택 경로만으로 겹침 (출처 없음)
                 승인자가 올린 선택만 일치 역량 (promoted, 출처 promoted)
- related        확인 related → 관련 경험 (겹침 아님). 승인자가 내린 겹침도 여기 (downgraded)
- posting_only   그 밖 (확인 mention·none 포함)
선택만 일치(select_only) [확정: 2026-11-06 사용자]: 확인 경로가 있고 action 이 아닌데 선택 경로만 고른 공고 역량은
  겹침이 아니다 — 확인 related 면 related, none·mention 이면 posting_only, 둘 다 select_only=True (확인 필요 표시).
  근거: v2 e2e (25쌍 × 2회, 최종 정답)에서 선택만 일치의 action 정밀도 0.14–0.2. 빼면 운영 겹침
  정밀도 0.733 → 0.827 · 0.719 → 0.830, 재현율 0.901 → 0.878 · 0.939 → 0.893 (오프라인 재계산).
  주장 → 역량 연결(선택 경로)은 그대로라 그 주장은 검증 후보로 남고 사유에 SELECT_ONLY 를 붙인다 (확인 질문 대상).
  승인자가 revise + promote_overlap 으로 겹침으로 올릴 수 있다 (출처 promoted, Comparison.promoted 에 누적,
  downgrade_overlap 으로 다시 내리면 promoted 에서 빠지고 원래 자리로). 올린 역량의 검증 후보 사유는 '승인자가 올림'.
  출처 select 는 예전 규칙의 자동 겹침 — 새 계산은 만들지 않는다. 예전 실행을 revise 로 다시 계산하면 새 규칙으로
  자리가 바뀌고, 승인자는 예전 select 겹침을 겹침 낮추기로 내릴 수 있다.
- applicant_only 선택 경로에서 고른 역량 중 공고에 없는 것
- uncovered      맞는 역량이 온톨로지에 없는 주장 (온톨로지 공백 후보) — 확인 경로 근거가 된 주장은 뺀다
- claims_to_verify 경험 검증 과제 후보 주장 (점수 순, 최대 MAX_VERIFY)
역할은 근거 주장의 칸 규칙 역할 (assign_roles, 불변식 6) — 확인 경로 근거로 정해지면 그것, 아니면 선택 경로 근거.
선택만 일치(select_only)인 공고 역량이 걸린 검증 후보는 사유에 SELECT_ONLY 문구를 붙인다 (승인 화면 표시와 같은 말).
승인자가 올린 역량이면 SELECT_ONLY 대신 PROMOTED_KO('승인자가 올림').

검증 후보 점수 [확정: 2026-11-02] — 공고와 관련 있고, 지원자 기여가 크고, 확인할 거리가 있는 주장 먼저.
  공고 주 역량 +3 · 공고 보조 역량 +2
  공고 밖 역량은 직무 연계(ontology/relatedness.py)로: 강 +2 · 중 +1 · 약 = 후보 제외 (목록엔 남김)
    연계 데이터가 없으면(단위 테스트 그래프) +1, 사유 '직무 연계 확인 불가'
  역할 설계 +2 · 주도 +1.5 · 참여 +0.5 · 판정 안 함 0 (사유에 '역할 확인 필요')
  결과·수치 있음 +1 · 이력서↔자기소개서 어긋남 +2 (T2 구현 후)
  주장의 역량 = 선택 경로에서 고른 역량 + 그 주장이 확인 경로 action 근거인 공고 역량 (승인자가 내린 것 제외).
역량이 하나도 없는 주장(uncovered 포함)은 후보에서 뺀다 — 무엇을 검증할지 정할 수 없다.
공통 과제는 이 결과로 바꾸지 않는다 (불변식 3) — draft_spec 이 공고 역량만으로 만든다.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from task_spec.ontology.relatedness import Relatedness
from task_spec.schemas import (
    Claim,
    ClaimCandidates,
    Comparison,
    CompetencyMatch,
    OverlapCheck,
    RunState,
    VerifyTarget,
)
from task_spec.schemas.domain import OverlapSource, Role

MAX_VERIFY = 5
W_POSTING = {True: 3.0, False: 2.0}  # 공고 주 / 보조
W_OUTSIDE = {"strong": 2.0, "medium": 1.0}  # 약 = 제외
W_UNKNOWN = 1.0
W_ROLE = {"design": 2.0, "lead": 1.5, "participate": 0.5}
W_RESULT = 1.0
W_MISMATCH = 2.0
LEVEL_KO = {"strong": "강", "medium": "중", "weak": "약"}
ROLE_KO = {"design": "설계", "lead": "주도", "participate": "참여"}
ROLE_RANK = {"design": 3, "lead": 2, "participate": 1}
CHECKED = "(확인)"  # 검증 후보 사유에서 확인 경로로만 이어진 공고 역량 표시
# 선택만 일치(select_only) 표시 문구 — 검증 후보 사유와 승인 화면(app.js SELECT_ONLY_KO)이 같은 말을 쓴다
SELECT_ONLY = "선택만 일치(확인 경로 근거 없음)"
PROMOTED_KO = "승인자가 올림"  # 승인 화면 SOURCE_KO.promoted 와 같은 말
_NUM = re.compile(r"\d")


def _has_result(c: Claim) -> bool:
    return bool(c.result and c.result.strip()) or bool(_NUM.search(c.quote))


def best_role(claim_ids: Iterable[str], claims: dict[str, Claim]) -> Role | None:
    roles = [claims[c].role for c in claim_ids if c in claims and claims[c].role]
    return max(roles, key=lambda r: ROLE_RANK[r]) if roles else None  # type: ignore[arg-type,index]


def compare_claims(
    state: RunState,
    rel: Relatedness | None = None,
    downgraded: Iterable[str] = (),
    promoted: Iterable[str] = (),
) -> Comparison:
    """downgraded = 승인자가 겹침에서 관련 경험으로 내린 공고 역량 id,
    promoted = 승인자가 겹침으로 올린 선택만 일치 공고 역량 id (둘 다 draft_spec 이 revise 때 넘긴다)."""
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

    td = state.overlap_check
    checks: dict[str, OverlapCheck] = (
        {c.competency_id: c for c in td.items if c.competency_id in posting} if td else {}
    )
    down = set(downgraded) & set(posting)
    # 선택만 일치: 확인 경로가 있고 action 이 아닌데 선택 경로만 고름 [2026-11-06]
    select_only = {k for k, ck in checks.items() if ck.label != "action" and k in by_comp}
    up = (set(promoted) & select_only) - down

    sub = p.sub if p else None
    levels = {
        k: rel.level(k, sub, posting) for k in by_comp if k not in posting and rel is not None
    }

    def place(comp: str) -> tuple[str, OverlapSource | None]:
        """공고 역량 → ('overlap'|'related'|'posting_only', 출처). select_only 표시는 match 가 붙인다."""
        l0 = comp in by_comp
        ck = checks.get(comp)
        if comp in down:
            return "related", None
        if comp in up:
            return "overlap", "promoted"  # 승인자가 올린 선택만 일치
        if ck is None:
            return ("overlap", None) if l0 else ("posting_only", None)  # 확인 안 함 — 예전 규칙
        if ck.label == "action":
            return "overlap", "both" if l0 else "verify"
        # 확인 related·mention·none — 선택만 일치(select_only)여도 겹침이 아니다
        return ("related", None) if ck.label == "related" else ("posting_only", None)

    def match(comp: str, source: OverlapSource | None = None) -> CompetencyMatch:
        pc = posting.get(comp)
        lv = levels.get(comp)
        ck = checks.get(comp)
        ev = list(ck.claim_ids) if ck else []
        return CompetencyMatch(
            competency_id=comp,
            label=labels.get(comp, comp),
            in_posting=pc is not None,
            primary=pc.primary if pc else None,
            claim_ids=by_comp.get(comp, []),
            relatedness=lv[0] if lv else None,
            relatedness_reason=lv[1] if lv else None,
            check_label=ck.label if ck else None,
            check_claim_ids=ev,
            overlap_source=source,
            # 확인 경로 근거의 칸 역할 → 없으면 선택 경로 근거의 칸 역할 (불변식 6)
            role=best_role(ev, claims) or best_role(by_comp.get(comp, []), claims),
            downgraded=comp in down,
            select_only=comp in select_only,
            promoted=comp in up,
        )

    placed = {k: place(k) for k in posting}

    # 주장 → 역량: 선택 경로 + 확인 경로 action 근거 (승인자가 내린 역량 제외)
    links: dict[str, list[str]] = {cid: list(x.competency_ids) for cid, x in picked.items()}
    checked_only: set[tuple[str, str]] = set()
    for comp, ck in checks.items():
        if ck.label != "action" or comp in down:
            continue
        for cid in ck.claim_ids:
            if cid not in claims:
                continue
            ids = links.setdefault(cid, [])
            if comp not in ids:
                ids.append(comp)
                checked_only.add((cid, comp))

    def name(cid: str, comp: str) -> str:
        return labels.get(comp, comp) + (CHECKED if (cid, comp) in checked_only else "")

    mismatch = {
        cid
        for link in state.links
        if link.relation == "mismatch"
        for cid in (link.resume_claim_id, link.cover_letter_claim_id)
    }

    targets: list[VerifyTarget] = []
    for cid, comps in sorted(links.items()):
        if not comps:
            continue
        c = claims[cid]
        score = 0.0
        reasons: list[str] = []
        prim = [k for k in comps if k in posting and posting[k].primary]
        supp = [k for k in comps if k in posting and not posting[k].primary]
        if prim:
            score += W_POSTING[True]
            reasons.append("공고 주 역량: " + ", ".join(name(cid, k) for k in prim))
        elif supp:
            score += W_POSTING[False]
            reasons.append("공고 보조 역량: " + ", ".join(name(cid, k) for k in supp))
        elif rel is None:
            score += W_UNKNOWN
            reasons.append("공고 밖 역량 (직무 연계 확인 불가)")
        else:
            outside = [(levels[k], k) for k in comps if k in levels]
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
        unsure = [k for k in comps if k in select_only and k not in up]
        if unsure:
            reasons.append(f"{SELECT_ONLY}: " + ", ".join(labels.get(k, k) for k in unsure))
        raised = [k for k in comps if k in up]
        if raised:
            reasons.append(f"{PROMOTED_KO}: " + ", ".join(labels.get(k, k) for k in raised))
        targets.append(
            VerifyTarget(
                claim_id=cid,
                competency_ids=list(comps),
                role=c.role,
                score=score,
                reasons=reasons,
            )
        )
    targets.sort(key=lambda t: (-t.score, t.claim_id))
    checked_claims = {cid for cid, _ in checked_only}

    return Comparison(
        overlap=[match(k, src) for k, (where, src) in placed.items() if where == "overlap"],
        related=[match(k) for k, (where, _) in placed.items() if where == "related"],
        posting_only=[match(k) for k, (where, _) in placed.items() if where == "posting_only"],
        applicant_only=[match(k) for k in sorted(by_comp) if k not in posting],
        uncovered=sorted(
            cid for cid, x in picked.items() if x.uncovered and cid not in checked_claims
        ),
        claims_to_verify=targets[:MAX_VERIFY],
        check_status=td.status if td else None,
        downgraded=sorted(down),
        promoted=sorted(up),
    )


def make_compare(
    rel: Relatedness | None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def compare(state: RunState) -> dict[str, Any]:
        return {"comparison": compare_claims(state, rel)}

    return compare
