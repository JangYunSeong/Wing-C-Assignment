"""운영 빌드 → 알파 폴더 (손 시범 재료용, docs/design/analysis-stage.md §9.1 재료).

손 시범 재료는 `run_alpha` 산출물 구조(`data/eval/build/<알파>/{task.json, contract.json, candidate/, overlays/<종류>/}`)를 읽는다.
builder 로 만든 운영 빌드는 DB(`common_task`·`common_task_build`·`starter_file`)에만 있어, 같은 구조로 꺼낸다.
`candidate/` 는 응시용 이미지와 같은 층 — 템플릿 project/ + 시작 코드 + 저장된 README (run_alpha 와 같은 조립).
지원자 데이터 없음 (공통 과제는 공고·재료로만 만든다). 결과는 git 밖 `data/eval/build/`.

  uv run python -m eval.pilot.export_build --build ct-4fb4cffa89b4-b2
  # → data/eval/build/<빌드 날짜>-prod-ct-4fb4cffa89b4-b2/
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from task_spec.config import Settings
from task_spec.schemas import TaskItem
from task_spec.schemas.build import GeneratedFile
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import load_template
from task_spec.starter.verify.workspace import assemble, write_overlay

ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = ROOT / "data" / "eval" / "build"
# DB 의 파일 종류·변형 → run_alpha 의 overlays/<이름> (ref_accepted 는 설계 id, ref_naive 는 변형 이름)
NAMED = {"starter": "starter", "public_test": "public", "hidden": "hidden"}


def overlay_key(kind: str, variant: str | None) -> str:
    if kind in NAMED:
        return NAMED[kind]
    if kind in ("ref_accepted", "ref_naive") and variant:
        return variant
    raise ValueError(f"알 수 없는 파일 종류 {kind}/{variant}")


def fetch(dsn: str, build_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        b = conn.execute(
            "select b.id, b.status, b.created_at, b.template_ref, b.contract, b.hidden_plan, t.task "
            "from common_task_build b join common_task t on t.id = b.common_id where b.id = %s",
            (build_id,),
        ).fetchone()
        if b is None:
            raise SystemExit(f"빌드 없음: {build_id}")
        files = conn.execute(
            "select kind, variant, path, content from starter_file where build_id = %s order by kind, variant, path",
            (build_id,),
        ).fetchall()
    return b, files


def export(b: dict[str, Any], files: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    if b["contract"] is None or b["hidden_plan"] is None or not b["template_ref"]:
        raise SystemExit(
            f"계약·숨은 계획·템플릿이 없는 빌드 ({b['status']}) — 계약 단계를 통과한 빌드만 꺼낸다"
        )
    template_id, _, ver = b["template_ref"].partition("@v")
    template = load_template(template_id, int(ver) if ver else None)
    task = TaskItem.model_validate(b["task"])

    out.mkdir(parents=True, exist_ok=True)
    (out / "task.json").write_text(task.model_dump_json(indent=1), "utf-8")
    (out / "contract.json").write_text(
        json.dumps(
            {"contract": b["contract"], "hidden_plan": b["hidden_plan"]},
            ensure_ascii=False,
            indent=1,
        ),
        "utf-8",
    )
    groups: dict[tuple[str, str | None], list[GeneratedFile]] = defaultdict(list)
    readme: str | None = None
    for f in files:
        if f["kind"] == "readme":
            readme = f["content"]
            continue
        groups[(f["kind"], f["variant"])].append(
            GeneratedFile(path=f["path"], content=f["content"])
        )
    overlays: dict[str, Overlay] = {}
    for (kind, variant), fs in groups.items():
        key = overlay_key(kind, variant)
        overlays[key] = Overlay(kind=kind, files=tuple(fs), label=variant or "")  # type: ignore[arg-type]
        write_overlay(overlays[key], out / "overlays" / key)
    starter = overlays.get("starter")
    if starter is None:
        raise SystemExit("시작 코드가 없는 빌드")
    assemble(template, out / "candidate", starter)  # 응시용 이미지와 같은 층
    if readme is not None:
        (out / "candidate" / "README.md").write_text(readme, "utf-8")
    report = {
        "source": "builder",
        "build_id": b["id"],
        "status": b["status"],
        "created_at": b["created_at"].isoformat(),
        "template_ref": b["template_ref"],
        "overlays": {k: len(o.files) for k, o in sorted(overlays.items())},
        "readme": readme is not None,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="운영 빌드를 알파 폴더 구조로 꺼낸다 (손 시범 재료용)")
    ap.add_argument("--build", required=True, help="빌드 id (예: ct-4fb4cffa89b4-b2)")
    ap.add_argument(
        "--out", type=Path, default=None, help="기본: data/eval/build/<빌드 날짜>-prod-<빌드 id>/"
    )
    a = ap.parse_args()
    b, files = fetch(Settings().database_url.get_secret_value(), a.build)  # type: ignore[call-arg]
    out = a.out or BUILD_DIR / f"{b['created_at'].date().isoformat()}-prod-{b['id']}"
    report = export(b, files, out)
    print(out)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
