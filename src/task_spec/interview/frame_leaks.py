"""함정 틀 검사 3 공개 중립성 (#318, docs/design/trap-frame.md §5.3) — 명세 뒤, 틀이 막은 것이 지원자가 볼 글에 나왔는가 (코드, 표시만).

1단계에서 지원자가 받는 글 — 제목·상황·공개 요구·공개 수용 기준·자료(방해 자료의 기존 코드 포함)·추가 요구 답·사실(f#)의 답 — 에서
틀의 금지 낱말과 금지 저장 엔티티를 찾는다. 2026-11-24 세션 유지 과제(ct-f6087a7ba679)에서 방해 자료의 기존 코드(findSessionById)가 흔한
구현의 저장 방식을 보였다. 명세 검사(spec/feature.check_feature)도 제목·상황·공개 요구·수용 기준의 금지 낱말을 보지만 생성 재요청 루프에서만
돈다 — ⑧ 에서 담당자가 고친 글은 이 표시만 다시 계산하고, 금지 저장 꼴(테이블·조회 SQL)은 명세 검사가 보지 않는다 (설계 검사 B-F6).

- 2단계 자료는 보지 않는다 — 흔한 구현을 배포한 뒤의 증상을 진단하는 단계라 그 저장 방식이 단서로 보여야 한다
  (2026-11-26 draft_feature_task v13 평가에서 v12·v13 모두 2단계 자료에 세션 조회 로그를 썼다).
- 낱말은 공개된 업무 요구에 나온 말과 운영 조건 낱말(trigger_terms — 1단계 자료에서 보여야 하는 단서)에 걸리는 말을 뺀다.
- 저장 엔티티는 낱말이 아니라 저장 꼴로 찾는다 — 테이블 정의·조회 SQL·'<이름> 테이블'·엔티티 클래스·Repository·조회 메서드.
  sessionId 같은 공개 계약 칸은 걸리지 않게 (검사 4 와 같은 이유).
- 과제 글에서 다시 계산하는 표시다 — 담당자가 글을 고치면 interview/task.with_content_flags 가 다시 계산한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from task_spec.schemas import FeatureSeed, TaskItem, TrapFrame
from task_spec.spec.feature import find_terms

FLAG_FRAME_LEAK = "함정 틀: 공개 쪽에 틀이 막은 것 — "
MAX_PLACES = 5  # 표시할 곳 수 (나머지는 '외 N곳')
_ROLE = {"key": "핵심", "support": "보조", "distractor": "방해"}


def storage_pattern(entity: str) -> re.Pattern[str] | None:
    """금지 엔티티를 저장 대상으로 그린 꼴 — 이름이 영문 식별자(틀 v3 는 PascalCase)일 때만. 단·복수·snake_case 테이블 이름을 같게 본다."""
    name = re.sub(r"[^0-9A-Za-z]", "", entity)
    if len(name) < 3:
        return None
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
    tables = "|".join(sorted({re.escape(name.lower()), re.escape(snake)}, key=len, reverse=True))
    table = rf"\w*(?:{tables})s?"
    ident = re.escape(name)
    return re.compile(
        rf"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"']?{table}\b"
        rf"|\b(?:from|join|update|into)\s+[`\"']?{table}\b"
        rf"|\b{table}\s*(?:테이블|table\b)"
        rf"|@Table\s*\(\s*name\s*=\s*[\"']{table}"
        rf"|\bclass\s+{ident}\b"
        rf"|\b{ident}Repository\b"
        rf"|\b(?:find|save|delete){ident}\w*\s*\(",
        re.IGNORECASE,
    )


def _terms(frame: TrapFrame, seed: FeatureSeed | None) -> list[str]:
    terms = [t for t in frame.forbidden_terms if t.strip()]
    if seed is None:
        return terms
    public = set(find_terms(terms, seed.business_need))
    trigger = " / ".join(seed.trigger_terms)
    return [t for t in terms if t not in public and not find_terms([t], trigger)]


def _places(task: TaskItem) -> Iterator[tuple[str, str]]:
    """(자리, 글) — 1단계에서 지원자가 받는 글 (자료는 제목도 저장 꼴로 본다)."""
    if task.title:
        yield "제목", task.title
    if task.scenario:
        yield "상황", task.scenario
    for r in task.requirements:
        yield f"공개 요구 {r.req_id}", r.text
    for ac in task.acceptance_public:
        yield f"공개 수용 기준 {ac.ac_id}", ac.text
    for a in task.artifacts:
        yield (
            f"자료 {a.artifact_id}({_ROLE.get(a.role, a.role)}) '{a.title}'",
            f"{a.title}\n{a.content}",
        )
    for c in task.clarifications:
        yield (
            f"추가 요구 {c.clarification_id} 답",
            "\n".join(x for x in (c.answer, c.answer_hint) if x),
        )
    for f in task.facts:
        yield f"사실 {f.fact_id} 답", "\n".join(x for x in (f.answer, f.answer_hint) if x)


def frame_leak_flags(
    task: TaskItem, frame: TrapFrame | None, seed: FeatureSeed | None
) -> list[str]:
    """검토 표시 — 자리마다 걸린 금지 낱말·저장 꼴. 틀이 없으면 없음. seed 는 등록 재료의 기능형 칸(업무 요구·운영 조건 낱말)."""
    if frame is None:
        return []
    terms = _terms(frame, seed)
    patterns = [(s.entity, p) for s in frame.forbidden_storage if (p := storage_pattern(s.entity))]
    found: list[str] = []
    for where, text in _places(task):
        hits = find_terms(terms, text)
        for entity, pattern in patterns:
            if m := pattern.search(text):
                shown = re.sub(r"\s+", " ", m.group(0))[:40]
                hits.append(f"{entity} 저장 '{shown}'")
        if hits:
            found.append(f"{FLAG_FRAME_LEAK}{where}: {', '.join(dict.fromkeys(hits))}")
    if len(found) > MAX_PLACES:
        found = [*found[:MAX_PLACES], f"{FLAG_FRAME_LEAK}외 {len(found) - MAX_PLACES}곳"]
    return found
