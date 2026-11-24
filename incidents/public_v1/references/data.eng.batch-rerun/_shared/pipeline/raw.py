"""원시 데이터 읽기 도우미.

원시 폴더 구조: `<raw>/YYYY-MM-DD/*.jsonl` — 수집 서버가 그날 받은 파일. 한 줄에 결제 이벤트 하나(JSON).
필드는 README 의 '원시 데이터' 표를 본다.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa

FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 필드 → DuckDB 형식
COLUMNS: dict[str, str] = {
    "payment_id": "VARCHAR",
    "store_id": "VARCHAR",
    "terminal_id": "VARCHAR",
    "paid_at": "TIMESTAMP",
    "amount": "BIGINT",
    "method": "VARCHAR",
    "received_at": "TIMESTAMP",
}

SCHEMA = pa.schema(
    [
        ("payment_id", pa.string()),
        ("store_id", pa.string()),
        ("terminal_id", pa.string()),
        ("paid_at", pa.timestamp("us")),
        ("amount", pa.int64()),
        ("method", pa.string()),
        ("received_at", pa.timestamp("us")),
    ]
)


def folder_dates(raw_dir: str | Path) -> list[date]:
    """원시 폴더 아래 날짜 폴더들 (오름차순)."""
    root = Path(raw_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"{root} 가 없습니다")
    return sorted(
        date.fromisoformat(p.name) for p in root.iterdir() if p.is_dir() and FOLDER_RE.match(p.name)
    )


def files_for(raw_dir: str | Path, day: date) -> list[Path]:
    """그 날짜 폴더의 `*.jsonl` 파일 (이름순). 폴더가 없으면 빈 목록."""
    folder = Path(raw_dir) / day.isoformat()
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.jsonl") if p.is_file())


def read_events(paths: list[Path]) -> pa.Table:
    """jsonl 파일들을 읽어 `SCHEMA` 형식의 Table 로 돌려준다 (파일 순서대로). 파일이 없으면 빈 Table."""
    if not paths:
        return SCHEMA.empty_table()
    cols = "{" + ", ".join(f"'{k}': '{v}'" for k, v in COLUMNS.items()) + "}"
    files = "[" + ", ".join("'" + Path(p).as_posix().replace("'", "''") + "'" for p in paths) + "]"
    con = duckdb.connect()
    try:
        return con.sql(
            f"SELECT * FROM read_json({files}, format = 'newline_delimited', columns = {cols})"
        ).to_arrow_table()
    finally:
        con.close()
