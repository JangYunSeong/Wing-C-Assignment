"""요약을 읽는 쪽 — 매장 운영 대시보드와 재무팀 대사 도구가 요약을 읽는 방식 그대로다. 바꾸지 않는다.

- `<out>/daily_sales/date=YYYY-MM-DD/` 폴더 하나가 날짜 하나다. 이름이 정확히 이 형식인 폴더만 읽는다.
- 그 안의 `*.parquet` 파일을 모두 읽어 매장별로 더한다. 이름이 `_`·`.` 로 시작하는 파일은 읽지 않는다 (예: `_SUCCESS`).
- 컬럼 `store_id`(문자열)·`orders`·`sales_amount`(정수 값)만 읽는다. 값이 정수면 실수·decimal 형식이어도 된다.
- 건수·금액이 모두 0 인 매장은 없는 것으로 본다. 매장이 하나도 없는 날짜는 없는 날짜다.
- 파일을 읽지 못하면 `SummaryReadError` — 대시보드는 그 날짜를 오류로 표시한다.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SUMMARY_DIR = "daily_sales"
PARTITION_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")
COLUMNS = ("store_id", "orders", "sales_amount")

Totals = dict[str, tuple[int, int]]  # store_id → (orders, sales_amount)


class SummaryReadError(Exception):
    """요약 파일을 계약대로 읽을 수 없다."""


def partition_dir(out_dir: str | Path, day: date) -> Path:
    return Path(out_dir) / SUMMARY_DIR / f"date={day.isoformat()}"


def partition_dirs(out_dir: str | Path) -> dict[date, Path]:
    base = Path(out_dir) / SUMMARY_DIR
    if not base.is_dir():
        return {}
    out = {}
    for p in sorted(base.iterdir()):
        m = PARTITION_RE.match(p.name)
        if m and p.is_dir():
            out[date.fromisoformat(m.group(1))] = p
    return out


def data_files(part_dir: Path) -> list[Path]:
    return sorted(
        f
        for f in part_dir.iterdir()
        if f.is_file() and f.suffix == ".parquet" and not f.name.startswith(("_", "."))
    )


def _ints(col: pa.ChunkedArray, name: str, file: Path) -> list[int]:
    if col.null_count:
        raise SummaryReadError(f"{file.name}: {name} 에 빈 값")
    t = col.type
    vals = col.to_pylist()
    if pa.types.is_integer(t):
        return [int(v) for v in vals]
    if pa.types.is_floating(t) or pa.types.is_decimal(t):
        out = []
        for v in vals:
            if v != int(v):
                raise SummaryReadError(f"{file.name}: {name} 값 {v} 이 정수가 아니다")
            out.append(int(Decimal(v) if pa.types.is_decimal(t) else v))
        return out
    raise SummaryReadError(f"{file.name}: {name} 형식 {t}")


def read_partition(part_dir: Path) -> Totals:
    totals: dict[str, list[int]] = {}
    for f in data_files(part_dir):
        try:
            table = pq.ParquetFile(f).read(columns=list(COLUMNS))
        except Exception as e:  # 깨진 파일·컬럼 없음
            raise SummaryReadError(f"{part_dir.name}/{f.name}: {type(e).__name__}") from e
        missing = [c for c in COLUMNS if c not in table.column_names]
        if missing:
            raise SummaryReadError(f"{part_dir.name}/{f.name}: 컬럼 없음 {missing}")
        sid = table.column("store_id")
        if pa.types.is_dictionary(sid.type):
            sid = sid.cast(pa.string())
        if not (pa.types.is_string(sid.type) or pa.types.is_large_string(sid.type)) or sid.null_count:
            raise SummaryReadError(f"{f.name}: store_id 형식 {sid.type}")
        orders = _ints(table.column("orders"), "orders", f)
        amounts = _ints(table.column("sales_amount"), "sales_amount", f)
        for s, o, a in zip(sid.to_pylist(), orders, amounts):
            t = totals.setdefault(s, [0, 0])
            t[0] += o
            t[1] += a
    return {s: (o, a) for s, (o, a) in sorted(totals.items()) if (o, a) != (0, 0)}


def read_daily_sales(out_dir: str | Path) -> dict[date, Totals]:
    """모든 날짜의 요약 {날짜: {store_id: (orders, sales_amount)}}. 매장이 없는 날짜는 빠진다."""
    out = {}
    for day, p in partition_dirs(out_dir).items():
        totals = read_partition(p)
        if totals:
            out[day] = totals
    return out
