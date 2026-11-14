"""묶음 등록 — 응시용·채점용 이미지를 빌드 이름으로 저장하고 descriptor·SHA256SUMS 를 붙여 빌드 저장소에 등록한다 (#85).

eval 적재(`eval/starter/import_mock_builds`, 목 판 approval=none)와 builder(#129, 승인 전 approval=pending)가 같은 코드를 쓴다.
빌드는 review 상태여야 한다 (저장소 규칙 — 묶음의 tree·grader 해시가 빌드 결과와 같아야 받는다).
지원자 README 는 응시용 폴더에도 같은 본문으로 둔다 — 이미지를 열지 않고 보여 줄 수 있게 (SHA256SUMS 에 들어간다).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from task_spec.schemas.build_record import README_PATH, BuildArtifact, BuildRecord
from task_spec.schemas.package import (
    CandidateDescriptor,
    GraderDescriptor,
    PackageHeader,
    ResultContract,
    VerifySummary,
)
from task_spec.starter.artifacts import CASE_FIELDS, RESULT_FIELDS, pack, write_role
from task_spec.starter.mounts import grade_argv
from task_spec.starter.store import BuildRepo


@dataclass(frozen=True)
class ImageRef:
    """로컬 podman 저장소의 이미지 — 빌드 이름을 붙여 저장한다 (image_id 는 바뀌지 않는다)."""

    name: str
    image_id: str  # sha256:…


@dataclass(frozen=True)
class PackageInputs:
    header: PackageHeader  # 두 descriptor 공통 머리 (mock·approval·판·해시·제목)
    candidate: ImageRef
    grader: ImageRef
    readme: str  # 응시용 이미지 작업 경로의 README.md 그대로
    workspace: str  # 응시 작업 경로 (예: /workspace)
    commands: dict[str, str]  # 템플릿 commands — hidden_test 는 응시 쪽에 주지 않는다
    grader_sha256: str
    timeout_s: int
    case_ids: list[str]  # 채점 설정의 케이스 id (숨은·공개)
    starter_expected: VerifySummary  # 시작 코드만 채점한 결과 — 받는 쪽 호환성 시험


def grade_command(image: str, relabel: bool = True) -> str:
    """전달 묶음의 grade 명령 — 견본 채점과 같은 마운트 정책 (starter/mounts.py). relabel=False 는 라벨을 못 바꾸는 저장소용."""
    return " ".join(["podman", *grade_argv(image, "<제출물>", "<결과>", relabel)])


async def register_packages(
    store: BuildRepo, root: Path, build: BuildRecord, p: PackageInputs
) -> list[BuildArtifact]:
    """응시용·채점용 묶음을 `<root>/<build_id>/<역할>/` 에 쓰고 등록한다. 저장(podman save·zstd)은 스레드에서."""
    head = p.header.model_dump()
    cdir, cimg = await asyncio.to_thread(
        pack, root, build, "candidate", {"name": p.candidate.name, "image_id": p.candidate.image_id}
    )
    (cdir / README_PATH).write_text(p.readme, "utf-8")
    cand = CandidateDescriptor(
        **head,
        image=cimg,
        load=f"podman load -i {cimg.file}",
        workspace=p.workspace,
        run=f"podman run -it --name <응시자> {cimg.name} bash",
        commands={k: v for k, v in p.commands.items() if k != "hidden_test"},
    )
    gdir, gimg = await asyncio.to_thread(
        pack, root, build, "grader", {"name": p.grader.name, "image_id": p.grader.image_id}
    )
    grade = grade_command(gimg.name)
    grader = GraderDescriptor(
        **head,
        image=gimg,
        load=f"podman load -i {gimg.file}",
        grader_sha256=p.grader_sha256,
        grade=grade,
        grade_without_selinux_relabel=grade_command(gimg.name, relabel=False),
        timeout_s=p.timeout_s,
        result=ResultContract(fields=RESULT_FIELDS, case_fields=CASE_FIELDS),
        case_ids=p.case_ids,
        starter_expected=p.starter_expected,
    )
    out: list[BuildArtifact] = []
    for role, d, desc, img in (
        ("candidate", cdir, cand, cimg),
        ("grader", gdir, grader, gimg),
    ):
        files = write_role(d, f"{role}.json", desc.model_dump_json(indent=2))
        artifact = BuildArtifact(
            build_id=build.build_id,
            role=role,  # type: ignore[arg-type]
            files=files,
            image_id=img.image_id,
            tree_sha256=p.header.tree_sha256,
            grader_sha256=p.grader_sha256,
            created_at=build.created_at,
        )
        await store.put_artifact(artifact)
        out.append(artifact)
    return out
