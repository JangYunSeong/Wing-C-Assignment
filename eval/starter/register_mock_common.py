"""평가용 명세로 만든 빌드 폴더를 목 공통 과제(`common_task`, draft)로 등록한다 — 빌드 저장소 FK 용 (이슈 #85).

평가 스크립트(run_feature → run_alpha_runs)로 만든 명세는 DB 에 공통 과제 행이 없고 task_id 가 `eval-<공고>` 라
`common_task_build` 에 넣을 수 없다. 이 스크립트는 명세를 draft 행으로 저장해 `ct-…` id 를 받고, 빌드 폴더를 복사해
task.json 의 task_id 를 그 id 로 바꾼다 — 복사본으로 build_mock_images 를 돌리면 이미지 라벨·이름에 `ct-…` 가 들어간다.

재사용 막기: 공통 과제는 (공고, 온톨로지 버전, prompt_ref) 가 같으면 재사용한다. 목 행은 prompt_ref 를 `mock:<원래>` 로 둬
실제 지원자 실행의 조회 키와 겹치지 않는다. draft 라 `approved_kind` 에도 잡히지 않는다.

  uv run python -m eval.starter.register_mock_common --run data/mock_images/runs/nplus1-k --out data/mock_images/runs [--db main]
기본은 시험 DB(TEST_DATABASE_URL). 운영 DB 는 `--db main` 으로만. 출력·DB 행 모두 지원자 데이터 없음 (공고에서 만든 명세).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
from pathlib import Path

from task_spec.config import Settings
from task_spec.schemas import TaskItem
from task_spec.spec.store import CommonKey, CommonTaskStore

MOCK_PREFIX = "mock:"
PROMPT_REF = "draft_feature_task"  # 평가 명세를 만든 프롬프트 (판은 빌드 폴더에 남지 않아 이름만)


def mock_key(task_id: str, ontology_version: str) -> CommonKey:
    """`eval-<공고 id>` → 목 공통 과제 키. 공고 id 의 '#' 는 평가 스크립트가 그대로 둔다."""
    m = re.fullmatch(r"(?:eval|alpha|smoke)-(.+)", task_id)
    if m is None:
        raise ValueError(f"평가용 명세가 아니다: {task_id}")
    return CommonKey(
        posting_id=m.group(1),
        ontology_version=ontology_version,
        prompt_ref=MOCK_PREFIX + PROMPT_REF,
    )


def dsn(settings: Settings, which: str) -> str:
    if which == "main":
        return settings.database_url.get_secret_value()
    if settings.test_database_url is None:
        raise SystemExit("TEST_DATABASE_URL 이 없다 — 운영 DB 는 --db main 으로만")
    return settings.test_database_url.get_secret_value()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--run", action="append", required=True, help="빌드 폴더 (task.json·report.json)"
    )
    ap.add_argument("--out", required=True, help="task_id 를 바꾼 복사본을 둘 곳")
    ap.add_argument("--db", choices=["test", "main"], default="test")
    a = ap.parse_args()
    s = Settings()  # type: ignore[call-arg]
    store = await CommonTaskStore.open(dsn(s, a.db))
    try:
        for r in map(Path, a.run):
            task = TaskItem.model_validate_json((r / "task.json").read_text("utf-8"))
            if task.task_id.startswith("ct-"):
                print(f"{r.name}: 이미 공통 과제 id ({task.task_id}) — 건너뜀")
                continue
            key = mock_key(task.task_id, s.ontology_version)
            row = await store.save_draft(key, task, [])
            dest = Path(a.out) / f"{r.name}-{row.id}"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(r, dest)
            (dest / "task.json").write_text(row.task.model_dump_json(indent=1), "utf-8")
            report = json.loads((dest / "report.json").read_text("utf-8"))
            report["ontology_version"] = s.ontology_version
            report["mock_common"] = {"from": task.task_id, "prompt_ref": key.prompt_ref}
            (dest / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=1), "utf-8"
            )
            print(f"{r.name}: {task.task_id} → {row.id} (판 {row.revision}) → {dest}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
