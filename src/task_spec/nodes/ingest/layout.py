"""PDF 단어 좌표 → 읽기 순서의 줄 목록.

- 2단 레이아웃: 페이지 가운데(30~70%)에 단어가 거의 지나가지 않는 세로 빈 띠가 있고, 양쪽 모두 글자가
  충분하면 왼쪽 단 → 오른쪽 단 순서로 읽는다. 기간만 있는 좁은 열(왼쪽 날짜 열 등)은 단으로 보지 않는다
  — 같은 줄의 내용과 붙어 있어야 머리말의 기간이 된다.
- 줄 조립: top 이 가까운 단어를 한 줄로. 단어 사이가 넓으면(오른쪽 정렬 날짜 등) 공백 두 칸으로 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    size: float


MIN_SIDE_SHARE = 0.25  # 2단으로 보려면 각 단이 전체 글자의 25% 이상
GAP_MIN_WIDTH = 12.0  # pt
GAP_MAX_COVER = 0.03  # 빈 띠를 지나가는 단어 비율 상한


def _split_columns(words: list[Word], page_width: float) -> list[list[Word]]:
    if len(words) < 20:
        return [words]
    lo, hi = int(page_width * 0.3), int(page_width * 0.7)
    cover = [0] * (hi - lo + 1)
    for w in words:
        for x in range(max(lo, int(w.x0)), min(hi, int(w.x1)) + 1):
            cover[x - lo] += 1
    limit = max(1, int(len(words) * GAP_MAX_COVER))
    best, run_start = (0, -1), -1
    for i, c in enumerate(cover + [limit + 1]):
        if c <= limit:
            run_start = i if run_start < 0 else run_start
        elif run_start >= 0:
            if i - run_start > best[0]:
                best = (i - run_start, run_start)
            run_start = -1
    width, start = best
    if width < GAP_MIN_WIDTH:
        return [words]
    cut = lo + start + width / 2
    left = [w for w in words if (w.x0 + w.x1) / 2 < cut]
    right = [w for w in words if (w.x0 + w.x1) / 2 >= cut]
    total = sum(len(w.text) for w in words)
    if (
        min(sum(len(w.text) for w in left), sum(len(w.text) for w in right))
        < total * MIN_SIDE_SHARE
    ):
        return [words]
    return [left, right]


def _lines(words: list[Word]) -> list[str]:
    out: list[str] = []
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (round(w.top, 1), w.x0)):
        if rows and abs(rows[-1][0].top - w.top) <= max(2.0, 0.5 * w.size):
            rows[-1].append(w)
        else:
            rows.append([w])
    for row in rows:
        row.sort(key=lambda w: w.x0)
        parts = [row[0].text]
        for prev, cur in zip(row, row[1:], strict=False):
            gap = cur.x0 - prev.x1
            parts.append("  " if gap > 2 * cur.size else " ")
            parts.append(cur.text)
        out.append("".join(parts))
    return out


def words_to_lines(words: list[Word], page_width: float) -> tuple[list[str], int]:
    """줄 목록과 감지한 단 수."""
    cols = _split_columns(words, page_width)
    lines: list[str] = []
    for col in cols:
        lines += _lines(col)
    return lines, len(cols)
