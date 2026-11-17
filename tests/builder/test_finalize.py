"""묶음 확정 (#130 ③ A) — 승인된 빌드의 descriptor 를 approved 로 다시 쓰고 평가 키트를 등록. 실제 파일, podman 없이 (pack 은 가짜)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from task_spec.builder.finalize import PackageFinalizer
from task_spec.schemas.build_record import README_FULL_PATH, README_PATH, BuildRecord, StarterFile
from task_spec.schemas.domain import AcceptedDesign
from task_spec.schemas.package import (
    CandidateDescriptor,
    EvalKit,
    GraderDescriptor,
    ImageArchive,
    PackageHeader,
    ReviewDecision,
    VerifySummary,
)
from task_spec.starter import loader
from task_spec.starter.store import CommonRef, InMemoryBuildStore, role_dir
from tests.starter.test_build_store import FLAG, H_OLD, _outcome
from tests.starter.test_kit import _task

CID = "ct-0123456789ab"
IMG = "sha256:" + "d" * 64
NOW = datetime(2026, 11, 12, tzinfo=UTC)


def _fake_pack(
    root: Path, build: BuildRecord, role: str, image: dict[str, Any]
) -> tuple[Path, ImageArchive]:
    d = role_dir(root, build.build_id, role)  # type: ignore[arg-type]
    d.mkdir(parents=True)
    tar = d / f"{build.build_id}-{role}.tar.zst"
    tar.write_bytes(f"가상 이미지 {role}".encode())
    return d, ImageArchive(
        file=tar.name,
        sha256=hashlib.sha256(tar.read_bytes()).hexdigest(),
        size_bytes=tar.stat().st_size,
        image_id=image["image_id"],
        name=f"localhost/task/{build.build_id}-{role}",
    )


async def _approved_build(
    store: InMemoryBuildStore,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    ontology: str = "onto-v2026.11.01",
    full_readme: str | None = None,
) -> BuildRecord:
    """builder 가 만든 것처럼 — 생성 요청 → review + 승인 전 묶음(pending) → 승인. full_readme = 문제 분석 판의 원래 판 README."""
    monkeypatch.setattr(loader, "pack", _fake_pack)
    store.commons[CID] = CommonRef("approved", "feature", ontology)
    store.tasks[CID] = _task(
        review_flags=[FLAG],
        acceptance_hidden=[{"ac_id": "h1", "trap_id": "t1", "text": "가상 숨김 기준"}],
        accepted_designs=[
            AcceptedDesign(
                design_id="d1", summary="가상 설계", conditions="가상 조건", trap_ids=["t1"]
            )
        ],
    )
    b, _ = await store.request_build(CID, "가상승인자")
    b = await store.claim("w1", 60)  # type: ignore[assignment]
    assert b is not None
    await store.put_readme(StarterFile.of(b.build_id, "readme", README_PATH, "# 가상"))
    if full_readme is not None:
        full = StarterFile.of(b.build_id, "readme_full", README_FULL_PATH, full_readme)
        await store.put_files(b.build_id, [full])
    await store.set_status(b.build_id, "verifying")
    await store.save_outcome(b.build_id, _outcome(H_OLD))
    b = await store.set_status(b.build_id, "review")
    s = VerifySummary(hidden_passed=0, hidden_total=1, public_passed=0, public_total=1)
    await loader.register_packages(
        store,
        root,
        b,
        loader.PackageInputs(
            header=PackageHeader(
                mock=False,
                approval="pending",
                common_id=CID,
                revision=1,
                build_id=b.build_id,
                template_ref="data/python-duckdb@v1",
                ontology_version=ontology,
                tree_sha256=H_OLD,
                title="가상 과제",
            ),
            candidate=loader.ImageRef("가상-candidate", IMG),
            grader=loader.ImageRef("가상-grader", IMG),
            readme="# 가상",
            workspace="/workspace",
            commands={"test": "python -m pytest -q"},
            grader_sha256=H_OLD,
            timeout_s=1200,
            case_ids=["hc1"],
            starter_expected=s,
        ),
    )
    ok = ReviewDecision.of(FLAG, "fixed", "가상승인자", note="가상 메모")
    return await store.approve(b.build_id, "가상승인자", NOW, [ok])


def _sums_ok(d: Path) -> bool:
    return (
        subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=d, check=False).returncode
        == 0
    )


async def test_finalize_rewrites_descriptors_and_registers_the_kit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InMemoryBuildStore()
    b = await _approved_build(store, tmp_path, monkeypatch)
    assert [x.build_id for x in await store.pending_finalize()] == [b.build_id]
    done = await PackageFinalizer(store, tmp_path).finalize(b)
    assert done.packages_final_at is not None and done.error is None
    for role, cls in (("candidate", CandidateDescriptor), ("grader", GraderDescriptor)):
        d = role_dir(tmp_path, b.build_id, role)  # type: ignore[arg-type]
        desc = cls.model_validate_json((d / f"{role}.json").read_text("utf-8"))
        assert (desc.approval, desc.mock) == ("approved", False)
        assert _sums_ok(d)
    kdir = role_dir(tmp_path, b.build_id, "eval_kit")
    kit = EvalKit.model_validate_json((kdir / "kit.json").read_text("utf-8"))
    assert kit.approval == "approved" and kit.build_id == b.build_id
    assert [d.verdict for d in kit.review_decisions] == ["fixed"]  # 승인자 판단이 키트에
    assert [c.id for c in kit.cases] == ["hc1"] and kit.cases[
        0
    ].reason_description  # 템플릿 사유 설명
    assert _sums_ok(kdir)
    assert {a.role for a in await store.artifacts(b.build_id)} == {
        "candidate",
        "grader",
        "eval_kit",
    }
    assert await store.pending_finalize() == []
    again = await PackageFinalizer(store, tmp_path).finalize(
        done
    )  # 두 번째는 규칙 위반 — 사유만, 상태 그대로
    assert again.status == "approved" and (again.error or "").startswith("finalize:")
    assert not (kdir / README_FULL_PATH).exists()  # 예전 판 — 원래 판 README 없음


async def test_full_readme_goes_beside_the_kit_not_into_the_candidate_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """문제 분석 판 — 원래 판 README 는 평가 키트 옆(평가 쪽, 채점과 같은 권한)에만. 응시용 묶음에는 얇은 판만 (analysis-stage.md §6·§8)."""
    store = InMemoryBuildStore()
    full = "# 가상 과제\n\n## 요구 사항\n\n- **r1** 가상 요구\n"
    b = await _approved_build(store, tmp_path, monkeypatch, full_readme=full)
    done = await PackageFinalizer(store, tmp_path).finalize(b)
    assert done.packages_final_at is not None and done.error is None
    kdir = role_dir(tmp_path, b.build_id, "eval_kit")
    assert (kdir / README_FULL_PATH).read_text("utf-8") == full and _sums_ok(kdir)
    kit = next(a for a in await store.artifacts(b.build_id) if a.role == "eval_kit")
    assert {f.name for f in kit.files} == {"kit.json", README_FULL_PATH}
    cdir = role_dir(tmp_path, b.build_id, "candidate")
    assert not (cdir / README_FULL_PATH).exists()


async def test_dirty_ontology_cannot_become_an_approved_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """커밋 안 된 온톨로지(+dirty)로 만든 빌드는 전달 묶음이 될 수 없다 (불변식 4) — 확정 실패 사유를 남기고 다시 하지 않는다."""
    store = InMemoryBuildStore()
    b = await _approved_build(store, tmp_path, monkeypatch, ontology="onto@abc+dirty")
    done = await PackageFinalizer(store, tmp_path).finalize(b)
    assert done.packages_final_at is None and done.status == "approved"
    assert done.error is not None and "+dirty" in done.error
    assert await store.pending_finalize() == []
    cand = json.loads((role_dir(tmp_path, b.build_id, "candidate") / "candidate.json").read_text())
    assert cand["approval"] == "pending"  # 파일도 그대로


async def test_worker_finalizes_before_taking_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from task_spec.builder.runner import RunConfig, Worker

    store = InMemoryBuildStore()
    b = await _approved_build(store, tmp_path, monkeypatch)
    w = Worker(
        store=store,
        steps=None,  # type: ignore[arg-type]  — 큐가 비어 단계를 부르지 않는다
        cfg=RunConfig(work_root=tmp_path / "work", budget_usd=0.5, lease_seconds=60),
        name="w1",
        daily_budget_usd=5.0,
        poll_seconds=0.01,
        max_attempts=2,
        finalizer=PackageFinalizer(store, tmp_path),
    )
    await w.serve(once=True)
    got = await store.get(b.build_id)
    assert got is not None and got.packages_final_at is not None
