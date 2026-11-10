"""공개 사례 읽기 (incidents/public_v1). 직군마다 기본 사례 1건(`<직군>.json`) — 부서 결정적 사건이 생기면 그쪽이 우선한다.

기능형 과제용 사례를 더 둘 수 있다 (`<직군>.<이름>.json`, `templates` 에 그 재료를 시험할 수 있는 템플릿) — 기능형 과제는
`feature_incident` 로 직군 템플릿이 시험할 수 있는 사례를 고른다 [2026-11-09 사용자 결정: 2건 허용 + 템플릿별 선택].
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from task_spec.schemas import PublicIncident
from task_spec.starter.templates import TEMPLATE_FOR_SUB

NONE = "(없음)"


class IncidentCatalog(dict[str, PublicIncident]):
    """직군 → 기본 사례 (dict 그대로 쓴다). `alternates` 는 직군 → 기능형 과제용 추가 사례 (feature_incident 가 고른다)."""

    def __init__(self, alternates: Mapping[str, list[PublicIncident]] | None = None) -> None:
        super().__init__()
        self.alternates: dict[str, list[PublicIncident]] = dict(alternates or {})


def load_incidents(root: Path) -> IncidentCatalog:
    """직군 → 기본 사례(`<직군>.json`). 그 밖의 파일은 기능형 과제용 추가 사례 — `templates` 가 비면 거부.
    폴더가 없으면 빈 카탈로그 (사례 없이 v1 처럼 생성)."""
    out = IncidentCatalog()
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*.json")):
        inc = PublicIncident.model_validate_json(f.read_text("utf-8"))
        if f.stem != inc.sub:
            if not inc.templates:
                raise ValueError(
                    f"직군 {inc.sub} 의 추가 사례 {f.name} 에 templates 가 없습니다 — 기본 사례는 {inc.sub}.json"
                )
            out.alternates.setdefault(inc.sub, []).append(inc)
            continue
        if inc.sub in out:
            raise ValueError(f"직군 {inc.sub} 사례가 둘 이상입니다: {f.name}")
        out[inc.sub] = inc
    return out


def feature_incident(
    incidents: Mapping[str, PublicIncident] | None, sub: str, posting_id: str = ""
) -> PublicIncident | None:
    """기능형 과제의 사례 — 직군 템플릿(TEMPLATE_FOR_SUB)이 시험할 수 있는 추가 사례가 있으면 그것, 없으면 기본 사례.
    그런 추가 사례가 여럿이면 공고 id 해시로 고정 배정한다 — 같은 공고는 늘 같은 재료 (2026-11-09 사용자 결정, 명세 source_incident 로
    승인자가 본다). 사례를 더하면 일부 공고의 배정이 바뀐다 (재료가 바뀌면 기능형 판 지문도 바뀌어 새 판)."""
    if not incidents:
        return None
    template = TEMPLATE_FOR_SUB.get(sub)
    # 기본 사례도 templates 에 그 템플릿을 적었으면 후보에 든다 — 2026-11-10 백엔드 추가 사례 3건: 기본 사례(풀 교착)가 빠지면
    # 검증한 재료가 새 공고에 배정되지 않는다 (프론트 기본 사례는 jsdom 으로 시험할 수 없어 templates 가 비어 그대로 빠짐)
    base = incidents.get(sub)
    pool = [
        *getattr(incidents, "alternates", {}).get(sub, []),
        *([base] if base is not None else []),
    ]
    cands = sorted(
        (
            inc
            for inc in pool
            if template is not None and template in inc.templates and inc.feature is not None
        ),
        key=lambda inc: inc.incident_id,
    )
    if not cands:
        return incidents.get(sub)
    if len(cands) == 1 or not posting_id:
        return base if base in cands else cands[0]  # 공고 없이 부르면 기본 사례 (예전과 같음)
    return cands[int(hashlib.sha256(posting_id.encode("utf-8")).hexdigest(), 16) % len(cands)]


def incident_by_id(
    incidents: Mapping[str, PublicIncident] | None, incident_id: str | None
) -> PublicIncident | None:
    """명세에 적힌 사례(TaskItem.source_incident)를 찾는다 — 기본·추가 사례 모두. 이미 만든 명세로 다시 돌릴 때 같은 재료를 쓴다."""
    if not incidents or not incident_id:
        return None
    pool = [
        *incidents.values(),
        *(i for v in getattr(incidents, "alternates", {}).values() for i in v),
    ]
    return next((i for i in pool if i.incident_id == incident_id), None)


def incident_block(inc: PublicIncident | None) -> str:
    """프롬프트에 넣는 사례 블록 — 원리만. 조직명·제목·URL·원문 수치는 넣지 않는다."""
    if inc is None:
        return NONE
    lines = [
        f"원리(유지할 심층 구조): {inc.deep_structure}",
        f"겉 증상: {inc.symptom}",
        f"실제 원인: {inc.root_cause}",
        f"흔한 처방(함정 후보): {inc.naive_fix}",
        f"흔한 처방이 틀리는 이유: {inc.why_naive_fails}",
        f"결정적 단서: {inc.key_insight}",
        "바꿀 수 있는 표면: " + ", ".join(inc.surface),
    ]
    if inc.accepted_fixes:
        # 2026-11-03: 원 사례의 실제 조치(근거 있는 풀 크기 산정)를 함정으로 적어 맞는 답이 감점될 뻔했다
        lines.append(
            "정답으로 인정할 조치 (함정·오답으로 쓰지 않음): " + " / ".join(inc.accepted_fixes)
        )
    if inc.hypotheses:
        lines.append("기각해야 할 가설 (자료로 하나씩 기각되게): " + " / ".join(inc.hypotheses))
    if inc.task_pattern:
        # 형태별 지시문은 프롬프트(draft_task v2)에 있다 — 여기서는 이름만 (불변식 7)
        lines.append(f"과제 형태: {inc.task_pattern}")
    return "\n".join(lines)


def incident_ref(inc: PublicIncident) -> str:
    """공통 과제 저장 키에 붙이는 사례 지문 — 사례 내용이 바뀌면 새 판을 만든다 (이전 초안을 다시 쓰지 않게).

    기능형 재료(feature)는 빼고 계산한다 — 재료를 붙이거나 고쳐도 이슈형 공통 과제 판이 바뀌지 않게 (예전 지문과 같음)."""
    body = inc.model_dump_json(exclude={"feature"})
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    return f"{inc.incident_id}#{digest}"


def feature_ref(inc: PublicIncident) -> str:
    """기능형 공통 과제 저장 키의 재료 지문 — 사례 원리 + 기능형 재료가 바뀌면 새 판."""
    digest = hashlib.sha256(inc.model_dump_json().encode("utf-8")).hexdigest()[:8]
    return f"{inc.incident_id}+feature#{digest}"
