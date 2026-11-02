"""② 역할 규칙 판정 (CLAUDE.md 불변식 6 — LLM 이 정하지 않는다). [확정: 2026-11-02]

주장의 원문 인용이 가명화본의 어느 ⟦칸⟧ 아래에 있는지 찾고(field), 칸 → 역할 규칙을 적용한다.

| 칸                     | 역할          |
| 과정 · 담당업무         | 참여 participate |
| 기여한 점               | 주도 lead      |
| 기여한 점·담당업무 + 설계 표현 | 설계 design (덮어씀) |
| 문제 정의 · 성과 · 사용 기술 · 칸 밖 | 판정 안 함 (None) |

판정 근거(role_basis)를 주장마다 남긴다 — 승인자가 "왜 주도인지" 따라갈 수 있게.
"""

from __future__ import annotations

import re
from typing import Any

from task_spec.llm.quotes import SECTION_MARKER, normalize_for_quote
from task_spec.nodes.ingest.structure import _PERIOD, _section_kind
from task_spec.schemas import Claim, RunState
from task_spec.schemas.domain import Role
from task_spec.schemas.ingest import FieldLabel

_MARKER_LINE = re.compile(r"^\s*⟦(?P<label>[^⟧]{1,40})⟧")
_FIELD_ROLE: dict[str, Role] = {
    "과정": "participate",
    "담당업무": "participate",
    "기여한 점": "lead",
}
_DESIGN_FIELDS = frozenset({"기여한 점", "담당업무"})
DESIGN_TERMS = re.compile(r"설계|아키텍처|구조\s?(?:를|을)?\s?(?:수립|정의|잡)")


def field_of_lines(text: str) -> list[str | None]:
    """줄마다 속한 칸. 칸은 다음 칸 표시·빈 줄·절 제목·머리말(기간) 줄에서 끝난다."""
    current: str | None = None
    out: list[str | None] = []
    for line in text.split("\n"):
        stripped = line.strip()
        m = _MARKER_LINE.match(line)
        if m:
            current = m.group("label")
        elif not stripped or _section_kind(stripped) or _PERIOD.search(stripped):
            current = None
        out.append(current)
    return out


def locate_field(text: str, quote: str) -> FieldLabel | None:
    """인용이 시작하는 줄의 칸. 인용이 여러 줄이면 첫 줄 기준."""
    q = normalize_for_quote(quote)
    if not q:
        return None
    lines = text.split("\n")
    fields = field_of_lines(text)
    head = q[: min(len(q), 30)]
    for i, line in enumerate(lines):
        joined = normalize_for_quote(" ".join(lines[i : i + 4]))
        if joined.startswith(head) or (head in normalize_for_quote(line)):
            return fields[i]  # type: ignore[return-value]
    return None


def decide_role(claim: Claim) -> tuple[Role | None, str]:
    if claim.field is None:
        return None, "no_field"
    role = _FIELD_ROLE.get(claim.field)
    basis = f"field:{claim.field}"
    if claim.field in _DESIGN_FIELDS and DESIGN_TERMS.search(
        f"{claim.action} {SECTION_MARKER.sub(' ', claim.quote)}"
    ):
        return "design", basis + "+design_term"
    return role, basis


async def assign_roles(state: RunState) -> dict[str, Any]:
    updated = []
    for c in state.claims:
        role, basis = decide_role(c)
        updated.append(c.model_copy(update={"role": role, "role_basis": basis}))
    return {"claims": updated}
