"""README 를 저장하지 않고 적재한 빌드에 지원자 README 를 채운다 (starter_file kind='readme').

응시용 이미지의 `<workspace>/README.md`(지원자가 실제로 받은 문장)를 꺼내고, 같은 명세·계약·템플릿으로 다시 렌더한
결과와 해시가 같을 때만 `put_readme` 로 넣는다. 다르면 넣지 않고 알린다 — 렌더 코드·템플릿이 그 뒤 바뀐 것이므로 사람이 본다.
이미지는 로컬 podman 에 있어야 하고, image_id 가 등록된 응시용 묶음(build_artifact)과 같아야 한다.

    uv run python -m eval.starter.backfill_readme              # review 빌드 중 README 없는 것 — 비교만
    uv run python -m eval.starter.backfill_readme --write      # 같으면 저장
    uv run python -m eval.starter.backfill_readme --build ct-…-b1 --write

로그에는 build_id·해시·길이만 남긴다. 지원자 데이터 없음 — 공고·명세에서 만든 공통 과제.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import subprocess

from psycopg.rows import dict_row

from eval.starter.split_delivery import extract_readme
from task_spec.config import get_settings
from task_spec.schemas import TaskItem
from task_spec.schemas.build_record import README_PATH, BuildRecord, StarterFile
from task_spec.starter.render import render_readme
from task_spec.starter.store import PgBuildStore
from task_spec.starter.templates import load_template


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _image_id(ref: str) -> str:
    return subprocess.run(
        ["podman", "image", "inspect", "--format", "{{.Id}}", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def _task(store: PgBuildStore, common_id: str) -> TaskItem:
    async with store._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT task FROM public.common_task WHERE id = %s", (common_id,))
        r = await cur.fetchone()
    if r is None:
        raise KeyError(common_id)
    return TaskItem.model_validate(r["task"])


async def backfill(store: PgBuildStore, build: BuildRecord, write: bool) -> str:
    bid = build.build_id
    if await store.readme(bid) is not None:
        return "이미 있음"
    if build.outcome is None:
        return "outcome 없음 — 넘어감"
    cand = next((a for a in await store.artifacts(bid) if a.role == "candidate"), None)
    if cand is None or cand.image_id is None:
        return "응시용 묶음 없음 — 넘어감"
    tid, _, ver = build.outcome.template_ref.partition("@v")
    template = load_template(tid, int(ver))
    image = cand.image_id.removeprefix("sha256:")
    got = _image_id(image)
    if got.removeprefix("sha256:") != image:
        return f"로컬 이미지 {got[:19]} ≠ 등록 {cand.image_id[:19]} — podman load 필요"
    shown = extract_readme(image, template.manifest.workspace)
    rendered = render_readme(
        await _task(store, build.common_id), build.outcome.contract, template
    ).content
    if _sha(shown) != _sha(rendered):
        return (
            f"다름 — 이미지 {_sha(shown)[:12]} ({len(shown)}자) · 다시 렌더 "
            f"{_sha(rendered)[:12]} ({len(rendered)}자). 넣지 않음"
        )
    if write:
        await store.put_readme(StarterFile.of(bid, "readme", README_PATH, shown))
    return f"같음 {_sha(shown)[:12]} ({len(shown)}자){' — 저장' if write else ''}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="append", help="build_id (여러 번). 없으면 review 빌드 전부")
    ap.add_argument("--write", action="store_true", help="같을 때 저장 (없으면 비교만)")
    a = ap.parse_args()
    store = await PgBuildStore.open(get_settings().database_url.get_secret_value())
    try:
        if a.build:
            builds = [b for b in [await store.get(x) for x in a.build] if b is not None]
        else:
            async with store._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM public.common_task_build WHERE status = 'review' ORDER BY id"
                )
                ids = [r[0] for r in await cur.fetchall()]
            builds = [b for b in [await store.get(x) for x in ids] if b is not None]
        for b in builds:
            print(f"{b.build_id}: {await backfill(store, b, a.write)}", flush=True)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
