"""반입 묶음을 받는 곳별로 나눈다 — 응시용·채점용 이미지 tar 와 평가 키트 (이슈 #85 P6, 목 판).

2026-11-12: 응시용·채점용은 적재(import_mock_builds)가 빌드 저장소 묶음(`<images_root>/<build_id>/<역할>/`)에 스키마로 검증해 둔 것을
그대로 복사하고, 평가 키트는 `EvalKit` 모델로 만들어 검증한 뒤 쓴다 — 예전에는 세 파일을 dict 로 다시 만들어 build_id·grader_sha256
이 빠지고 숨김 수용 기준에 스키마 밖 칸(items·faults)이 들어가 평가 쪽이 스키마로 읽으면 거부됐다. 묶음이 없으면 먼저 적재한다.

build_mock_images 의 통합 묶음(images.tar 에 응시용·채점용 12개, manifest.json 에 trap_id·intended_reason, samples/ 에 단언
문구)을 그대로 보내면 응시 쪽에 채점 정보가 넘어간다. 이 스크립트는 로컬 podman 저장소의 이미지를 다시 빌드하지 않고
역할마다 따로 저장(zstd -3)하고 json·SHA256SUMS 를 붙인다 — image_id 가 바뀌지 않는다.

  <out>/<common_id>-r<판>/
    candidate/  <id>-candidate.tar.zst · README.md · candidate.json · SHA256SUMS   → 응시 환경으로만
    grader/     <id>-grader.tar.zst · grader.json · SHA256SUMS         → 채점 서버로만
    eval_kit/   kit.json · SHA256SUMS                                  → 평가 쪽 (채점과 같은 권한)

넣지 않는 것: 참조 구현(ref_*), samples/, 통합 manifest·images.tar, 명세의 지원자 파생 칸(slots·claims_to_verify).
저장 뒤 tar.zst 를 다시 load 해 image_id 가 같은지 확인한다. README.md 는 응시용 이미지의 작업 경로에서 꺼낸 그대로 —
평가 서버·응시 화면이 이미지를 열지 않고 보여 줄 수 있게. 출력은 git 밖(data/).
묶음 복사·평가 키트 코드는 `task_spec.starter.artifacts`·`task_spec.starter.kit` (#126 — builder·승인 뒤 키트 등록과 같은 코드).

사용:
  uv run python -m eval.starter.split_delivery --manifest data/mock_images/2026-11-10/manifest.json \\
      --runs data/mock_images/runs --task ct-0683f8a1b8d6 --revision 3 --out data/mock_images/delivery/2026-11-10
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.starter.import_mock_builds import build_flags_of
from task_spec.schemas.domain import TaskItem
from task_spec.schemas.package import CandidateDescriptor, GraderDescriptor
from task_spec.starter.artifacts import copy_role, summaries, write_role
from task_spec.starter.kit import build_kit
from task_spec.starter.material_guide import load_guides, reason_descriptions
from task_spec.starter.templates import load_template

ROOT = Path(__file__).resolve().parents[2]


def find_build(images_root: Path, common_id: str, tree: str, grader: str) -> Path:
    """적재한 빌드 묶음 — 같은 시작 코드(tree_sha256)·같은 채점 내용(grader_sha256)의 `<common_id>-b<n>` 폴더 (마지막 것)."""
    found: list[Path] = []
    for d in sorted(images_root.glob(f"{common_id}-b*")):
        cj, gj = d / "candidate" / "candidate.json", d / "grader" / "grader.json"
        if not (cj.is_file() and gj.is_file()):
            continue
        c, g = json.loads(cj.read_text("utf-8")), json.loads(gj.read_text("utf-8"))
        if c.get("tree_sha256") == tree and g.get("grader_sha256") == grader:
            found.append(d)
    if not found:
        raise SystemExit(
            f"{common_id}: 이 이미지(tree {tree[:12]}·grader {grader[:12]})로 적재한 빌드 묶음이 {images_root} 에 없다 — "
            "먼저 import_mock_builds 로 적재한다"
        )
    return found[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument(
        "--runs",
        required=True,
        help="build_mock_images 에 넘긴 빌드 폴더들의 상위 (task.json·report.json)",
    )
    ap.add_argument("--task", required=True, help="common_id")
    ap.add_argument("--revision", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--images-root", default="data/images", help="적재한 빌드 묶음 (Settings.images_root)"
    )
    ap.add_argument(
        "--builds", default="data/eval/build", help="알파 결과 상위 (손 예시 루프의 원래 report)"
    )
    a = ap.parse_args()

    m = json.loads(Path(a.manifest).read_text("utf-8"))
    t = next(x for x in m["tasks"] if x["common_id"] == a.task and x["revision"] == a.revision)
    run = Path(a.runs) / t["source_build"]
    task = json.loads((run / "task.json").read_text("utf-8"))
    src = find_build(
        Path(a.images_root),
        a.task,
        t["candidate"]["labels"]["tree_sha256"],
        t["grader"]["grader_sha256"],
    )
    cand = CandidateDescriptor.model_validate_json(
        (src / "candidate" / "candidate.json").read_text("utf-8")
    )
    grad = GraderDescriptor.model_validate_json((src / "grader" / "grader.json").read_text("utf-8"))
    tid, _, ver = t["template_ref"].rpartition("@v")
    # 템플릿 사유 위에 재료 숨은 안내의 사유 (#256 — 빌드·finalize 와 같은 설명)
    guide = load_guides(ROOT / "incidents" / "public_v1").get(task.get("source_incident") or "")
    reasons = reason_descriptions(load_template(tid, int(ver), ROOT / "templates"), guide)
    # 키트를 먼저 만들어 검증 — 맞지 않으면 아무것도 쓰지 않고 멈춘다 (반쪽 묶음 방지)
    kit = build_kit(
        TaskItem.model_validate(task),
        cand,
        grad,
        t["grader"]["cases"],
        summaries(t["verification"]),
        reasons,
        build_flags_of(run, Path(a.builds)),
    )

    base = Path(a.out) / f"{a.task}-r{a.revision}"
    copy_role(src / "candidate", base / "candidate")
    copy_role(src / "grader", base / "grader")
    (base / "eval_kit").mkdir(parents=True, exist_ok=True)
    write_role(
        base / "eval_kit",
        "kit.json",
        json.dumps(kit.model_dump(mode="json"), ensure_ascii=False, indent=2),
    )
    print(f"→ {base}  (빌드 {cand.build_id})")
    for role in ("candidate", "grader", "eval_kit"):
        for p in sorted((base / role).iterdir()):
            print(f"   {role}/{p.name}  {p.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
