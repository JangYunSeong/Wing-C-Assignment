"""컨테이너 마운트 정책 — 신뢰 수준별 한 정의 (#174). 가상 경로만."""

from __future__ import annotations

from pathlib import Path

from task_spec.starter.loader import grade_command
from task_spec.starter.mounts import grade_argv, security_args, volume
from task_spec.starter.verify.workspace import container_argv


def test_generated_code_keeps_host_labels_and_runs_unlabeled(tmp_path: Path) -> None:
    """과제 생성 검증: 의존성 캐시처럼 함께 쓰는 호스트 폴더를 붙이므로 라벨을 바꾸지 않고 이 컨테이너만 라벨 분리를 끈다."""
    argv = container_argv("img", ["true"], tmp_path, "n", mounts=[tmp_path / "cache"])
    assert "label=disable" in argv
    assert all(not a.endswith((":z", ",z")) for a in argv)
    assert volume("/a", "/a", "generated", read_only=True) == "/a:/a:ro"


def test_applicant_code_keeps_selinux_confinement() -> None:
    """채점(신뢰할 수 없는 지원자 코드): 격리를 켠 채 붙이는 사본만 공유 라벨로 — label=disable 은 라벨을 못 바꾸는 저장소용 대안에만."""
    argv = grade_argv("grader:1", "/s", "/o")
    assert "label=disable" not in argv
    assert "/s:/submission:ro,z" in argv and "/o:/out:z" in argv
    assert security_args("applicant") == [] and security_args("generated") == list(
        security_args("applicant", relabel=False)
    )


def test_delivered_grade_commands_stay_the_same_strings() -> None:
    """grader.json 의 두 명령은 받는 쪽 계약 — 한 정의로 옮겨도 글자가 같다 (예전 문자열 바꾸기 판)."""
    assert grade_command("g:1") == (
        "podman run --rm --network none -v <제출물>:/submission:ro,z -v <결과>:/out:z g:1 grade"
    )
    assert grade_command("g:1", relabel=False) == (
        "podman run --rm --network none --security-opt label=disable "
        "-v <제출물>:/submission:ro -v <결과>:/out g:1 grade"
    )
