"""공개 사례 읽기 (incidents/public_v1). 직군마다 1건 — 부서 결정적 사건이 생기면 그쪽이 우선한다."""

from __future__ import annotations

from pathlib import Path

from task_spec.schemas import PublicIncident

NONE = "(없음)"


def load_incidents(root: Path) -> dict[str, PublicIncident]:
    """직군 → 사례. 폴더가 없으면 빈 dict (사례 없이 v1 처럼 생성)."""
    if not root.is_dir():
        return {}
    out: dict[str, PublicIncident] = {}
    for f in sorted(root.glob("*.json")):
        inc = PublicIncident.model_validate_json(f.read_text("utf-8"))
        if inc.sub in out:
            raise ValueError(f"직군 {inc.sub} 사례가 둘 이상입니다: {f.name}")
        out[inc.sub] = inc
    return out


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
    if inc.hypotheses:
        lines.append("기각해야 할 가설 (자료로 하나씩 기각되게): " + " / ".join(inc.hypotheses))
    if inc.task_pattern:
        # 형태별 지시문은 프롬프트(draft_task v2)에 있다 — 여기서는 이름만 (불변식 7)
        lines.append(f"과제 형태: {inc.task_pattern}")
    return "\n".join(lines)
