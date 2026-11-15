"""컨테이너 마운트 정책 — 한 곳 (#174) [확정: 2026-11-14 사용자 검토 — 생성 코드·지원자 코드 모두 SELinux 격리를 켠다].

검증은 `--security-opt label=disable`(격리 끔), 채점은 `:z`(공유 라벨)로 갈려 있었고 이유가 코드에 없었다. 실행하는 코드가 우리가
만든 LLM 생성 코드든 신뢰할 수 없는 지원자 코드든 격리를 켜 두고, 붙이는 폴더의 쓰임으로 라벨을 고른다:

- run — 실행마다 만든 사본(검증 조립 폴더·채점 제출물 사본·결과 폴더): `:Z` 그 컨테이너 전용 라벨. 컨테이너를 탈출한 코드가 같은
  서버에서 앞서 붙인 다른 실행(다른 지원자 제출물)을 읽지 못한다. 사본이라 라벨이 바뀌어도 다른 데 영향이 없다.
  전제: 같은 폴더를 두 컨테이너가 동시에 `:Z` 로 붙이지 않는다 — 병렬 검증은 대상마다 조립 폴더가 따로다.
- shared — 여러 실행이 함께 쓰는 파이프라인 전용 캐시(Node 검증의 node_modules): `:z` 공유 라벨. `:Z` 면 다음·동시 컨테이너가
  못 읽는다. 캐시를 만드는 설치(npm ci)에서 그 캐시가 작업 폴더여도 shared.
- 라벨을 바꿀 수 없는 저장소(NFS 등)용 채점 대안만 `--security-opt label=disable` — 격리를 끄는 것이라 사본을 로컬 디스크에 둘 수
  없을 때만 (grader.json `grade_without_selinux_relabel`).

SELinux 가 꺼진 서버(운영 서버, docs/deploy.md)에서는 라벨 옵션이 동작에 영향이 없다 — SELinux 가 켜진 서버 기준의 정책이다.
"""

from __future__ import annotations

from typing import Literal

from task_spec.schemas.package import COPY_NOTE, NO_RELABEL_NOTE

__all__ = ["COPY_NOTE", "NO_RELABEL_NOTE", "Scope", "grade_argv", "security_args", "volume"]

Scope = Literal["run", "shared"]
LABEL_DISABLE = ("--security-opt", "label=disable")


def security_args(relabel: bool = True) -> list[str]:
    """`podman run` 보안 옵션 — 라벨을 바꿀 수 없는 저장소의 채점 대안에서만 격리를 끈다."""
    return [] if relabel else list(LABEL_DISABLE)


def volume(
    host: str,
    container: str,
    scope: Scope = "run",
    read_only: bool = False,
    relabel: bool = True,
) -> str:
    """`-v` 값 — 실행 사본은 `:Z`(전용), 함께 쓰는 캐시는 `:z`(공유). relabel=False 면 라벨 옵션 없이 (security_args 와 함께)."""
    opts = ["ro"] if read_only else []
    if relabel:
        opts.append("Z" if scope == "run" else "z")
    return f"{host}:{container}" + (f":{','.join(opts)}" if opts else "")


def grade_argv(image: str, submission: str, out: str, relabel: bool = True) -> list[str]:
    """채점 컨테이너 `podman run` 인자 (podman 뒤) — 견본 채점(images)·전달 묶음의 grade 명령(loader)이 같이 쓴다.
    제출물·결과는 채점마다 만든 사본이라 run (COPY_NOTE)."""
    return [
        "run",
        "--rm",
        "--network",
        "none",
        *security_args(relabel),
        "-v",
        volume(submission, "/submission", "run", read_only=True, relabel=relabel),
        "-v",
        volume(out, "/out", "run", relabel=relabel),
        image,
        "grade",
    ]
