"""뼈대 확인 — 진입점이 저장소 루트에서 실행되고 도움말을 낸다."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_entrypoint_shows_help() -> None:
    p = subprocess.run(
        [sys.executable, "pipeline/run.py", "--help"], cwd=ROOT, capture_output=True, text=True
    )
    assert p.returncode == 0 and "usage:" in p.stdout  # 옵션 이름은 과제 계약이 정한다
