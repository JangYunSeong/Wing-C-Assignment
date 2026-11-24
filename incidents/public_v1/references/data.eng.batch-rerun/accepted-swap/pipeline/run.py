"""일별 매장 매출 요약 — 배치 작업 진입점.

    python pipeline/run.py --date 2026-10-11 --raw data/raw --out out

참고 구현 accepted-swap (허용 설계 3 — 매번 전부 다시 계산 + 파티션 폴더 바꿔 끼우기):
- D 까지 들어온 폴더 전부를 읽어 payment_id 로 한 번씩 세고 모든 날짜를 다시 계산한다 (규모가 작아 몇 초).
- daily_sales/_staging-<난수>/ 에 DuckDB PARTITION_BY 로 전부 쓴 뒤, 날짜 폴더마다 기존 폴더를 _old-<난수>/ 로 옮기고
  새 폴더를 그 자리로 옮긴다. 다 쓰기 전에 죽으면 _staging 만 남는다 (읽는 쪽은 _ 로 시작하는 폴더를 보지 않는다).
- 한계: 날짜 폴더 하나를 옮기는 두 rename 사이에 죽으면 그 날짜가 잠깐 빈다 (파일 하나 바꾸기보다 약함).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import raw, reader  # noqa: E402

COLUMNS = "{" + ", ".join(f"'{k}': '{v}'" for k, v in raw.COLUMNS.items()) + "}"


def run(day: date, raw_dir: Path, out_dir: Path) -> None:
    if not raw.files_for(raw_dir, day):
        raise SystemExit(f"{day}: 원시 파일이 없습니다")
    files = [f for d in raw.folder_dates(raw_dir) if d <= day for f in raw.files_for(raw_dir, d)]
    file_list = "[" + ", ".join("'" + f.as_posix().replace("'", "''") + "'" for f in files) + "]"
    base = out_dir / reader.SUMMARY_DIR
    base.mkdir(parents=True, exist_ok=True)
    for left in [*base.glob("_staging-*"), *base.glob("_old-*")]:  # 지난 크래시가 남긴 것
        shutil.rmtree(left)
    run_id = uuid.uuid4().hex
    staging, old = base / f"_staging-{run_id}", base / f"_old-{run_id}"
    staging.mkdir()
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE TABLE payments AS
        SELECT payment_id, store_id, CAST(paid_at AS DATE) AS paid_date, amount
        FROM read_json({file_list}, format = 'newline_delimited', columns = {COLUMNS}, filename = true)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY payment_id ORDER BY received_at, filename) = 1
        """
    )
    con.execute(
        f"""
        COPY (
            SELECT paid_date AS date, store_id, COUNT(*)::BIGINT AS orders, SUM(amount)::BIGINT AS sales_amount
            FROM payments GROUP BY 1, 2 ORDER BY 1, 2
        ) TO '{staging.as_posix()}' (FORMAT PARQUET, PARTITION_BY (date))
        """
    )
    old.mkdir()
    swapped = 0
    for staged in sorted(staging.iterdir()):
        if not reader.PARTITION_RE.match(staged.name):
            continue
        final = base / staged.name
        if final.exists():
            os.rename(final, old / staged.name)
        os.rename(staged, final)
        swapped += 1
    shutil.rmtree(staging)
    shutil.rmtree(old)
    print(f"{day}: 날짜 {swapped}개 다시 계산")


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
