"""기존 기능 테스트의 되돌리지 않는 쓰기 비율 — gen_starter v12 첫 평가 지표 (#340, LLM 0).

시작 코드(Java)마다 기존 기능 테스트를 정적 검사와 같은 판정(`static_check.existing_test_scan`)으로 본다:
되돌리지 않는 쓰기가 있나 · 표 전체 행 수·목록 크기 단언이 있나 · 둘이 같은 컨텍스트에 함께 있나(정적 검사가 막는 꼴).
기준선 (gen_starter v11, 이 서버 알파 최종본): 기존 테스트가 있는 Java 시작 코드 20개 중 8개에 되돌리지 않는 쓰기 (40%).

    # 알파 산출물 (기본: data/eval/build/*/overlays/starter)
    uv run python -m eval.starter.scan_existing_tests
    # 빌드 DB — 운영 서버에서. --since 로 v12 배포 뒤만
    uv run python -m eval.starter.scan_existing_tests --db main --since 2026-11-28
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from eval.starter.register_mock_common import dsn
from task_spec.config import Settings
from task_spec.schemas import GeneratedFile
from task_spec.starter.static_check import existing_test_scan
from task_spec.starter.store import PgBuildStore

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Row:
    name: str
    when: str
    unrolled: bool  # 되돌리지 않는 쓰기가 있음
    sizes: bool  # 행 수·목록 크기 단언이 있음
    flagged: bool  # 둘이 같은 컨텍스트에 함께 — 정적 검사가 재요청하는 꼴
    files: list[str]  # 되돌리지 않는 쓰기 파일


def row(name: str, when: str, files: list[GeneratedFile]) -> Row | None:
    """기존 테스트가 있는 Java 시작 코드만 센다 (분모). 스프링 테스트가 없으면 None."""
    tests = [f for f in files if f.path.endswith(".java") and "/test/" in f.path]
    scan = existing_test_scan(tests)
    if not scan.groups:
        return None
    writes = sorted({n for names in scan.writes.values() for n in names})
    return Row(name, when, bool(writes), any(scan.sizes.values()), bool(scan.flagged), writes)


def from_dirs(pattern: str) -> list[Row]:
    out: list[Row] = []
    for root in sorted(ROOT.glob(pattern)):
        files = [
            GeneratedFile(
                path=f.relative_to(root).as_posix(), content=f.read_text("utf-8", "replace")
            )
            for f in root.rglob("*.java")
        ]
        when = datetime.fromtimestamp(root.stat().st_mtime, UTC).strftime("%Y-%m-%d")
        if (r := row(root.parent.parent.name, when, files)) is not None:
            out.append(r)
    return out


async def from_db(which: str, since: str | None, limit: int | None) -> list[Row]:
    store = await PgBuildStore.open(dsn(Settings(), which), ddl=False)  # type: ignore[call-arg]
    try:
        out: list[Row] = []
        for b in await store.recent(limit):
            when = b.created_at.strftime("%Y-%m-%d")
            if since is not None and when < since:
                continue
            files = [
                GeneratedFile(path=f.path, content=f.content)
                for f in await store.files(b.build_id, kinds=["starter"])
            ]
            if (r := row(b.build_id, when, files)) is not None:
                out.append(r)
        return out
    finally:
        await store.close()


def summary(rows: list[Row]) -> str:
    n = len(rows)
    if n == 0:
        return "기존 테스트가 있는 Java 시작 코드 없음"
    u = sum(r.unrolled for r in rows)
    f = sum(r.flagged for r in rows)
    return (
        f"기존 테스트가 있는 Java 시작 코드 {n}개 — 되돌리지 않는 쓰기 {u}개 ({u / n:.0%}) · "
        f"행 수 단언 {sum(r.sizes for r in rows)}개 · 함께(정적 검사가 막는 꼴) {f}개 "
        "[기준선 v11: 20개 중 8개 = 40%]"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument(
        "--db",
        choices=["test", "main"],
        default=None,
        help="빌드 DB 에서 읽음 (없으면 알파 산출물 폴더)",
    )
    ap.add_argument(
        "--dirs",
        default="data/eval/build/*/overlays/starter",
        help="알파 산출물 시작 코드 폴더 글롭 (레포 기준)",
    )
    ap.add_argument("--since", default=None, help="이 날짜(YYYY-MM-DD) 뒤 빌드만 — DB 일 때")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", default=None, help="행을 JSON 으로 저장할 경로")
    a = ap.parse_args()
    rows = await from_db(a.db, a.since, a.limit) if a.db else from_dirs(a.dirs)
    for r in rows:
        mark = "함께" if r.flagged else ("쓰기" if r.unrolled else "-")
        print(f"{r.when} {r.name} {mark} {', '.join(r.files)}")
    print(summary(rows))
    if a.json:
        Path(a.json).write_text(
            json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=1), "utf-8"
        )


if __name__ == "__main__":
    asyncio.run(main())
