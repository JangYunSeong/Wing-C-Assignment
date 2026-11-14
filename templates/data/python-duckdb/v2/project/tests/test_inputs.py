"""입력 읽기 도우미(pipeline/inputs.py) 확인 — 템플릿이 주는 잠긴 파일. 형식 추정 없이 값 그대로, 폴더 하나만."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from pipeline.inputs import data_files, read_rows


def test_reads_each_format_without_guessing_types(tmp_path: Path) -> None:
    day = tmp_path / "raw" / "2026-09-01"
    day.mkdir(parents=True)
    (day / "b.jsonl").write_text(
        json.dumps({"id": "P1", "at": "2026-09-01 10:00:00", "amount": 1000}) + "\n\n", "utf-8"
    )
    (day / "a.csv").write_text("id,at,amount\nP2,2026-09-01 11:00:00,500\n", "utf-8")
    pq.write_table(pa.table({"id": ["P3"], "amount": [7]}), day / "c.parquet")
    (day / "_SUCCESS").write_text("", "utf-8")
    (day / ".tmp.jsonl").write_text("깨진 임시 파일", "utf-8")
    (day / "sub").mkdir()
    (day / "sub" / "d.jsonl").write_text(json.dumps({"id": "X"}) + "\n", "utf-8")

    files = data_files(day)
    assert [p.name for p in files] == [
        "a.csv",
        "b.jsonl",
        "c.parquet",
    ]  # 이름 순, 숨은 파일·하위 폴더 뺌
    rows = read_rows(files)
    assert rows == [
        {"id": "P2", "at": "2026-09-01 11:00:00", "amount": "500"},  # csv 는 모두 글자
        {
            "id": "P1",
            "at": "2026-09-01 10:00:00",
            "amount": 1000,
        },  # jsonl 은 JSON 값 그대로 (시각도 글자)
        {"id": "P3", "amount": 7},
    ]
    assert data_files(tmp_path / "raw" / "없는-폴더") == []
    assert read_rows([]) == []


def test_parent_folder_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    """날짜 폴더들의 상위 폴더에 부르면 빈 목록 대신 알려 준다 — 2026-11-10 알파 14회차 허용 구현이 data_files(raw_dir) 로 0개를 읽었다."""
    import pytest

    (tmp_path / "raw" / "2026-09-01").mkdir(parents=True)
    (tmp_path / "raw" / "2026-09-01" / "a.jsonl").write_text("{}\n", "utf-8")
    with pytest.raises(ValueError, match="폴더마다"):
        data_files(tmp_path / "raw")
    (tmp_path / "empty").mkdir()
    assert data_files(tmp_path / "empty") == []  # 하위 폴더도 파일도 없으면 빈 목록
