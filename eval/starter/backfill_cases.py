"""케이스별 결과 채우기 — 케이스 결과(cases)를 저장하기 전에 적재한 빌드에, 그 이미지를 검증할 때 남긴 채점 결과를 붙인다.

build_mock_images 는 검증 대상마다 채점 결과를 `<out>/samples/<common_id>-r<판>/grade-<대상>/result.json` 에 남긴다. 빌드 저장소의
`common_task_build.verification` 은 대상별 통과 수만 담고 있어, 빌드 화면 '테스트' 절(승인자가 문제를 쓸지 정할 때)의 대상별 결과 칸이
비었다. 이 스크립트는 그 칸만 채운다 — 통과 수·다른 칸은 바꾸지 않고, 이미 cases 가 있는 대상은 건드리지 않는다.
통과 수가 저장된 값과 다르면(다른 검증의 결과) 그 대상은 건너뛴다.

  uv run python -m eval.starter.backfill_cases --manifest data/mock_images/2026-11-11/manifest.json [--manifest …] [--db main]
기본은 시험 DB(TEST_DATABASE_URL), 운영 DB 는 `--db main` 으로만.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from eval.starter.register_mock_common import dsn
from task_spec.config import Settings
from task_spec.schemas.package import VerifySummary
from task_spec.starter.artifacts import case_results

_COUNTS = ("hidden_passed", "hidden_total", "public_passed", "public_total")


def fill(verification: dict[str, Any], samples: Path) -> tuple[dict[str, Any], list[str]]:
    """저장된 verification 에 samples 의 케이스 결과를 붙인 새 dict 와 채운 대상 이름."""
    out = dict(verification)
    filled: list[str] = []
    for name, v in verification.items():
        if v.get("cases"):
            continue
        res = samples / f"grade-{name}" / "result.json"
        if not res.is_file():
            continue
        result = json.loads(res.read_text("utf-8"))
        summary = result.get("summary") or {}
        if any(summary.get(k) != v.get(k) for k in _COUNTS):
            continue
        cases = case_results(result.get("cases"))
        VerifySummary.model_validate({**v, "cases": cases})  # 저장 전 모양 확인
        out[name] = {**v, "cases": cases}
        filled.append(name)
    return out, filled


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", action="append", required=True)
    ap.add_argument("--db", choices=["test", "main"], default="test")
    a = ap.parse_args()
    s = Settings()  # type: ignore[call-arg]
    with psycopg.connect(dsn(s, a.db), row_factory=dict_row) as conn:
        for m in map(Path, a.manifest):
            for t in json.loads(m.read_text("utf-8"))["tasks"]:
                samples = m.parent / "samples" / f"{t['common_id']}-r{t['revision']}"
                rows = conn.execute(
                    "SELECT id, verification FROM public.common_task_build "
                    "WHERE common_id = %s AND grader_sha256 = %s",
                    (t["common_id"], t["grader"]["grader_sha256"]),
                ).fetchall()
                for r in rows:
                    new, filled = fill(r["verification"] or {}, samples)
                    if filled:
                        conn.execute(
                            "UPDATE public.common_task_build SET verification = %s WHERE id = %s",
                            (Jsonb(new), r["id"]),
                        )
                    print(f"{r['id']}: {', '.join(filled) or '채울 대상 없음'}")
        conn.commit()


if __name__ == "__main__":
    main()
