"""목 이미지(build_mock_images 결과)를 빌드 저장소에 적재한다 — `common_task_build`·`starter_file`·`build_artifact` (이슈 #85).

과제(manifest 의 common_id·판)마다:
  1. 빌드 행을 만들고 pending → generating(오버레이 파일·생성 결과) → verifying → review 로 올린다.
     승인은 그래프 interrupt 로만 받으므로(불변식 9) 여기서는 review 까지 — 평가 키트(승인 뒤 등록)와 전달 기록은 없다.
  2. 이미지를 `localhost/task/<build_id>-<역할>` 로 이름만 붙이고(image_id 그대로) 역할마다 저장(zstd -3)해
     `<images_root>/<build_id>/<역할>/` 에 descriptor(json, schemas/package.py 로 검증)·SHA256SUMS 와 함께 둔다.
  3. 묶음 행을 등록한다 — tree_sha256·grader_sha256 이 빌드 결과와 같아야 저장소가 받는다.
  지원자 README 는 응시용 이미지의 작업 경로에서 꺼내(지원자가 받는 문장 그대로) `starter_file`(kind=readme)과
  응시용 폴더 README.md 에 같은 본문으로 둔다 — 승인에는 저장한 README 가 있어야 한다.

같은 공통 과제에 같은 시작 코드(tree_sha256)의 빌드가 이미 묶음까지 있으면 건너뛴다. review 에서 묶음이 없으면 묶음만 다시.
공통 과제 행(FK)이 먼저 있어야 한다 — 평가용 명세는 register_mock_common 으로 등록한 뒤 다시 만든 이미지를 넣는다.
기본은 시험 DB(TEST_DATABASE_URL), 운영 DB 는 `--db main` 으로만. 묶음은 `Settings.images_root` 아래 (git 밖).

  uv run python -m eval.starter.import_mock_builds --manifest data/mock_images/2026-11-10/manifest.json \\
      --runs data/mock_images/runs --task ct-0683f8a1b8d6:3 [--db main]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg

from eval.starter.build_mock_images import grader_sha256, load_run, template_of
from eval.starter.register_mock_common import dsn
from eval.starter.split_delivery import extract_readme, save_image, sha256
from task_spec.config import Settings
from task_spec.schemas.build import RepairRecord
from task_spec.schemas.build_record import (
    README_PATH,
    BuildArtifact,
    BuildOutcome,
    BuildRecord,
    StarterFile,
)
from task_spec.schemas.package import (
    ArtifactFile,
    ArtifactRole,
    CandidateDescriptor,
    GraderDescriptor,
    ImageArchive,
    ResultContract,
    VerifySummary,
)
from task_spec.starter.store import PgBuildStore, archive_name, role_dir, sha256sums

MOCK_NOTE = "승인되지 않은 평가용 명세로 만든 목 판"
RESULT_FIELDS = ["common_id", "revision", "setup", "seconds", "cases", "summary", "report_path"]
CASE_FIELDS = ["id", "trap_id", "kind", "passed", "reason", "intended_reason", "message"]


def overlay_files(build_id: str, item: dict[str, Any]) -> list[StarterFile]:
    """빌드 폴더의 오버레이 → `starter_file` 행 (참조 구현은 변형 라벨로 나눔)."""
    out: list[StarterFile] = []
    overlays = [item["starter"], item["hidden"], item["public"], *item["refs"].values()]
    for ov in overlays:
        if ov is None:
            continue
        for f in ov.files:
            out.append(StarterFile.of(build_id, ov.kind, f.path, f.content, ov.label))
    return out


def repairs_of(run: Path, builds_root: Path) -> list[RepairRecord]:
    """수리 기록 — 빌드 폴더 report(run_alpha 는 `build.repairs`)에 없으면 원래 알파 폴더(report `source`)의 성공한 루프 report 에서."""
    report = json.loads((run / "report.json").read_text("utf-8"))
    raw = report.get("repairs")
    if raw is None and isinstance(
        report.get("build"), dict
    ):  # 2026-11-12: run_alpha 보고서의 수리 기록이 적재되지 않았다
        raw = report["build"].get("repairs")
    if raw is None and report.get("source"):
        for loop in sorted((builds_root / report["source"]).glob("loop-*/report.json")):
            r = json.loads(loop.read_text("utf-8"))
            if r.get("status") == "review":
                raw = r.get("repairs")
    return [RepairRecord.model_validate({**x, "reason": str(x["reason"])[:300]}) for x in raw or []]


def build_flags_of(run: Path, builds_root: Path) -> list[str]:
    """빌드 단계의 검토 표시 — 변별 못 해 뺀 케이스·근거 확인·재생성 예산 등 (run_alpha 는 `build.review_flags`, 손 예시 루프는
    원래 알파 폴더의 성공한 루프). 2026-11-12: 적재가 명세의 표시만 넣어 승인자가 빌드 단계의 판단 근거를 못 봤다."""
    report = json.loads((run / "report.json").read_text("utf-8"))
    raw = report["build"].get("review_flags") if isinstance(report.get("build"), dict) else None
    if raw is None and report.get("source"):
        for loop in sorted((builds_root / report["source"]).glob("loop-*/report.json")):
            r = json.loads(loop.read_text("utf-8"))
            if r.get("status") == "review":
                raw = r.get("review_flags")
    return [str(f) for f in raw or []]


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
    return {
        name: VerifySummary.model_validate({**v["summary"], "cases": case_results(v.get("cases"))})
        for name, v in verification.items()
        if v.get("summary")
    }


def write_role(d: Path, descriptor: str, body: str) -> list[ArtifactFile]:
    """descriptor 를 쓰고 SHA256SUMS(자기 자신 제외)를 만든다. 권한은 소유자만 (split_delivery 와 같게)."""
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


def pack(
    root: Path, build: BuildRecord, role: ArtifactRole, image: dict[str, Any]
) -> tuple[Path, ImageArchive]:
    """이미지에 빌드 이름을 붙여 역할 폴더에 저장. image_id 는 바뀌지 않는다 (save_image 가 다시 load 해 확인)."""
    d = role_dir(root, build.build_id, role)
    if d.exists():
        shutil.rmtree(d)  # 묶음 행이 없을 때만 여기 온다 — 지난 실패의 반쯤 쓴 폴더
    d.mkdir(parents=True)
    name = f"localhost/task/{build.build_id}-{role}"
    subprocess.run(["podman", "tag", image["name"], name], check=True)
    info = save_image(name, image["image_id"], d / archive_name(build.build_id, role))
    return d, ImageArchive.model_validate(info)


async def common_revision(conninfo: str, common_id: str) -> int:
    async with await psycopg.AsyncConnection.connect(conninfo) as conn:
        cur = await conn.execute(
            "SELECT revision FROM public.common_task WHERE id = %s", (common_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise SystemExit(f"{common_id}: 공통 과제 행이 없다 — register_mock_common 으로 먼저 등록")
    return int(row[0])


async def import_one(
    store: PgBuildStore,
    conninfo: str,
    root: Path,
    t: dict[str, Any],
    run: Path,
    context: Path,
    builds_root: Path,
) -> str:
    cid = t["common_id"]
    tmpl = template_of(run)
    item = load_run(run, tmpl)
    labels = t["candidate"]["labels"]
    tree = labels["tree_sha256"]
    grader_hash = t["grader"].get("grader_sha256") or grader_sha256(
        context / f"{cid}-r{t['revision']}" / "grader"
    )
    revision = await common_revision(conninfo, cid)
    c = t["candidate"]
    readme = extract_readme(c["image_id"], c["workspace"])

    build = next(
        (
            b
            for b in await store.builds(cid)
            if b.outcome is not None and b.outcome.tree_sha256 == tree and b.status != "failed"
        ),
        None,
    )
    if build is not None and {a.role for a in await store.artifacts(build.build_id)} >= {
        "candidate",
        "grader",
    }:
        return f"{cid} r{t['revision']}: 이미 {build.build_id} — 건너뜀"
    if build is None:
        build = await store.new_build(cid, labels["ontology_version"])
        await store.set_status(build.build_id, "generating")
        await store.put_files(
            build.build_id,
            [
                *overlay_files(build.build_id, item),
                StarterFile.of(build.build_id, "readme", README_PATH, readme),
            ],
        )
        await store.save_outcome(
            build.build_id,
            BuildOutcome(
                template_ref=t["template_ref"],
                tree_sha256=tree,
                grader_sha256=grader_hash,
                contract=item["contract"],
                hidden_plan=item["plan"],
                verification=summaries(t["verification"]),
                review_flags=merged_flags(
                    item["task"].review_flags, build_flags_of(run, builds_root)
                ),
                repairs=repairs_of(run, builds_root),
            ),
        )
        await store.set_status(build.build_id, "verifying")
        build = await store.set_status(build.build_id, "review")
    elif build.status != "review":
        raise SystemExit(f"{build.build_id}: {build.status} — review 빌드에만 묶음을 붙인다")
    else:  # README 를 저장하기 전에 적재한 빌드 — 이미 있으면 같은 본문이어야 한다
        await store.put_readme(StarterFile.of(build.build_id, "readme", README_PATH, readme))

    header = {
        "mock": True,
        "approval": "none",
        "note": MOCK_NOTE,
        "common_id": cid,
        "revision": revision,
        "build_id": build.build_id,
        "template_ref": t["template_ref"],
        "ontology_version": build.ontology_version,
        "tree_sha256": tree,
        "title": t.get("title"),
    }
    cdir, cimg = pack(root, build, "candidate", c)
    (cdir / README_PATH).write_text(
        readme, "utf-8"
    )  # 이미지를 열지 않고 보여 줄 수 있게 — SHA256SUMS 에 들어간다
    cand = CandidateDescriptor(
        **header,
        image=cimg,
        load=f"podman load -i {cimg.file}",
        workspace=c["workspace"],
        run=f"podman run -it --name <응시자> {cimg.name} bash",
        commands={k: v for k, v in (c.get("commands") or {}).items() if k != "hidden_test"},
    )
    g = t["grader"]
    gdir, gimg = pack(root, build, "grader", g)
    grade = f"podman run --rm --network none -v <제출물>:/submission:ro,z -v <결과>:/out:z {gimg.name} grade"
    starter = summaries(t["verification"]).get("starter-only")
    if starter is None:
        raise SystemExit(f"{cid}: 시작 코드만 채점한 결과(starter-only)가 없다")
    grader = GraderDescriptor(
        **header,
        image=gimg,
        load=f"podman load -i {gimg.file}",
        grader_sha256=grader_hash,
        grade=grade,
        grade_without_selinux_relabel=grade.replace(":ro,z", ":ro")
        .replace(":/out:z", ":/out")
        .replace("--network none", "--network none --security-opt label=disable"),
        timeout_s=g["timeout_s"],
        result=ResultContract(fields=RESULT_FIELDS, case_fields=CASE_FIELDS),
        case_ids=[x["id"] for x in g["cases"]],
        starter_expected=starter,
    )
    for role, d, desc, img in (
        ("candidate", cdir, cand, cimg),
        ("grader", gdir, grader, gimg),
    ):
        files = write_role(d, f"{role}.json", desc.model_dump_json(indent=2))
        await store.put_artifact(
            BuildArtifact(
                build_id=build.build_id,
                role=role,  # type: ignore[arg-type]
                files=files,
                image_id=img.image_id,
                tree_sha256=tree,
                grader_sha256=grader_hash,
                created_at=build.created_at,
            )
        )
    return f"{cid} r{t['revision']}: → {build.build_id} (review, 묶음 {root / build.build_id})"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--runs", required=True, help="이미지를 만든 빌드 폴더들의 상위")
    ap.add_argument(
        "--task", action="append", default=[], help="<common_id>:<판> — 없으면 manifest 전부"
    )
    ap.add_argument("--db", choices=["test", "main"], default="test")
    ap.add_argument(
        "--builds", default="data/eval/build", help="알파 결과 상위 (수리 기록을 읽을 곳)"
    )
    a = ap.parse_args()
    s = Settings()  # type: ignore[call-arg]
    manifest = Path(a.manifest)
    tasks = json.loads(manifest.read_text("utf-8"))["tasks"]
    want = {(x.split(":")[0], int(x.split(":")[1])) for x in a.task}
    picked = [t for t in tasks if not want or (t["common_id"], t["revision"]) in want]
    if want and len(picked) != len(want):
        raise SystemExit(
            f"manifest 에 없는 과제: {want - {(t['common_id'], t['revision']) for t in picked}}"
        )
    # build_mock_images 는 <mock_images>/<날짜>/manifest.json 옆 <mock_images>/build-context/<날짜>/ 에 작업 폴더를 둔다
    manifest = manifest.resolve()
    context = manifest.parent.parent / "build-context" / manifest.parent.name
    conninfo = dsn(s, a.db)
    store = await PgBuildStore.open(conninfo)
    try:
        for t in picked:
            print(
                await import_one(
                    store,
                    conninfo,
                    s.images_root,
                    t,
                    Path(a.runs) / t["source_build"],
                    context,
                    Path(a.builds),
                ),
                flush=True,
            )
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
