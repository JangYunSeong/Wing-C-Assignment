"""정답 장치 표지 꼴 거름 (#337 1절) — 공개 재료 표지는 모두 남고, 문장 표지는 빠진다. 가상 데이터만 (문장 예시는 지어 쓴 글)."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_spec.schemas import FeatureSeed, PublicIncident
from task_spec.spec.fix_markers import code_shaped, marker_note, split_markers, usable_markers

PUBLIC = Path(__file__).resolve().parents[2] / "incidents" / "public_v1"


def _public_markers() -> list[tuple[str, str]]:
    out = []
    for f in sorted(PUBLIC.glob("*.json")):
        feature = PublicIncident.model_validate_json(f.read_text("utf-8")).feature
        out += [(f.name, m) for m in (feature.fix_markers if feature is not None else [])]
    return out


def test_every_public_marker_is_code_shaped() -> None:
    """공개 재료 표지는 사람이 코드 정규식으로 적었다 — 하나도 빠지면 안 된다 (2026-11-26 측정 48개)."""
    markers = _public_markers()
    assert len(markers) >= 48
    assert [(f, m) for f, m in markers if not code_shaped(m)] == []


@pytest.mark.parametrize(
    "marker",
    [
        "세션 확인 경로에서 DB 조회와 갱신을 제거",  # 인터뷰 재료가 쓴 꼴 — 장치를 말로 풀어 씀
        "목록 데이터를 화면 그리기 전에 loader 로 확보",
        "캐시 사용",
        "캐시",
        "use AbortController to cancel stale requests",
        "",
    ],
)
def test_sentences_are_not_markers(marker: str) -> None:
    """문장은 코드에 걸리지 않는다 — 두면 ③ 의 '흔한 구현이 정답 장치를 씀' 판정이 조용히 꺼진다."""
    assert not code_shaped(marker)


def test_split_keeps_order_and_reports_what_it_dropped() -> None:
    kept, dropped = split_markers(
        [r"\buseQuery\s*\(", "캐시 사용", "AbortController", "요청 취소 처리"]
    )
    assert kept == [r"\buseQuery\s*\(", "AbortController"]
    assert dropped == ["캐시 사용", "요청 취소 처리"]


def test_the_note_says_the_check_is_off_only_when_nothing_is_left() -> None:
    assert (
        marker_note(3, 0) == "정답 장치 표지 없음 — 무력화 판정 꺼짐 (코드 꼴이 아닌 표지 3개 제외)"
    )
    assert marker_note(1, 2) == "정답 장치 표지 1개 제외 — 코드 꼴이 아님"


def _seed(markers: list[str]) -> FeatureSeed:
    f = next(PUBLIC.glob("sw.backend.retry-duplicate.json"))
    feature = PublicIncident.model_validate_json(f.read_text("utf-8")).feature
    assert feature is not None
    return feature.model_copy(update={"fix_markers": markers})


def test_build_markers_come_without_sentences_and_with_a_note() -> None:
    """이미 저장된 재료는 다시 만들지 않으므로 빌드가 같은 거름을 쓴다 — 뺀 표지가 있을 때만 알린다."""
    assert usable_markers(None) == ([], None)
    assert usable_markers(_seed([r"\bfindByRequestId\b"])) == ([r"\bfindByRequestId\b"], None)
    assert usable_markers(_seed(["요청 번호를 먼저 저장해 중복을 막음", "중복 요청 거절"])) == (
        [],
        "정답 장치 표지 없음 — 무력화 판정 꺼짐 (코드 꼴이 아닌 표지 2개 제외)",
    )
