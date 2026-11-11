"""빌드 단계 검토 표시·수리 기록 채우기 — 적재가 명세의 표시만 넣고 빌드 보고서의 표시·수리 기록(run_alpha `build.*`)을 버리던
때 적재한 빌드에, 그 빌드 폴더(사본)의 보고서에서 붙인다 (2026-11-12). 명세의 표시는 그대로 두고 빌드 표시를 뒤에 덧붙이며, 수리 기록은
비어 있을 때만 채운다. 승인자 판단(review_decisions)은 표시 글의 해시로 짝지으므로 기존 표시 글은 바꾸지 않는다.

  uv run python -m eval.starter.backfill_build_flags --run data/mock_images/runs-1111/<빌드 사본> [--run …] [--db main]
빌드 사본의 task.json task_id(공통 과제 ct-…)로 그 공통 과제의 review 빌드를 찾는다. 기본은 시험 DB, 운영 DB 는 `--db main` 으로만.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from eval.starter.import_mock_builds import build_flags_of, merged_flags, repairs_of
from eval.starter.register_mock_common import dsn
from task_spec.config import Settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True, help="이미지를 만든 빌드 사본 폴더")
    ap.add_argument(
        "--builds", default="data/eval/build", help="알파 결과 상위 (손 예시 루프의 원래 report)"
    )
    ap.add_argument("--db", choices=["test", "main"], default="test")
    a = ap.parse_args()
    s = Settings()  # type: ignore[call-arg]
    with psycopg.connect(dsn(s, a.db), row_factory=dict_row) as conn:
        for run in map(Path, a.run):
            cid = json.loads((run / "task.json").read_text("utf-8"))["task_id"]
            flags = build_flags_of(run, Path(a.builds))
            repairs = [r.model_dump(mode="json") for r in repairs_of(run, Path(a.builds))]
            rows = conn.execute(
                "SELECT id, review_flags, repairs FROM public.common_task_build "
                "WHERE common_id = %s AND status = 'review'",
                (cid,),
            ).fetchall()
            for r in rows:
                new_flags = merged_flags(r["review_flags"] or [], flags)
                new_repairs = r["repairs"] or repairs
                conn.execute(
                    "UPDATE public.common_task_build SET review_flags = %s, repairs = %s WHERE id = %s",
                    (Jsonb(new_flags), Jsonb(new_repairs), r["id"]),
                )
                print(
                    f"{r['id']}: 표시 {len(r['review_flags'] or [])} → {len(new_flags)}, "
                    f"수리 {len(r['repairs'] or [])} → {len(new_repairs)}"
                )
            if not rows:
                print(f"{cid}: review 빌드 없음 — 건너뜀")
        conn.commit()


if __name__ == "__main__":
    main()
