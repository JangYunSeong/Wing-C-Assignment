"""컨테이너 마운트 정책 — 신뢰 수준별 한 정의 (#174) [확정: 2026-11-14 사용자 — '신뢰 수준별 한 정의'].

검증과 채점이 SELinux 를 다르게 다뤘다 — 검증은 `--security-opt label=disable`, 채점은 `:z`. 갈림이 알파마다 한쪽만 고친 결과라
이유가 코드에 없었다. 실행하는 코드의 신뢰 수준이 다르므로 정책을 둘로 두되 여기 한 곳에 이유와 함께 정한다.

- generated — 과제 생성 검증(시작 코드·참조 구현·변형 = 우리가 만든 LLM 생성 코드, 네트워크 없음). 붙이는 폴더에 의존성 캐시처럼
  여러 실행이 함께 쓰는 호스트 폴더가 있어 라벨을 바꾸지 않고 이 컨테이너만 라벨 분리를 끈다 (2026-11-11 이 서버: 라벨 unlabeled_t →
  npm ci 'Permission denied').
- applicant — 채점(지원자 코드 = 신뢰할 수 없는 코드). SELinux 격리를 켜 둔 채 붙이는 폴더(제출물 사본·결과 폴더)만 공유 라벨 `:z` 로
  바꾼다. 라벨을 바꿀 수 없는 저장소(NFS 등)용 대안(relabel=False)은 격리를 끄는 것이라 받는 쪽 운영자가 고른다 (grader.json
  `grade_without_selinux_relabel`).
"""

from __future__ import annotations

from typing import Literal

Trust = Literal["generated", "applicant"]
LABEL_DISABLE = ("--security-opt", "label=disable")


def security_args(trust: Trust, relabel: bool = True) -> list[str]:
    """`podman run` 보안 옵션 — 라벨 분리를 끄는 경우에만 (생성 코드, 또는 라벨을 못 바꾸는 저장소의 채점)."""
    return list(LABEL_DISABLE) if trust == "generated" or not relabel else []


def volume(
    host: str, container: str, trust: Trust, read_only: bool = False, relabel: bool = True
) -> str:
    """`-v` 값 — 채점(applicant)은 라벨을 바꿀 수 있으면 `:z` (격리 유지), 생성 코드(generated)는 라벨을 건드리지 않는다."""
    opts = ["ro"] if read_only else []
    if trust == "applicant" and relabel:
        opts.append("z")
    return f"{host}:{container}" + (f":{','.join(opts)}" if opts else "")


def grade_argv(image: str, submission: str, out: str, relabel: bool = True) -> list[str]:
    """채점 컨테이너 `podman run` 인자 (podman 뒤) — 견본 채점(images)·전달 묶음의 grade 명령(loader)이 같이 쓴다."""
    return [
        "run",
        "--rm",
        "--network",
        "none",
        *security_args("applicant", relabel),
        "-v",
        volume(submission, "/submission", "applicant", read_only=True, relabel=relabel),
        "-v",
        volume(out, "/out", "applicant", relabel=relabel),
        image,
        "grade",
    ]
