"""성공한 빌드(status=review)로 과제마다 응시용·채점용 이미지를 만들어 목 데이터로 묶는다 (docs/design/starter-templates.md §6.1).

만들기·검증 코드는 `task_spec.starter.images` (#126 — builder 와 같은 코드). 여기는 알파 빌드 폴더를 재료(`TaskMaterial`)로 읽고
여러 과제를 한 묶음(images.tar · manifest.json)으로 내는 명령줄만.

    uv run python -m eval.starter.build_mock_images            # 기본 3과제
    uv run python -m eval.starter.build_mock_images --run data/eval/build/<빌드 폴더> ...

층: 런타임(Java: JDK 17 / Node: node 22 bookworm-slim / Python: python 3.11 slim-bookworm) → 템플릿 층(템플릿 project/ + 오프라인
의존성 — Gradle 캐시·node_modules·uv.lock 그대로 만든 가상 환경 /opt/venv, 하네스·manifest 없음) → 과제 층. 빌드 폴더의 직군(report.json sub)으로
템플릿을 고른다 (select_template — 2026-11-09 React·데이터 템플릿).
  응시용 = 템플릿 층 + 시작 코드 + README (숨은 테스트 없음)
  채점용 = 템플릿 층 + python3 + grade(task_spec/starter/grade/ — Java image_grade · Node image_grade_node · Python image_grade_py)
           + 숨은 테스트·하네스·조절값 + config.json
참조 구현(ref_*)은 어느 이미지에도 넣지 않는다 — 검증 때 제출물로만 쓴다.

검증 (모두 --network none): 응시용에서 기존 테스트가 오프라인으로 통과, 채점용으로 시작 코드만·허용 구현·흔한 구현 제출물을 채점.
응시용 이미지의 모든 레이어에 금지 경로(숨은 테스트·하네스·manifest)가 없는지 저장한 묶음에서 확인한다.
출력: data/mock_images/<날짜>/ — images.tar · manifest.json · samples/ (git 밖, 전달용). 빌드 작업 폴더는 data/mock_images/build-context/<날짜>/. podman 저장소는 data/mock_images/storage 에
넣되 이 작업 이름(localhost/task…)으로만 — 다른 이미지·컨테이너는 건드리지 않는다. 빌드 임시 파일은 data/mock_images/tmp. 지원자 데이터 없음 — 공고·재료·명세에서 만든 공통 과제.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from pathlib import Path

from eval.starter.replay_contracts import _loose as loose_contract
from task_spec.schemas import GeneratedFile, HiddenPlan, TaskContract, TaskItem
from task_spec.starter.images import (
    Layers,
    TaskImages,
    TaskMaterial,
    build_layers,
    build_task_images,
    manifest_entry,
    save_archive,
    scan_candidates,
    verify_task_images,
)
from task_spec.starter.layers import Podman, stack_for
from task_spec.starter.render import render_readme
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template, load_template, select_template

ROOT = Path(__file__).resolve().parents[2]
BUILDS = ROOT / "data" / "eval" / "build"
DEFAULT_RUNS = [  # 과제별 최신 성공 빌드 (status=review — 2026-11-08 사용자 선택 "과제별 최신 1개씩")
    "2026-11-08-alpha-full-sg004",  # 테스트 먼저 흐름 (공개 테스트 있음)
    "2026-11-08-alpha-full-ae023",  # 테스트 먼저 흐름 (공개 테스트 있음)
    "2026-11-07-alpha-kakaobank005-r2",  # 같은 날 전체 알파는 하네스 버그로 failed — 성공 판정 받은 빌드만
    "2026-11-07-loop-delivery-r5",
    # 프론트엔드 React (2026-11-09 알파 3공고 — 전부 review)
    "2026-11-09-alpha-fe-kb007-r4",
    "2026-11-09-alpha-fe-naver003",
    "2026-11-09-alpha-fe-autoever048-r2",
]
JAVA_TEMPLATE = "backend/java-spring-boot3-jpa"


def template_of(run: Path) -> Template:
    """빌드 폴더의 직군 → 템플릿 (직군이 없는 손 예시 루프는 Java). 알파가 판을 고정했으면(report.template_pinned,
    run_alpha --template-version) 그 판 — 빌드·검증한 판과 이미지·저장 기록(template_ref)이 같게 (2026-11-11: v2 로 돈 백엔드 빌드가
    CURRENT v1 로 기록될 뻔했다)."""
    report = json.loads((run / "report.json").read_text("utf-8"))
    pinned = report.get("template_pinned")
    if isinstance(pinned, str) and "@v" in pinned:
        tid, ver = pinned.rsplit("@v", 1)
        return load_template(tid, int(ver))
    sub = report.get("sub")
    return (select_template(sub) if sub else None) or load_template(JAVA_TEMPLATE)


def _overlay(kind: str, root: Path, label: str = "") -> Overlay | None:
    if not root.is_dir():
        return None
    files = tuple(
        GeneratedFile(path=str(p.relative_to(root)), content=p.read_text("utf-8"))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )
    return Overlay(kind=kind, files=files, label=label)  # type: ignore[arg-type]


def _common_id(task_id: str) -> str:
    raw = re.sub(r"^(alpha|smoke)-", "", task_id).replace("#", "-").lower()
    return re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-")


def load_run(run: Path, t: Template) -> TaskMaterial:
    """빌드 폴더 → 과제 재료 (명세·계약·숨은 계획·오버레이·README)."""
    report = json.loads((run / "report.json").read_text("utf-8"))
    status = (report.get("build") or report).get("status")
    if status != "review":
        raise ValueError(f"{run.name}: 성공한 빌드가 아님 (status={status})")
    if (run / "task.json").is_file():
        task = TaskItem.model_validate_json((run / "task.json").read_text("utf-8"))
        contract_file = run / "contract.json"
    else:  # 손 예시 루프 — 명세는 손 예시에서, 계약은 그때 쓴 파일에서
        from eval.starter.run_plan_contract import build_task

        task, _ = build_task(ROOT / "data" / "feature_starters" / "delivery-audit")
        contract_file = ROOT / str(report["contract"])
    data = json.loads(contract_file.read_text("utf-8"))
    # 코드가 붙인 문장으로 200자를 넘은 제약 칸 — 파이프라인 안에서는 다시 검증하지 않는다 (회귀 검사와 같게 읽을 때만 자름)
    contract = TaskContract.model_validate(loose_contract(data["contract"]))
    plan = HiddenPlan.model_validate(data["hidden_plan"])
    ov = run / "overlays"
    readme = (
        (run / "candidate" / "README.md").read_text("utf-8")
        if (run / "candidate" / "README.md").is_file()
        else render_readme(task, contract, t).content
    )
    return TaskMaterial(
        source=run.name,
        task=task,
        contract=contract,
        plan=plan,
        common_id=_common_id(task.task_id),
        revision=int(report.get("revision") or 1),
        ontology_version=report.get("ontology_version") or "unknown",
        readme=readme,
        starter=_overlay("starter", ov / "starter"),
        hidden=_overlay("hidden", ov / "hidden"),
        public=_overlay("public_test", ov / "public"),
        refs={
            name: _overlay(
                "ref_naive" if name.startswith("naive") else "ref_accepted", ov / name, name
            )
            for name in sorted(p.name for p in ov.iterdir() if p.is_dir())
            if name not in ("starter", "hidden", "public")
        },
    )


def _say(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--run", action="append", default=[], help="빌드 폴더 (data/eval/build/<이름>) — 여러 번"
    )
    ap.add_argument("--out", default=str(ROOT / "data" / "mock_images" / str(date.today())))
    ap.add_argument("--store", default=None, help="podman 저장소 경로 (기본: 시스템 저장소)")
    ap.add_argument("--skip-verify", action="store_true")
    a = ap.parse_args()
    runs = [Path(r) if "/" in r else BUILDS / r for r in (a.run or DEFAULT_RUNS)]
    run_templates = [(run, template_of(run)) for run in runs]
    out = Path(
        a.out
    ).resolve()  # podman -v 는 상대 경로를 이름 있는 볼륨으로 본다 (2026-11-09: 채점 결과가 비었다)
    # 빌드 작업 폴더(참조 구현·숨은 테스트 평문 포함)와 로그는 전달 폴더 밖에 — 전달물에 섞이지 않게
    ctx = ROOT / "data" / "mock_images" / "build-context" / out.name
    out.mkdir(parents=True, exist_ok=True)
    ctx.mkdir(parents=True, exist_ok=True)
    log = ctx / "build.log"
    pm = Podman(ROOT / "data" / "mock_images" / "tmp", Path(a.store) if a.store else None)

    t0 = time.monotonic()
    layers: dict[str, Layers] = {
        ref: build_layers(t, ctx, pm, log, _say)
        for ref, t in {t.manifest.ref: t for _, t in run_templates}.items()
    }
    tasks: list[TaskImages] = []
    for run, t in run_templates:
        item = load_run(run, t)
        _say(f"③ {item.common_id} ({run.name})")
        tasks.append(build_task_images(t, item, layers[t.manifest.ref], ctx, pm, log))

    samples = out / "samples"
    if not a.skip_verify:
        for x in tasks:
            verify_task_images(x, ctx, samples, pm, _say)

    _say("⑤ 저장 (여러 이미지 한 묶음 — 공통 레이어는 한 번)")
    archive = out / "images.tar"
    save_archive(tasks, archive, pm, log)
    leaks = scan_candidates(archive, {x.candidate: x.template for x in tasks})
    _say(f"   응시용 레이어 금지 경로: {leaks}")

    entries = [manifest_entry(x, pm, leaks) for x in tasks]
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "created": str(date.today()),
                "archive": "images.tar",
                "archive_format": "docker-archive (podman save --multi-image-archive) — podman load -i images.tar",
                "base_images": {t.manifest.ref: stack_for(t).base for _, t in run_templates},
                "tasks": entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    _say(
        f"→ {out} ({round(time.monotonic() - t0)}초, images.tar {round(archive.stat().st_size / 1e9, 2)} GB)"
    )


if __name__ == "__main__":
    main()
