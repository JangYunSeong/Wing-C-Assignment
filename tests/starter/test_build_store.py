"""빌드 저장소 규칙 — 메모리 구현. 같은 시나리오를 Postgres(test_build_store_pg.py)에서도 돈다. 가상 데이터만."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from task_spec.config import Settings
from task_spec.schemas.build_record import BuildArtifact, BuildOutcome, StarterFile
from task_spec.schemas.package import ArtifactFile, ArtifactRole, ReviewDecision, VerifySummary
from task_spec.starter.store import (
    BuildRepo,
    BuildRuleError,
    InMemoryBuildStore,
    archive_name,
    build_dir,
    sha256sums,
)
from tests.starter.test_data_template import _contract, _plan

AT = datetime(2026, 11, 10, tzinfo=UTC)
H_OLD = "c" * 64
H_NEW = "e" * 64
IMG = "sha256:" + "d" * 64
FLAG = "가상 검토 표시"


def _outcome(tree: str) -> BuildOutcome:
    s = VerifySummary(hidden_passed=0, hidden_total=1, public_passed=0, public_total=1)
    return BuildOutcome(
        template_ref="data/python-duckdb@v1",
        tree_sha256=tree,
        grader_sha256=tree,
        contract=_contract(),
        hidden_plan=_plan(),
        verification={"starter-only": s},
        review_flags=[FLAG],
    )


def _artifact(build_id: str, role: ArtifactRole, tree: str) -> BuildArtifact:
    desc = {"candidate": "candidate.json", "grader": "grader.json", "eval_kit": "kit.json"}[role]
    files = [ArtifactFile(name=desc, sha256=tree, size_bytes=1)]
    if role != "eval_kit":
        files.append(ArtifactFile(name=archive_name(build_id, role), sha256=tree, size_bytes=9))
    return BuildArtifact(
        build_id=build_id,
        role=role,
        files=files,
        image_id=None if role == "eval_kit" else IMG,
        tree_sha256=tree,
        grader_sha256=tree,
        created_at=AT,
    )


async def run_lifecycle(store: BuildRepo, common_id: str, tag: str) -> None:
    """빌드 → 파일 → 결과 → 묶음 → 수리(낡은 묶음 폐기) → 다시 묶음 → 승인 → 전달 → published."""
    b1 = await store.new_build(common_id, "onto-test", AT)
    b2 = await store.new_build(common_id, "onto-test", AT)
    assert (b1.build_id, b1.seq, b1.status) == (f"{common_id}-b1", 1, "pending")
    assert b2.build_id == f"{common_id}-b2"
    assert [b.seq for b in await store.builds(common_id)] == [1, 2]

    bid = b1.build_id
    with pytest.raises(BuildRuleError):
        await store.set_status(bid, "approved")  # 승인은 approve() 로만
    with pytest.raises(BuildRuleError, match="수리"):
        await store.reopen(bid)  # pending 은 되돌릴 것이 없다
    await store.set_status(bid, "generating")

    files = [
        StarterFile.of(bid, "starter", "src/App.java", "class App {}"),
        StarterFile.of(bid, "hidden", "src/test/Hc1Test.java", "// 가상 숨은 테스트"),
        StarterFile.of(bid, "ref_accepted", "src/Svc.java", "// d1", variant="d1"),
        StarterFile.of(bid, "ref_accepted", "src/Svc.java", "// d2", variant="d2"),
    ]
    await store.put_files(bid, files)
    await store.put_files(bid, [StarterFile.of(bid, "starter", "src/App.java", "class App { }")])
    assert [f.content for f in await store.files(bid, ["starter"])] == ["class App { }"]
    assert [f.variant for f in await store.files(bid, ["ref_accepted"])] == ["d1", "d2"]
    assert len(await store.files(bid)) == 4

    await store.set_status(bid, "verifying")
    with pytest.raises(BuildRuleError, match="오버레이 파일 저장"):
        await store.put_files(bid, files[:1])  # 검증 중에는 고치지 않는다 — 수리는 reopen 뒤
    await store.save_outcome(bid, _outcome(H_OLD))
    with pytest.raises(BuildRuleError, match="candidate 묶음 등록"):
        await store.put_artifact(_artifact(bid, "candidate", H_OLD))  # review 전

    await store.set_status(bid, "review")
    with pytest.raises(BuildRuleError, match="결과 저장"):
        await store.save_outcome(bid, _outcome(H_NEW))
    with pytest.raises(BuildRuleError, match="현재 결과와 다르다"):
        await store.put_artifact(_artifact(bid, "candidate", H_NEW))
    with pytest.raises(BuildRuleError, match="eval_kit 묶음 등록"):
        await store.put_artifact(_artifact(bid, "eval_kit", H_OLD))  # 키트는 승인 뒤
    for role in ("candidate", "grader"):
        await store.put_artifact(_artifact(bid, role, H_OLD))  # type: ignore[arg-type]
    with pytest.raises(BuildRuleError, match="바꾸지 않는다"):
        await store.put_artifact(_artifact(bid, "grader", H_OLD))

    # 리뷰 #91-1: 묶음 등록 뒤 수리 — 낡은 묶음은 지워지고, 새 결과의 묶음만 승인된다
    removed = await store.reopen(bid)
    assert [a.role for a in removed] == ["candidate", "grader"]
    assert await store.artifacts(bid) == []
    b = await store.get(bid)
    assert b is not None and b.status == "generating"
    await store.put_files(
        bid, [StarterFile.of(bid, "starter", "src/App.java", "class App { int x; }")]
    )
    await store.set_status(bid, "verifying")
    await store.save_outcome(bid, _outcome(H_NEW))
    await store.set_status(bid, "review")

    ok = ReviewDecision.of(FLAG, "accepted_risk", "reviewer-fake", note="가상 메모")
    with pytest.raises(BuildRuleError, match="묶음이 없다"):
        await store.approve(bid, "reviewer-fake", AT, [ok])
    with pytest.raises(BuildRuleError, match="현재 결과와 다르다"):
        await store.put_artifact(_artifact(bid, "candidate", H_OLD))  # 수리 전 이미지
    for role in ("candidate", "grader"):
        await store.put_artifact(_artifact(bid, role, H_NEW))  # type: ignore[arg-type]

    with pytest.raises(BuildRuleError, match="전달"):
        await store.request_delivery(bid, "grader", f"{tag}-grader", "operator-fake", AT)
    with pytest.raises(BuildRuleError, match="승인자 판단이 없는"):
        await store.approve(bid, "reviewer-fake", AT, [])
    other = ReviewDecision.of("없는 표시", "confirmed", "reviewer-fake")
    with pytest.raises(BuildRuleError, match="없는 검토 표시"):
        await store.approve(bid, "reviewer-fake", AT, [ok, other])
    approved = await store.approve(bid, "reviewer-fake", AT, [ok])
    assert approved.status == "approved" and approved.review_decisions == [ok]
    with pytest.raises(BuildRuleError):
        await store.set_status(bid, "generating")
    with pytest.raises(BuildRuleError, match="수리"):
        await store.reopen(bid)  # 승인된 빌드는 되돌리지 않는다 — 새 빌드로
    await store.put_artifact(_artifact(bid, "eval_kit", H_NEW))

    # 전달 대상: 대상마다 받을 수 있는 역할 하나 (리뷰 #91-2)
    for role in ("candidate", "grader", "eval_kit"):
        await store.add_target(f"{tag}-{role}", role)  # type: ignore[arg-type]
    assert (
        await store.add_target(f"{tag}-grader", "grader")
    ).role == "grader"  # 같은 등록은 그대로
    with pytest.raises(BuildRuleError, match="이미 grader 용"):
        await store.add_target(f"{tag}-grader", "candidate")
    with pytest.raises(BuildRuleError, match="grader 만 받는다"):
        await store.request_delivery(bid, "eval_kit", f"{tag}-grader", "operator-fake", AT)
    with pytest.raises(BuildRuleError, match="candidate 만 받는다"):
        await store.request_delivery(bid, "grader", f"{tag}-candidate", "operator-fake", AT)
    with pytest.raises(BuildRuleError, match="등록되지 않은"):
        await store.request_delivery(bid, "grader", f"{tag}-nowhere", "operator-fake", AT)
    with pytest.raises(BuildRuleError, match="전달"):
        await store.request_delivery(b2.build_id, "grader", f"{tag}-grader", "operator-fake", AT)

    d1 = await store.request_delivery(bid, "candidate", f"{tag}-candidate", "operator-fake", AT)
    assert d1.status == "requested" and [f.name for f in d1.files] == [
        "candidate.json",
        archive_name(bid, "candidate"),
    ]
    failed = await store.finish_delivery(d1.delivery_id, False, AT, "가상 연결 실패")
    assert failed.status == "failed" and failed.sent_at is None
    b = await store.get(bid)
    assert b is not None and b.status == "approved"  # 실패한 전달은 발행이 아니다

    d2 = await store.request_delivery(bid, "grader", f"{tag}-grader", "operator-fake", AT)
    sent = await store.finish_delivery(d2.delivery_id, True, AT)
    assert sent.status == "sent" and sent.sent_at == AT
    with pytest.raises(BuildRuleError, match="이미 끝난"):
        await store.finish_delivery(d2.delivery_id, True, AT)
    b = await store.get(bid)
    assert b is not None and b.status == "published" and b.outcome is not None
    assert b.outcome.hidden_plan == _plan() and b.outcome.contract == _contract()
    assert b.outcome.tree_sha256 == H_NEW
    assert [d.status for d in await store.deliveries(bid)] == ["failed", "sent"]

    d3 = await store.request_delivery(bid, "eval_kit", f"{tag}-eval_kit", "operator-fake", AT)
    kit = next(a for a in await store.artifacts(bid) if a.role == "eval_kit")
    assert d3.files == kit.files


async def test_lifecycle_in_memory() -> None:
    await run_lifecycle(InMemoryBuildStore(), "ct-0123456789ab", "t-mem")


async def test_missing_build_raises_key_error() -> None:
    store = InMemoryBuildStore()
    with pytest.raises(KeyError):
        await store.set_status("ct-0123456789ab-b1", "generating")


def test_paths_and_sums(tmp_path: Path) -> None:
    assert build_dir(tmp_path, "ct-0123456789ab-b3") == tmp_path / "ct-0123456789ab-b3"
    for bad in ("../etc", "ct-0123456789ab", "ct-0123456789ab-b1/../x"):
        with pytest.raises(BuildRuleError):
            build_dir(tmp_path, bad)
    assert archive_name("ct-0123456789ab-b3", "grader") == "ct-0123456789ab-b3-grader.tar.zst"
    with pytest.raises(BuildRuleError):
        archive_name("ct-0123456789ab-b3", "eval_kit")
    sums = sha256sums(
        [
            ArtifactFile(name="b.json", sha256=H_OLD, size_bytes=1),
            ArtifactFile(name="a.tar.zst", sha256=H_NEW, size_bytes=1),
        ]
    )
    assert sums == f"{H_NEW}  a.tar.zst\n{H_OLD}  b.json\n"


def test_images_root_defaults_under_data_dir(tmp_path: Path) -> None:
    base = Settings.model_construct(data_dir=tmp_path / "data", images_dir=None)
    assert base.images_root == tmp_path / "data" / "images"
    own = Settings.model_construct(data_dir=tmp_path / "data", images_dir=tmp_path / "img")
    assert own.images_root == tmp_path / "img"  # 지원자 데이터 볼륨과 나눌 수 있다


def test_starter_file_rules() -> None:
    bid = "ct-0123456789ab-b1"
    with pytest.raises(ValueError, match="상대 경로"):
        StarterFile.of(bid, "starter", "../x.java", "x")
    with pytest.raises(ValueError, match="variant"):
        StarterFile.of(bid, "ref_naive", "src/x.java", "x")
    with pytest.raises(ValueError, match="variant"):
        StarterFile.of(bid, "starter", "src/x.java", "x", variant="d1")
    f = StarterFile.of(bid, "starter", "src/x.java", "가")
    with pytest.raises(ValueError, match="sha256"):
        StarterFile.model_validate({**f.model_dump(), "content": "나"})


def test_artifact_rules() -> None:
    bid = "ct-0123456789ab-b1"
    a = _artifact(bid, "grader", H_OLD)
    with pytest.raises(ValueError, match="image_id"):
        BuildArtifact.model_validate({**a.model_dump(), "image_id": None})
    with pytest.raises(ValueError, match="이미지 파일"):
        BuildArtifact.model_validate({**a.model_dump(), "files": [a.files[0].model_dump()]})
    k = _artifact(bid, "eval_kit", H_OLD)
    with pytest.raises(ValueError, match="image_id"):
        BuildArtifact.model_validate({**k.model_dump(), "image_id": IMG})


def test_review_decision_id_must_match_text() -> None:
    d = ReviewDecision.of(FLAG, "confirmed", "reviewer-fake")
    with pytest.raises(ValueError, match="flag_id"):
        ReviewDecision.model_validate({**d.model_dump(), "flag": "문구가 바뀐 표시"})
