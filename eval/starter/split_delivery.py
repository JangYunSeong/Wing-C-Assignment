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

사용:
  uv run python -m eval.starter.split_delivery --manifest data/mock_images/2026-11-10/manifest.json \\
      --runs data/mock_images/runs --task ct-0683f8a1b8d6 --revision 3 --out data/mock_images/delivery/2026-11-10
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import yaml

from task_spec.schemas.domain import TaskItem
from task_spec.schemas.package import (
    CandidateDescriptor,
    EvalKit,
    GraderDescriptor,
    KitAcceptance,
    KitCase,
    KitImages,
)
from task_spec.starter.kit import kit_stages

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save_image(name: str, image_id: str, dst: Path) -> dict[str, Any]:
    """podman save(docker-archive) → zstd -3 → 다시 load 해 image_id 확인. 쓰는 동안 .part 이름."""
    raw = dst.with_suffix("")  # x.tar
    part = dst.with_name(dst.name + ".part")
    subprocess.run(
        ["podman", "save", "--format", "docker-archive", "-o", str(raw), f"{name}:latest"],
        check=True,
    )
    subprocess.run(["zstd", "-3", "-T0", "-q", "-f", str(raw), "-o", str(part)], check=True)
    raw.unlink()
    loaded = subprocess.run(
        ["podman", "load", "-i", str(part)], check=True, capture_output=True, text=True
    )
    got = subprocess.run(
        ["podman", "image", "inspect", "--format", "{{.Id}}", f"{name}:latest"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not image_id.endswith(got):
        raise RuntimeError(
            f"{name}: 다시 load 한 image_id {got} 가 manifest {image_id} 와 다름 ({loaded.stdout.strip()})"
        )
    part.rename(dst)
    os.chmod(dst, 0o600)
    return {
        "file": dst.name,
        "sha256": sha256(dst),
        "size_bytes": dst.stat().st_size,
        "image_id": image_id,
        "name": name,
    }


def extract_readme(image: str, workspace: str) -> str:
    """응시용 이미지의 `<workspace>/README.md` — 지원자가 실제로 받는 문장 (컨테이너는 만들기만 하고 실행하지 않는다)."""
    ctr = subprocess.run(
        ["podman", "create", image], check=True, capture_output=True, text=True
    ).stdout.strip()
    try:
        out = subprocess.run(
            ["podman", "cp", f"{ctr}:{workspace}/README.md", "-"],  # 표준 출력은 tar 묶음
            check=True,
            capture_output=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(out)) as tar:
            member = tar.extractfile("README.md")
            if member is None:
                raise RuntimeError(f"{image}: {workspace}/README.md 가 파일이 아니다")
            return member.read().decode("utf-8")
    finally:
        subprocess.run(["podman", "rm", ctr], check=True, capture_output=True)


def write_role(d: Path, info: dict[str, Any], json_name: str) -> None:
    (d / json_name).write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
    files = sorted(p for p in d.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (d / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files), "utf-8")
    for p in d.iterdir():
        os.chmod(p, 0o600)
    os.chmod(d, 0o700)


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


def copy_role(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted(src.iterdir()):
        if p.is_file():
            shutil.copy2(p, dst / p.name)
            os.chmod(dst / p.name, 0o600)
    os.chmod(dst, 0o700)
    lines = (dst / "SHA256SUMS").read_text("utf-8").splitlines()
    for line in lines:  # 복사본이 저장소 묶음과 같은지
        digest, name = line.split(maxsplit=1)
        if sha256(dst / name.strip()) != digest:
            raise SystemExit(f"{dst / name}: SHA256SUMS 와 다르다")


def build_kit(
    t: dict[str, Any],
    task: dict[str, Any],
    cand: CandidateDescriptor,
    grad: GraderDescriptor,
    reasons: dict[str, str],
    build_flags: list[str],
) -> EvalKit:
    """평가 키트 — 스키마 모델로 만들어 검증한다 (빠진 칸·스키마 밖 칸이 있으면 쓰기 전에 멈춤)."""
    # import_mock_builds 가 이 모듈(save_image 등)을 import 하므로 여기서
    from eval.starter.import_mock_builds import merged_flags, summaries

    item = TaskItem.model_validate(task)
    stage1, stage2 = kit_stages(item)
    traps = {x.trap_id: x for x in item.traps}
    header = cand.model_dump(
        include={
            "mock",
            "approval",
            "note",
            "common_id",
            "revision",
            "build_id",
            "template_ref",
            "template_sha256",
            "ontology_version",
            "tree_sha256",
            "platform",
            "title",
        }
    )
    hidden = [x for x in t["grader"]["cases"] if x.get("kind") == "hidden"]
    return EvalKit(
        **header,
        images=KitImages(candidate=cand.image.image_id, grader=grad.image.image_id),
        grader_sha256=grad.grader_sha256,
        cases=[
            KitCase(
                id=x["id"],
                trap_id=x.get("trap_id") or "",
                intended_reason=x.get("intended_reason") or "",
                reason_description=reasons.get(x.get("intended_reason") or "", ""),
                hidden_requirement=(
                    traps[x["trap_id"]].description if x.get("trap_id") in traps else ""
                ),
            )
            for x in hidden
        ],
        # 숨김 수용 기준은 스키마 칸만 — 실패 지정(items·faults)은 채점용 이미지 안에서 처리된다
        acceptance_hidden=[
            KitAcceptance(ac_id=a.ac_id, trap_id=a.trap_id, text=a.text)
            for a in item.acceptance_hidden
        ],
        accepted_designs=list(item.accepted_designs),
        behavioral_anchors=item.behavioral_anchors,
        follow_up_questions=list(item.follow_up_questions),
        stage1=stage1,
        stage2=stage2,
        target_competencies=list(item.target_competencies),
        verification=summaries(t["verification"]),
        verification_note=(
            "starter-only = 시작 코드 그대로, naive-all = 흔한 구현, d* = 허용 설계별 참조 구현 (참조 구현 자체는 넣지 않음)"
        ),
        review_flags=merged_flags(item.review_flags, build_flags),
    )


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
    template_dir = ROOT / "templates" / t["template_ref"].replace("@", "/")
    reasons = {
        r["code"]: r["description"]
        for r in yaml.safe_load((template_dir / "manifest.yaml").read_text("utf-8"))[
            "failure_reasons"
        ]
    }
    from eval.starter.import_mock_builds import build_flags_of

    # 키트를 먼저 만들어 검증 — 맞지 않으면 아무것도 쓰지 않고 멈춘다 (반쪽 묶음 방지)
    kit = build_kit(t, task, cand, grad, reasons, build_flags_of(run, Path(a.builds)))

    base = Path(a.out) / f"{a.task}-r{a.revision}"
    copy_role(src / "candidate", base / "candidate")
    copy_role(src / "grader", base / "grader")
    (base / "eval_kit").mkdir(parents=True, exist_ok=True)
    write_role(base / "eval_kit", kit.model_dump(mode="json"), "kit.json")
    print(f"→ {base}  (빌드 {cand.build_id})")
    for role in ("candidate", "grader", "eval_kit"):
        for p in sorted((base / role).iterdir()):
            print(f"   {role}/{p.name}  {p.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
