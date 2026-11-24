"""일별 매장 매출 요약 — 배치 작업 진입점.

    python pipeline/run.py --date 2026-10-11 --raw data/raw --out out

참고 구현 naive-nonatomic (숨은 요구 t2 만 놓침):
- 실행 D 마다 D-3 ~ D 날짜를 다시 계산하고 payment_id 로 한 번씩 센다 (accepted 와 같음).
- 쓰기만 다르다: 파티션의 기존 파일을 지우고 part-0.parquet 를 그 자리에 바로 쓴다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import raw, reader  # noqa: E402

LOOKBACK_DAYS = 3


def parse(line: str, where: str) -> dict:
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{where}: 읽을 수 없는 줄 ({e})") from e


def load(raw_dir: Path, lo: date, day: date) -> dict[str, tuple[str, str, date, int]]:
    """폴더 lo ~ day 의 결제 → {payment_id: (received_at, store_id, 결제일, 금액)} (가장 이른 수신 하나)."""
    events: dict[str, tuple[str, str, date, int]] = {}
    for d in raw.folder_dates(raw_dir):
        if not lo <= d <= day:
            continue
        for f in raw.files_for(raw_dir, d):
            with f.open(encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    e = parse(line, f"{f}:{n}")
                    rec = (e["received_at"], e["store_id"], date.fromisoformat(e["paid_at"][:10]), int(e["amount"]))
                    old = events.get(e["payment_id"])
                    if old is None or rec[0] < old[0]:
                        events[e["payment_id"]] = rec
    return events


def write_partition(out_dir: Path, day: date, totals: dict[str, list[int]]) -> None:
    part = reader.partition_dir(out_dir, day)
    part.mkdir(parents=True, exist_ok=True)
    for old in part.glob("*.parquet"):
        old.unlink()
    stores = sorted(totals)
    table = pa.table(
        {
            "store_id": pa.array(stores, pa.string()),
            "orders": pa.array([totals[s][0] for s in stores], pa.int64()),
            "sales_amount": pa.array([totals[s][1] for s in stores], pa.int64()),
        }
    )
    pq.write_table(table, part / "part-0.parquet")


def run(day: date, raw_dir: Path, out_dir: Path) -> None:
    lo = day - timedelta(days=LOOKBACK_DAYS)
    events = load(raw_dir, lo, day)
    by_date: dict[date, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for _, store, paid, amount in events.values():
        if lo <= paid <= day:
            t = by_date[paid][store]
            t[0] += 1
            t[1] += amount
    for i in range(LOOKBACK_DAYS + 1):
        x = lo + timedelta(days=i)
        if by_date.get(x):
            write_partition(out_dir, x, by_date[x])
    print(f"{day}: {lo}~{day} 다시 계산, 결제 {len(events)}건")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="일별 매장 매출 요약")
    ap.add_argument("--date", required=True, type=date.fromisoformat, help="실행 날짜 YYYY-MM-DD")
    ap.add_argument("--raw", required=True, type=Path, help="원시 폴더 (<raw>/YYYY-MM-DD/*.jsonl)")
    ap.add_argument("--out", required=True, type=Path, help="요약 폴더 (<out>/daily_sales/date=YYYY-MM-DD/)")
    args = ap.parse_args(argv)
    run(args.date, args.raw, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
