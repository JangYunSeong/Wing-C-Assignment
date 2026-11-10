"""배치 작업 진입점 — 시작 코드가 과제의 계약(명령·옵션·출력 형식)대로 채운다.

    python pipeline/run.py --date 2026-10-11 --raw data/raw --out out

저장소 루트에서 실행된다. 의존성은 pyproject.toml 에 있는 것(DuckDB·pyarrow·pandas·numpy)만 쓴다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run(day: date, raw_dir: Path, out_dir: Path) -> None:
    raise NotImplementedError("pipeline/run.py 의 run() 을 구현하세요")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="배치 작업")
    ap.add_argument("--date", required=True, type=date.fromisoformat, help="실행 날짜 YYYY-MM-DD")
    ap.add_argument("--raw", required=True, type=Path, help="입력 폴더")
    ap.add_argument("--out", required=True, type=Path, help="출력 폴더")
    args = ap.parse_args(argv)
    try:
        run(args.date, args.raw, args.out)
    except Exception as e:  # 실패는 원인을 stderr 에 한 줄 남기고 계약의 실패 종료 코드로 (말없이 삼키지 않는다)
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
