"""정답 장치 표지(FeatureSeed.fix_markers) 꼴 거름 — 코드에 나올 낱말·정규식만 남긴다 (#337 1절).

표지는 허용 설계의 장치가 코드에 드러나는 꼴이다. 흔한 구현이 숨은 케이스를 통과했는데 그 코드에 표지가 보이면 ③ 은 숨은 테스트 대신
흔한 구현을 그 장치를 짚어 다시 만들고, 다시 만든 뒤에도 그렇면 재료 판정(무력화) — 조건이 맞으면 숨은 케이스는 채점에 남기고 '함정 아님'
표시로 review, 아니면 멈춘다 (#341, starter/repair.marker_hits · nodes/starter/build._accept_material).

인터뷰 재료 생성(interview_feature_seed v1)은 이 칸을 설명하지 않아 표지를 문장으로 썼다. 문장은 코드에 걸리지 않아 그 판정이 조용히
꺼졌고, 재료 검증은 정규식 컴파일만 봐서 통과했다 (2026-11-26 운영 실패 분류 — 회사 재료 2건 모두). 막지 않는다 — 출력 모델 검증으로
막으면 형식 재시도 한 번 뒤 재료 생성 전체가 실패한다(llm/client SchemaViolation). 꼴이 아닌 표지는 빼고 승인 화면에 알린다.
이미 저장된 재료는 다시 만들지 않으므로 빌드도 같은 거름을 쓴다.

규칙(2026-11-26 측정): 공개 재료 표지 48개는 모두 통과하고, 운영 문장 표지 6개·'캐시 사용'·영어 문장은 모두 걸러진다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from task_spec.schemas import FeatureSeed

_HANGUL = re.compile(r"[가-힣]")
_CODE_WORD = re.compile(r"[A-Za-z_@$][A-Za-z0-9_]{2,}")  # 3자 이상 코드 낱말 (식별자·애너테이션)
MAX_WORDS = 3  # 띄어 쓴 낱말 수 상한 — 공개 표지의 최대는 'JOIN FETCH|join fetch' 의 셋


def code_shaped(marker: str) -> bool:
    """코드에 걸릴 수 있는 꼴인가 — 정규식으로 컴파일되고, 띄어 쓴 낱말이 셋 이하, 한글이 든 낱말이 하나 이하, 3자 이상 코드 낱말이 있다.
    한글 낱말 하나는 허용한다 — 공개 표지에 화면 글 값을 고르는 묶음('(?:pending|sent|대기|보냄)')이 있다."""
    try:
        re.compile(marker)
    except re.error:
        return False
    words = marker.split()
    if not words or len(words) > MAX_WORDS:
        return False
    if sum(1 for w in words if _HANGUL.search(w)) > 1:
        return False
    return _CODE_WORD.search(marker) is not None


def split_markers(markers: Sequence[str]) -> tuple[list[str], list[str]]:
    """(쓸 표지, 뺀 표지) — 순서는 그대로."""
    kept = [m for m in markers if code_shaped(m)]
    return kept, [m for m in markers if m not in kept]


def marker_note(dropped: int, kept: int) -> str:
    """뺀 표지 알림 (표지 글은 싣지 않는다 — 허용 설계의 장치를 풀어 쓴 재료 글이다)."""
    if kept:
        return f"정답 장치 표지 {dropped}개 제외 — 코드 꼴이 아님"
    return f"정답 장치 표지 없음 — 무력화 판정 꺼짐 (코드 꼴이 아닌 표지 {dropped}개 제외)"


def usable_markers(seed: FeatureSeed | None) -> tuple[list[str], str | None]:
    """빌드가 쓸 표지와 승인 화면 알림 — 뺀 표지가 없으면 알림 없음."""
    if seed is None:
        return [], None
    kept, dropped = split_markers(seed.fix_markers)
    return kept, (marker_note(len(dropped), len(kept)) if dropped else None)
