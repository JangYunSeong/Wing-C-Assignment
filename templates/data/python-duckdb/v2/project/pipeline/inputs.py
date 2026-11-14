"""입력 파일 읽기 도우미 — 템플릿이 주는 잠긴 파일이다 (바꾸지 않는다, 쓰지 않아도 된다).

    from pipeline.inputs import data_files, read_rows

    rows = read_rows(data_files(raw_dir / "2026-09-01"))   # 한 폴더의 파일들 → 행 목록

- `data_files(폴더)`: 그 폴더 하나에 있는 데이터 파일(.jsonl·.csv·.parquet) 경로, 이름 순. 이름이 `_`·`.` 로 시작하는 파일은 뺀다.
  하위 폴더는 보지 않는다 — 어떤 폴더들을 읽을지는 작업 코드가 정하고, 폴더마다 부른다
  (파일 없이 하위 폴더만 있는 폴더에 부르면 ValueError — 상위 폴더를 잘못 준 것).
- `read_rows(파일들)`: 주어진 파일들을 순서대로 읽어 행(dict) 목록으로. 형식은 확장자로 정한다.
  값은 **형식 추정 없이 그대로** 돌려준다 — .jsonl 은 JSON 값 그대로(글자는 글자, 숫자는 숫자), .csv 는 모두 글자, .parquet 은 저장된 형식.
  날짜·시각도 글자 그대로라, 날짜 부분이 필요하면 작업 코드가 자른다 (예: "2026-09-01 10:00:00"[:10]).

행을 고르거나 합치거나 같은 행을 지우는 일은 하지 않는다 — 그것은 작업 코드의 몫이다.
DuckDB 로 집계하려면 pandas DataFrame 을 만든 뒤 SQL 에서 그 변수 이름을 표처럼 쓴다 (예: `df = pandas.DataFrame(rows)` → `SELECT … FROM df`).
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

SUFFIXES = (".jsonl", ".csv", ".parquet")


def data_files(folder: str | Path, suffixes: Iterable[str] = SUFFIXES) -> list[Path]:
    """폴더 하나의 데이터 파일 경로 (이름 순, `_`·`.` 로 시작하는 이름은 뺌). 폴더가 없으면 빈 목록."""
    d = Path(folder)
    if not d.is_dir():
        return []
    wanted = tuple(suffixes)
    files = sorted(
        p
        for p in d.iterdir()
        if p.is_file() and p.name.endswith(wanted) and not p.name.startswith(("_", "."))
    )
    subdirs = sorted(
        p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))
    )
    if not files and subdirs:
        raise ValueError(
            f"data_files 는 폴더 하나의 파일만 본다 — {d} 에는 하위 폴더({', '.join(subdirs[:3])} …)만 있다. "
            "읽을 폴더마다 data_files(그 폴더) 를 부른다"
        )
    return files


def read_rows(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """파일들 → 행 목록 (파일 순서, 파일 안 순서 그대로). 값은 형식 추정 없이 그대로."""
    rows: list[dict[str, Any]] = []
    for path in paths:
        p = Path(path)
        if p.suffix == ".jsonl":
            with p.open(encoding="utf-8") as fh:
                rows += [json.loads(line) for line in fh if line.strip()]
        elif p.suffix == ".csv":
            with p.open(newline="", encoding="utf-8") as fh:
                rows += [dict(r) for r in csv.DictReader(fh)]
        elif p.suffix == ".parquet":
            rows += pq.read_table(p).to_pylist()
        else:
            raise ValueError(f"읽을 수 없는 입력 형식: {p.name} (.jsonl·.csv·.parquet 만)")
    return rows
