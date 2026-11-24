"""일별 매장 매출 요약 — 배치 작업 진입점.

    python pipeline/run.py --date 2026-10-11 --raw data/raw --out out

참고 구현 naive-no-dedupe (숨은 요구 t3 만 놓침): accepted-sql 과 같고 payment_id 로 한 번씩 세는 단계만 없다
(읽은 줄을 모두 결제로 센다).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import raw, reader  # noqa: E402

COLUMNS = "{" + ", ".join(f"'{k}': '{v}'" for k, v in raw.COLUMNS.items()) + "}"


def _list(paths: list[Path]) -> str:
    return "[" + ", ".join("'" + p.as_posix().replace("'", "''") + "'" for p in paths) + "]"


def _read(paths: list[Path]) -> str:
    return f"read_json({_list(paths)}, format = 'newline_delimited', columns = {COLUMNS}, filename = true)"


def run(day: date, raw_dir: Path, out_dir: Path) -> None:
    today = raw.files_for(raw_dir, day)
    if not today:
        raise SystemExit(f"{day}: 원시 파일이 없습니다")
    con = duckdb.connect()
    touched = [r[0] for r in con.sql(f"SELECT DISTINCT CAST(paid_at AS DATE) FROM {_read(today)} ORDER BY 1").fetchall()]
    lo = touched[0]
    files = [f for d in raw.folder_dates(raw_dir) if lo <= d <= day for f in raw.files_for(raw_dir, d)]
    con.execute(
        f"""
        CREATE TABLE payments AS
        SELECT payment_id, store_id, CAST(paid_at AS DATE) AS paid_date, amount
        FROM {_read(files)}
        WHERE CAST(paid_at AS DATE) >= DATE '{lo.isoformat()}'
        """
    )
    staging = out_dir / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    for x in touched:
        part = reader.partition_dir(out_dir, x)
        part.mkdir(parents=True, exist_ok=True)
        tmp = staging / f"{uuid.uuid4().hex}.parquet"
        con.execute(
            f"""
            COPY (
                SELECT store_id, COUNT(*)::BIGINT AS orders, SUM(amount)::BIGINT AS sales_amount
                FROM payments WHERE paid_date = DATE '{x.isoformat()}'
                GROUP BY store_id ORDER BY store_id
            ) TO '{tmp.as_posix()}' (FORMAT PARQUET)
            """
        )
        os.replace(tmp, part / "part-0.parquet")
    for left in staging.glob("*.parquet"):  # 지난 크래시가 남긴 것
        left.unlink()
    print(f"{day}: 다시 계산 {', '.join(x.isoformat() for x in touched)}")


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
