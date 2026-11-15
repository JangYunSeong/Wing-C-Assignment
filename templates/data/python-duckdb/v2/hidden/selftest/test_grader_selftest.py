"""하네스 자체 점검 (잠긴 하네스). 검증기가 케이스보다 먼저 돌린다 — 여기서 실패하면 결과는 HARNESS-ERR 이고 지원자·참조 구현의
판정을 내지 않는다. 과제 코드와 무관하게 템플릿 뼈대만으로 통과해야 한다 (지원자 작업 대신 작은 확인용 스크립트)."""

from __future__ import annotations

from datetime import date, datetime

from harness.grader import params, reason, run, tag, workspace

PROBE = """
import os, sys, time
from pathlib import Path
mode = sys.argv[1]
if mode == "exit":
    print("hello", os.environ.get("TZ"))
    sys.exit(int(sys.argv[2]))
if mode == "write":
    target = Path(sys.argv[2])
    try:
        with target.open("wb") as fh:
            for _ in range(64):
                fh.write(b"x" * 16384)
                fh.flush()
    finally:
        print("cleanup ran")
if mode == "sleep":
    time.sleep(float(sys.argv[2]))
"""


def _probe(ws) -> str:  # type: ignore[no-untyped-def]
    return str(ws.write_text("probe.py", PROBE))


def test_exit_code_output_and_fixed_timezone() -> None:
    ws = workspace()
    r = run(_probe(ws), "exit", "3")
    assert r.code == 3 and not r.ok and not r.crashed
    assert r.stdout.strip() == "hello Asia/Seoul"
    assert run(_probe(ws), "exit", "0").ok


def test_crash_injection_kills_on_first_large_write_without_cleanup() -> None:
    ws = workspace()
    target = ws.path("big.bin")
    r = run(_probe(ws), "write", str(target), crash_after_bytes=128)
    assert r.crashed, (r.code, r.stderr[-300:])
    assert target.stat().st_size <= 128
    assert "cleanup ran" not in r.stdout  # 정리 코드가 돌지 않는다 — 실제 장애처럼
    normal = run(_probe(ws), "write", str(ws.path("big2.bin")))
    assert (
        normal.ok
        and "cleanup ran" in normal.stdout
        and ws.path("big2.bin").stat().st_size == 64 * 16384
    )


def test_timeout_is_reported() -> None:
    ws = workspace()
    r = run(_probe(ws), "sleep", "5", timeout=0.5)
    assert r.timed_out and r.code is None


def test_files_roundtrip_in_contract_formats() -> None:
    ws = workspace()
    rows = [{"id": "a", "n": 1, "d": date(2026, 9, 1), "t": datetime(2026, 9, 1, 23, 59, 59)}]
    ws.write_parquet(
        "out/part=1/a.parquet", rows, {"id": "string", "n": "int", "d": "date", "t": "timestamp"}
    )
    ws.write_parquet(
        "out/part=1/b.parquet", [{"id": "b", "n": 2, "d": date(2026, 9, 2), "t": None}]
    )
    ws.write_text("out/part=1/_SUCCESS", "")
    ws.write_text("out/part=1/.tmp.parquet", "깨진 임시 파일")
    got = sorted(ws.read_parquet("out/part=1"), key=lambda r: r["id"])
    assert (
        [r["id"] for r in got] == ["a", "b"]
        and got[0]["n"] == 1
        and got[0]["d"] == date(2026, 9, 1)
    )
    assert ws.partitions("out", "part") == ["1"]
    ws.write_jsonl("raw/2026-09-01/batch.jsonl", [{"k": 1}, {"k": 2}])
    assert ws.read_jsonl("raw/2026-09-01") == [{"k": 1}, {"k": 2}]
    ws.write_csv("x.csv", [{"a": 1, "b": "z"}])
    assert ws.read_csv("x.csv") == [{"a": "1", "b": "z"}]
    before = ws.snapshot("out")
    assert set(before) == {
        "part=1/a.parquet",
        "part=1/b.parquet",
        "part=1/_SUCCESS",
        "part=1/.tmp.parquet",
    }
    ws.write_text("out/part=1/_SUCCESS", "바뀜")
    assert ws.snapshot("out") != before


def test_reason_tag_and_param_fallback() -> None:
    assert (
        reason("late_data_missed", "앞 날짜 값이 그대로")
        == "[reason:late_data_missed] 앞 날짜 값이 그대로"
    )
    assert tag("x") == "[reason:x]"
    assert (
        params.int("없는_조절값", 7) == 7
        and params.str("없음", "a") == "a"
        and params.list("없음", [1]) == [1]
    )


def test_workspace_works_like_a_path() -> None:
    """ws / "x" · str(ws) · f"{ws}/x" · os.fspath(ws) 가 모두 작업 폴더 안 경로 (pytest tmp_path 처럼 쓰는 테스트)."""
    import os

    ws = workspace()
    assert ws / "raw" == ws.path("raw") and str(ws) == str(ws.root) == os.fspath(ws)
    assert f"{ws}/raw" == str(ws.path("raw"))
    ws.write_text("raw/a.txt", "x")
    assert (ws / "raw" / "a.txt").read_text() == "x"


def test_folder_reads_include_partition_subfolders_and_skip_hidden() -> None:
    """파티션 상위 폴더를 읽으면 date=…/ 아래 파일까지 (읽는 쪽이 보는 것과 같게), _·. 로 시작하는 파일·폴더는 뺀다."""
    ws = workspace()
    ws.write_parquet("out/daily/date=2026-09-01/a.parquet", [{"id": "a"}])
    ws.write_parquet("out/daily/date=2026-09-02/b.parquet", [{"id": "b"}])
    ws.write_parquet("out/daily/.tmp-x/c.parquet", [{"id": "c"}])
    ws.write_parquet("out/daily/date=2026-09-02/_part.parquet", [{"id": "d"}])
    assert sorted(r["id"] for r in ws.read_parquet("out/daily")) == ["a", "b"]
    assert [p.name for p in ws.files("out/daily")] == ["a.parquet", "b.parquet"]
    assert ws.read_parquet("out/daily/date=2026-09-01") == [{"id": "a"}]


def test_missing_output_is_not_an_empty_output() -> None:
    """v2: 없는 경로는 FileNotFoundError (빈 목록이 아니다) · 있는지는 exists · 있는 빈 폴더는 빈 목록 — 크래시 뒤 '출력 없음'을 가른다."""
    import pytest

    ws = workspace()
    assert not ws.exists("out/2026-09-01.csv")
    for read in (ws.read_csv, ws.read_jsonl, ws.read_parquet):
        with pytest.raises(FileNotFoundError):
            read("out/2026-09-01.csv")
    ws.dir("out/empty")
    assert ws.exists("out/empty") and ws.read_csv("out/empty") == []


def test_half_written_csv_is_a_broken_file() -> None:
    """v2: 칸 수가 머리글과 다른 CSV 줄(반쯤 쓴 마지막 줄)은 깨진 파일 — parquet·jsonl 처럼 읽기에서 예외."""
    import pytest

    ws = workspace()
    ws.write_text("x.csv", "a,b\n1,2\n3")
    with pytest.raises(ValueError, match="깨진 CSV"):
        ws.read_csv("x.csv")
    ws.write_text("y.csv", "a,b\n1,2,3\n")
    with pytest.raises(ValueError):
        ws.read_csv("y.csv")


def test_requested_crash_that_does_not_happen_is_a_harness_error() -> None:
    """v2: crash_after_bytes 는 파일 하나의 크기 상한 — 쓰는 파일이 모두 그보다 작아 끝까지 성공하면 CrashNotTriggered (케이스 전제가 서지 않음).
    작업이 오류로 먼저 끝나면 그대로 돌려준다 (구현 탓)."""
    import pytest

    from harness.grader import CrashNotTriggered

    ws = workspace()
    small = ws.path("small.py")
    small.write_text("open(__import__('sys').argv[1], 'w').write('x' * 10)\n", "utf-8")
    with pytest.raises(CrashNotTriggered):
        run(str(small), ws.path("out.txt"), crash_after_bytes=128)
    big = ws.path("big.py")
    big.write_text("open(__import__('sys').argv[1], 'w').write('x' * 4096)\n", "utf-8")
    assert run(str(big), ws.path("out2.txt"), crash_after_bytes=128).crashed
    fail = ws.path("fail.py")
    fail.write_text("raise SystemExit(3)\n", "utf-8")
    assert run(str(fail), crash_after_bytes=128).code == 3
