"""일별 매장 매출 요약 — 배치 작업 진입점.

    python pipeline/run.py --date 2026-10-11 --raw data/raw --out out

참고 구현 naive-by-paid-date: 그날 폴더의 결제를 결제 날짜·매장별로 묶어, 결제 날짜마다 그 날짜 파티션에 바로 쓴다 (덮어씀).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import raw, reader  # noqa: E402


def run(day: date, raw_dir: Path, out_dir: Path) -> None:
    files = raw.files_for(raw_dir, day)
    if not files:
        raise SystemExit(f"{day}: 원시 파일이 없습니다")
    file_list = "[" + ", ".join(f"'{f.as_posix()}'" for f in files) + "]"
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE TABLE summary AS
        SELECT CAST(paid_at AS DATE) AS paid_date, store_id,
               COUNT(*) AS orders, SUM(amount) AS sales_amount
        FROM read_json({file_list}, format = 'newline_delimited')
        GROUP BY ALL
        """
    )
    days = [r[0] for r in con.execute("SELECT DISTINCT paid_date FROM summary ORDER BY 1").fetchall()]
    for paid in days:
        part = reader.partition_dir(out_dir, paid)
        part.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"""
            COPY (
                SELECT store_id, orders, sales_amount FROM summary
                WHERE paid_date = DATE '{paid.isoformat()}'
                ORDER BY store_id
            ) TO '{(part / "part-0.parquet").as_posix()}' (FORMAT PARQUET)
            """
        )
        print(f"{day}: {part} 저장")


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
