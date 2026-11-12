"""빌드 묶음 — 이미지를 역할(응시용·채점용)마다 저장(zstd -3)하고 descriptor·SHA256SUMS 를 붙여 빌드 저장소에 둔다 (이슈 #85).

`<images_root>/<build_id>/<역할>/` 에 `<build_id>-<역할>.tar.zst` · `<역할>.json` · SHA256SUMS (권한은 소유자만).
저장 뒤 tar.zst 를 다시 load 해 image_id 가 같은지 확인한다 — 이름만 붙이고 다시 빌드하지 않으므로 image_id 가 바뀌지 않는다.
지원자 README 는 응시용 이미지의 작업 경로에서 꺼낸다 (지원자가 받는 문장 그대로).

2026-11-12 eval/starter 의 import_mock_builds·split_delivery 에서 옮김 (#126) — 알파 적재와 builder(#129)가 같은 코드.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from task_spec.schemas.build_record import BuildRecord, StarterFile
from task_spec.schemas.package import ArtifactFile, ArtifactRole, ImageArchive, VerifySummary
from task_spec.starter.store import archive_name, role_dir, sha256sums

if TYPE_CHECKING:  # 타입만 — 묶음·평가 키트 쪽이 이미지 만들기·검증기를 불러오지 않게
    from task_spec.starter.images import TaskMaterial

# 채점 결과 파일(result.json)의 칸 — 채점용 descriptor 의 `result` 계약
RESULT_FIELDS = ["common_id", "revision", "setup", "seconds", "cases", "summary", "report_path"]
CASE_FIELDS = ["id", "trap_id", "kind", "passed", "reason", "intended_reason", "message"]


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


def write_role(d: Path, descriptor: str, body: str) -> list[ArtifactFile]:
    """descriptor(또는 kit.json)를 쓰고 SHA256SUMS(자기 자신 제외)를 만든다. 권한은 소유자만."""
    (d / descriptor).write_text(body, "utf-8")
    files = [
        ArtifactFile(name=p.name, sha256=sha256(p), size_bytes=p.stat().st_size)
        for p in sorted(d.iterdir())
        if p.is_file() and p.name != "SHA256SUMS"
    ]
    (d / "SHA256SUMS").write_text(sha256sums(files), "utf-8")
    for p in d.iterdir():
        os.chmod(p, 0o600)
    os.chmod(d, 0o700)
    return files


def copy_role(src: Path, dst: Path) -> None:
    """저장소 묶음의 역할 폴더를 복사하고 SHA256SUMS 로 복사본을 확인한다."""
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
            raise RuntimeError(f"{dst / name}: SHA256SUMS 와 다르다")


def pack(
    root: Path, build: BuildRecord, role: ArtifactRole, image: dict[str, Any]
) -> tuple[Path, ImageArchive]:
    """이미지에 빌드 이름을 붙여 역할 폴더에 저장. image_id 는 바뀌지 않는다 (save_image 가 다시 load 해 확인).
    image: `{"name": 지금 이미지 이름, "image_id": …}`."""
    d = role_dir(root, build.build_id, role)
    if d.exists():
        shutil.rmtree(d)  # 묶음 행이 없을 때만 여기 온다 — 지난 실패의 반쯤 쓴 폴더
    d.mkdir(parents=True)
    name = f"localhost/task/{build.build_id}-{role}"
    subprocess.run(["podman", "tag", image["name"], name], check=True)
    info = save_image(name, image["image_id"], d / archive_name(build.build_id, role))
    return d, ImageArchive.model_validate(info)


def overlay_files(build_id: str, item: TaskMaterial) -> list[StarterFile]:
    """재료의 오버레이 → `starter_file` 행 (참조 구현은 변형 라벨로 나눔)."""
    out: list[StarterFile] = []
    for ov in [item.starter, item.hidden, item.public, *item.refs.values()]:
        if ov is None:
            continue
        for f in ov.files:
            out.append(StarterFile.of(build_id, ov.kind, f.path, f.content, ov.label))
    return out


def merged_flags(task_flags: Sequence[str], build_flags: Sequence[str]) -> list[str]:
    """명세의 표시 + 빌드 단계의 표시 (같은 글은 한 번)."""
    return list(dict.fromkeys([*task_flags, *build_flags]))


def case_results(cases: Any) -> list[dict[str, Any]]:
    """result.json cases[] → CaseResult 모양 (id·kind·passed·reason·message 앞 300자)."""
    out: list[dict[str, Any]] = []
    for c in cases or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        msg = c.get("message")
        out.append(
            {
                "id": str(c["id"])[:120],
                "kind": str(c.get("kind") or "hidden")[:20],
                "passed": bool(c.get("passed")),
                "reason": (str(c["reason"])[:80] if c.get("reason") else None),
                "message": (" ".join(str(msg).split())[:300] if msg else None),
            }
        )
    return out


def summaries(verification: dict[str, Any]) -> dict[str, VerifySummary]:
    """manifest `verification`(제출물 이름 → summary·cases) → 제출물마다 VerifySummary (케이스별 결과 포함)."""
    return {
        name: VerifySummary.model_validate({**v["summary"], "cases": case_results(v.get("cases"))})
        for name, v in verification.items()
        if v.get("summary")
    }
