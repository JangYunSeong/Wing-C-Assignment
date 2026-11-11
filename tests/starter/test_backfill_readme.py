"""README 채우기 — 필드 순서만 다른지 판정. 가상 데이터만."""

from __future__ import annotations

from eval.starter.backfill_readme import reorder_only

IMAGE = "- `Slot` 3행 — id 1~2: `name`=ev-1, `left`=5 · id 3~3: `name`=ev-2, `left`=0 — 그 밖: `slotId`=그 행의 id\n"


def test_reorder_only() -> None:
    swapped = IMAGE.replace("`name`=ev-1, `left`=5", "`left`=5, `name`=ev-1").replace(
        "`name`=ev-2, `left`=0", "`left`=0, `name`=ev-2"
    )
    assert reorder_only(IMAGE, swapped)
    assert not reorder_only(IMAGE, IMAGE)  # 같으면 해시 비교로 끝난다
    assert not reorder_only(IMAGE, swapped.replace("`left`=0", "`left`=1"))  # 값이 다름
    assert not reorder_only(IMAGE, swapped.replace("id 3~3", "id 3~4"))  # 나열 밖 글자
    # 나열 사이로 값이 옮겨 가면 순서 차이가 아니다
    moved = IMAGE.replace("`left`=5", "`left`=TMP").replace("`left`=0", "`left`=5")
    assert not reorder_only(IMAGE, moved.replace("`left`=TMP", "`left`=0"))
