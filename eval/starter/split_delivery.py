"""반입 묶음을 받는 곳별로 나눈다 — 응시용·채점용 이미지 tar 와 평가 키트 (이슈 #85 P6, 목 판).

build_mock_images 의 통합 묶음(images.tar 에 응시용·채점용 12개, manifest.json 에 trap_id·intended_reason, samples/ 에 단언
문구)을 그대로 보내면 응시 쪽에 채점 정보가 넘어간다. 이 스크립트는 로컬 podman 저장소의 이미지를 다시 빌드하지 않고
역할마다 따로 저장(zstd -3)하고 json·SHA256SUMS 를 붙인다 — image_id 가 바뀌지 않는다.

  <out>/<common_id>-r<판>/
    candidate/  <id>-candidate.tar.zst · candidate.json · SHA256SUMS   → 응시 환경으로만
    grader/     <id>-grader.tar.zst · grader.json · SHA256SUMS         → 채점 서버로만
    eval_kit/   kit.json · SHA256SUMS                                  → 평가 쪽 (채점과 같은 권한)

넣지 않는 것: 참조 구현(ref_*), samples/, 통합 manifest·images.tar, 명세의 지원자 파생 칸(slots·claims_to_verify).
저장 뒤 tar.zst 를 다시 load 해 image_id 가 같은지 확인한다. 출력은 git 밖(data/).

사용:
  uv run python -m eval.starter.split_delivery --manifest data/mock_images/2026-11-10/manifest.json \\
      --runs data/mock_images/runs --task ct-0683f8a1b8d6 --revision 3 --out data/mock_images/delivery/2026-11-10
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
MOCK = {"mock": True, "approval": "none", "note": "승인되지 않은 평가용 명세로 만든 목 판"}
PLATFORM = "linux/amd64"


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


def write_role(d: Path, info: dict[str, Any], json_name: str) -> None:
    (d / json_name).write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
    files = sorted(p for p in d.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (d / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files), "utf-8")
    for p in d.iterdir():
        os.chmod(p, 0o600)
    os.chmod(d, 0o700)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument(
        "--runs",
        required=True,
        help="build_mock_images 에 넘긴 빌드 폴더들의 상위 (task.json·contract.json)",
    )
    ap.add_argument("--task", required=True, help="common_id")
    ap.add_argument("--revision", type=int, required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    m = json.loads(Path(a.manifest).read_text("utf-8"))
    t = next(x for x in m["tasks"] if x["common_id"] == a.task and x["revision"] == a.revision)
    run = Path(a.runs) / t["source_build"]
    task = json.loads((run / "task.json").read_text("utf-8"))
    template_dir = ROOT / "templates" / t["template_ref"].replace("@", "/")
    reasons = {
        r["code"]: r["description"]
        for r in yaml.safe_load((template_dir / "manifest.yaml").read_text("utf-8"))[
            "failure_reasons"
        ]
    }
    key = f"{a.task}-r{a.revision}"
    base = Path(a.out) / key
    for role in ("candidate", "grader", "eval_kit"):
        (base / role).mkdir(parents=True, exist_ok=True)
    trace = {
        "common_id": a.task,
        "revision": a.revision,
        "template_ref": t["template_ref"],
        "ontology_version": t["candidate"]["labels"].get("ontology_version"),
        "tree_sha256": t["candidate"]["labels"].get("tree_sha256"),
        "platform": PLATFORM,
    }

    c = t["candidate"]
    cimg = save_image(c["name"], c["image_id"], base / "candidate" / f"{key}-candidate.tar.zst")
    write_role(
        base / "candidate",
        {
            **MOCK,
            **trace,
            "role": "candidate",
            "title": t["title"],
            "image": cimg,
            "workspace": c["workspace"],
            "run": f"podman run -it --name <응시자> {c['name']} bash",
            # 숨은 테스트 실행 명령(hidden_test)은 빼고 응시자가 쓰는 명령만
            "commands": {k: v for k, v in c["commands"].items() if k != "hidden_test"},
            "load": f"podman load -i {cimg['file']}",
        },
        "candidate.json",
    )

    g = t["grader"]
    gimg = save_image(g["name"], g["image_id"], base / "grader" / f"{key}-grader.tar.zst")
    grade = f"podman run --rm --network none -v <제출물>:/submission:ro,z -v <결과>:/out:z {g['name']} grade"
    write_role(
        base / "grader",
        {
            **MOCK,
            **trace,
            "role": "grader",
            "image": gimg,
            "load": f"podman load -i {gimg['file']}",
            "grade": grade,
            "grade_without_selinux_relabel": grade.replace(":ro,z", ":ro")
            .replace(":/out:z", ":/out")
            .replace("--network none", "--network none --security-opt label=disable"),
            "network": "none (필수)",
            "timeout_s": g["timeout_s"],
            "result": {
                "path": "/out/result.json",
                "fields": [
                    "common_id",
                    "revision",
                    "setup",
                    "seconds",
                    "cases",
                    "summary",
                    "report_path",
                ],
                "case_fields": ["id", "trap_id", "kind", "passed", "reason", "intended_reason"],
                "note": "result.json 에 trap_id·intended_reason·실패 메시지가 들어 있다 — 채점·평가 쪽 권한으로만 다룬다",
            },
            "case_ids": [x["id"] for x in g["cases"]],
            "acceptance_check": "시작 코드(응시용 이미지의 /workspace)를 제출물로 grade → setup == 'ok' 이고 숨은 케이스 0/N",
        },
        "grader.json",
    )

    hidden = [x for x in g["cases"] if x.get("kind") == "hidden"]
    traps = {x["trap_id"]: x for x in task.get("traps", [])}
    write_role(
        base / "eval_kit",
        {
            **MOCK,
            **trace,
            "role": "eval_kit",
            "title": t["title"],
            "images": {"candidate": cimg["image_id"], "grader": gimg["image_id"]},
            "cases": [
                {
                    "id": x["id"],
                    "trap_id": x.get("trap_id"),
                    "intended_reason": x.get("intended_reason"),
                    "reason_description": reasons.get(x.get("intended_reason") or ""),
                    "hidden_requirement": (traps.get(x.get("trap_id") or "") or {}).get(
                        "description"
                    ),
                }
                for x in hidden
            ],
            "acceptance_hidden": task.get("acceptance_hidden", []),
            "accepted_designs": task.get("accepted_designs", []),
            "behavioral_anchors": task.get("behavioral_anchors"),
            "follow_up_questions": task.get("follow_up_questions", []),
            "target_competencies": [
                {"competency_id": x["competency_id"], "label": x["label"]}
                for x in task.get("target_competencies", [])
            ],
            "verification": {k: v["summary"] for k, v in t["verification"].items()},
            "verification_note": "starter-only = 시작 코드 그대로, naive-all = 흔한 구현, d* = 허용 설계별 참조 구현 (참조 구현 자체는 넣지 않음)",
            "review_flags": task.get("review_flags", []),
        },
        "kit.json",
    )
    print(f"→ {base}")
    for role in ("candidate", "grader", "eval_kit"):
        for p in sorted((base / role).iterdir()):
            print(f"   {role}/{p.name}  {p.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
