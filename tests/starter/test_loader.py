"""묶음 등록 (`starter.loader.register_packages`) — builder 의 승인 전 묶음(approval=pending)·eval 의 목 판. podman 없이 (pack 을 가짜로)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from task_spec.schemas.build_record import README_PATH, BuildRecord, StarterFile
from task_spec.schemas.package import (
    CandidateDescriptor,
    GraderDescriptor,
    ImageArchive,
    PackageHeader,
    VerifySummary,
)
from task_spec.starter import loader
from task_spec.starter.store import InMemoryBuildStore, role_dir
from tests.starter.test_build_store import AT, H_OLD, _outcome

CID = "ct-0123456789ab"
IMG = "sha256:" + "d" * 64


def _fake_pack(
    root: Path, build: BuildRecord, role: str, image: dict[str, Any]
) -> tuple[Path, ImageArchive]:
    d = role_dir(root, build.build_id, role)  # type: ignore[arg-type]
    d.mkdir(parents=True)
    tar = d / f"{build.build_id}-{role}.tar.zst"
    tar.write_bytes("가상 이미지".encode())
    return d, ImageArchive(
        file=tar.name,
        sha256=hashlib.sha256(tar.read_bytes()).hexdigest(),
        size_bytes=tar.stat().st_size,
        image_id=image["image_id"],
        name=f"localhost/task/{build.build_id}-{role}",
    )


async def _review_build(store: InMemoryBuildStore) -> BuildRecord:
    b = await store.new_build(CID, "onto@abc+dirty", AT)
    await store.set_status(b.build_id, "generating")
    await store.put_readme(StarterFile.of(b.build_id, "readme", README_PATH, "# 가상 README"))
    await store.save_outcome(b.build_id, _outcome(H_OLD))
    await store.set_status(b.build_id, "verifying")
    return await store.set_status(b.build_id, "review")


def _inputs(
    build: BuildRecord, approval: str = "pending", mock: bool = False
) -> loader.PackageInputs:
    return loader.PackageInputs(
        header=PackageHeader(
            mock=mock,
            approval=approval,  # type: ignore[arg-type]
            common_id=CID,
            revision=2,
            build_id=build.build_id,
            template_ref="data/python-duckdb@v1",
            ontology_version=build.ontology_version,
            tree_sha256=H_OLD,
            title="가상 과제",
        ),
        candidate=loader.ImageRef("localhost/task/가상-candidate", IMG),
        grader=loader.ImageRef("localhost/task/가상-grader", IMG),
        readme="# 가상 README",
        workspace="/workspace",
        commands={"test": "python -m pytest -q", "hidden_test": "숨은 테스트 명령"},
        grader_sha256=H_OLD,
        timeout_s=1200,
        case_ids=["hc1", "pt1"],
        starter_expected=VerifySummary(
            hidden_passed=0, hidden_total=1, public_passed=0, public_total=1
        ),
    )


async def test_pending_packages_are_registered_with_descriptors_and_readme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "pack", _fake_pack)
    store = InMemoryBuildStore()
    b = await _review_build(store)
    arts = await loader.register_packages(store, tmp_path, b, _inputs(b))
    assert [a.role for a in arts] == ["candidate", "grader"]
    assert [a.role for a in await store.artifacts(b.build_id)] == ["candidate", "grader"]
    cdir = role_dir(tmp_path, b.build_id, "candidate")
    cand = CandidateDescriptor.model_validate_json((cdir / "candidate.json").read_text("utf-8"))
    assert (cand.approval, cand.mock) == ("pending", False)  # 승인 전 실제 빌드 — 전달은 승인 뒤
    assert "hidden_test" not in cand.commands  # 숨은 테스트 명령은 응시 쪽에 주지 않는다
    assert (cdir / README_PATH).read_text("utf-8") == "# 가상 README"
    sums = (cdir / "SHA256SUMS").read_text("utf-8")
    assert README_PATH in sums and "candidate.json" in sums
    grader = GraderDescriptor.model_validate_json(
        (role_dir(tmp_path, b.build_id, "grader") / "grader.json").read_text("utf-8")
    )
    assert grader.case_ids == ["hc1", "pt1"] and "--network none" in grader.grade
    assert json.loads((cdir / "candidate.json").read_text("utf-8"))["build_id"] == b.build_id


async def test_packages_need_a_review_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from task_spec.starter.store import BuildRuleError

    monkeypatch.setattr(loader, "pack", _fake_pack)
    store = InMemoryBuildStore()
    b = await store.new_build(CID, "onto-test", AT)
    await store.set_status(b.build_id, "generating")
    await store.save_outcome(b.build_id, _outcome(H_OLD))
    with pytest.raises(BuildRuleError, match="review"):
        await loader.register_packages(store, tmp_path, b, _inputs(b))
