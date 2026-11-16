"""문제 분석 판 명세의 참조 검사 (docs/design/analysis-stage.md §4.2·§6·§7.1) — 순수 함수.

스키마(schemas/domain.py)는 형식만 본다 — 재요청 뒤 고쳐 쓴 초안도 저장·승인 화면에 올라가야 하므로. id 사이 참조는 여기서 본다:
- 사실(`facts`)의 `covers` 는 있는 공식 요구(r#)·공개 수용 기준(p#)만 가리킨다.
- 공식 요구·공개 수용 기준마다 그것을 덮는 사실이 하나 이상 — 물어서 알 수 없는 요구로 채점하면 안 된다 (§4.2).
- `request_mentions`·`req_ids` 는 있는 r# 만. 공개 수용 기준마다 `req_ids` 하나 이상, 공식 요구마다 그것을 가리키는 p# 하나 이상 (§6·§5.1).
- 요청 글과 사실은 함께 있거나 함께 없다 — 한쪽만 있으면 판이 섞인 것.
문제 분석 판이 아닌 명세(요청 글·사실이 모두 없음)는 검사할 것이 없다. 생성기 연결은 문제 분석 생성기(§12 4)에서.
"""

from __future__ import annotations

from task_spec.schemas import TaskItem


def check_analysis(task: TaskItem) -> list[str]:
    """문제 분석 판의 참조 문제 목록 (비면 통과). 예전 판(요청 글·사실 없음)은 늘 통과."""
    has_request, has_facts = task.request is not None, bool(task.facts)
    if not has_request and not has_facts:
        return []
    if has_request != has_facts:
        return [
            "요청 글과 사실(facts)은 함께 있어야 한다 — "
            + ("사실이 없음" if has_request else "요청 글이 없음")
        ]
    reqs = [r.req_id for r in task.requirements]
    acs = [a.ac_id for a in task.acceptance_public]
    known = set(reqs) | set(acs)
    problems: list[str] = []
    ids = [f.fact_id for f in task.facts]
    if dup := sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"사실 id 가 겹침: {', '.join(dup)}")
    for f in task.facts:
        if bad := [c for c in f.covers if c not in known]:
            problems.append(f"{f.fact_id} 의 covers 가 없는 요구·기준을 가리킴: {', '.join(bad)}")
    covered = {c for f in task.facts for c in f.covers}
    if loose := [i for i in [*reqs, *acs] if i not in covered]:
        problems.append(f"덮는 사실이 없는 요구·기준 (물어서 알 수 없음): {', '.join(loose)}")
    if bad := [r for r in task.request_mentions if r not in reqs]:
        problems.append(f"request_mentions 가 없는 요구를 가리킴: {', '.join(bad)}")
    for a in task.acceptance_public:
        if not a.req_ids:
            problems.append(
                f"{a.ac_id} 에 req_ids 가 없음 — 공개 수용 기준마다 공식 요구 하나 이상"
            )
        elif bad := [r for r in a.req_ids if r not in reqs]:
            problems.append(f"{a.ac_id} 의 req_ids 가 없는 요구를 가리킴: {', '.join(bad)}")
    pointed = {r for a in task.acceptance_public for r in a.req_ids}
    if orphan := [r for r in reqs if r not in pointed]:
        problems.append(
            f"가리키는 공개 수용 기준이 없는 요구 (공개 테스트가 없음): {', '.join(orphan)}"
        )
    return problems
