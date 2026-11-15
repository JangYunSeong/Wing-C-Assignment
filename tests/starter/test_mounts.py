"""컨테이너 마운트 정책 — 신뢰 수준별 한 정의 (#174). 가상 경로만."""

from __future__ import annotations

from pathlib import Path

from task_spec.starter.loader import grade_command
from task_spec.starter.mounts import grade_argv, security_args, volume
from task_spec.starter.verify.workspace import container_argv


def test_verify_keeps_confinement_with_private_run_and_shared_cache(tmp_path: Path) -> None:
    """과제 생성 검증도 격리를 켠다 — 실행마다 만든 조립 폴더는 전용 라벨 :Z, 함께 쓰는 캐시는 공유 라벨 :z (2026-11-14 사용자 검토)."""
    work, cache = tmp_path / "r1-d1", tmp_path / "cache" / "node_modules"
    argv = container_argv("img", ["true"], work, "n", mounts=[cache])
    assert "label=disable" not in argv
    assert f"{work.resolve()}:{work.resolve()}:Z" in argv
    assert f"{cache.resolve()}:{cache.resolve()}:z" in argv
    # 캐시를 만드는 설치에서는 캐시가 작업 폴더여도 공유 — 전용 라벨이면 다음 검증 컨테이너가 못 읽는다
    install = container_argv(
        "img", ["npm", "ci"], cache.parent, "n2", network=True, cwd_shared=True
    )
    assert f"{cache.parent.resolve()}:{cache.parent.resolve()}:z" in install
    assert volume("/a", "/a", "shared", read_only=True) == "/a:/a:ro,z"


def test_applicant_code_gets_a_private_label_on_its_copies() -> None:
    """채점: 공유 라벨(:z)이면 컨테이너를 탈출한 지원자 코드가 앞서 붙인 다른 제출물을 읽을 수 있다 — 사본마다 전용 라벨 :Z."""
    argv = grade_argv("grader:1", "/s", "/o")
    assert "label=disable" not in argv
    assert "/s:/submission:ro,Z" in argv and "/o:/out:Z" in argv
    assert security_args() == [] and security_args(relabel=False) == [
        "--security-opt",
        "label=disable",
    ]


def test_delivered_grade_commands_and_their_notes() -> None:
    """grader.json 의 두 명령은 받는 쪽 계약 — 글자를 고정하고, 칸 설명에 사본 폴더·격리를 끄는 대안의 조건을 적는다."""
    from task_spec.schemas.package import COPY_NOTE, NO_RELABEL_NOTE, GraderDescriptor

    assert grade_command("g:1") == (
        "podman run --rm --network none -v <제출물>:/submission:ro,Z -v <결과>:/out:Z g:1 grade"
    )
    assert grade_command("g:1", relabel=False) == (
        "podman run --rm --network none --security-opt label=disable "
        "-v <제출물>:/submission:ro -v <결과>:/out g:1 grade"
    )
    fields = GraderDescriptor.model_fields
    assert fields["grade"].description == COPY_NOTE and "사본 폴더" in COPY_NOTE
    assert fields["grade_without_selinux_relabel"].description == NO_RELABEL_NOTE
