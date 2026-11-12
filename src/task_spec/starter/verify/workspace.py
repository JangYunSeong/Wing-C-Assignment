"""검증기 공통 — 대상 조립(템플릿 project/ + 오버레이)과 로그를 남기는 명령 실행.

조립 순서는 채점용 이미지와 같다 (`task_spec.starter.images.grader_overlay`). 세 검증기(gradle·node·python)와 이미지 만들기가 같이 쓴다.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template

# 테스트 도구(vitest·pytest)를 비대화·색 없이 — 로그에서 판정 문구를 읽는다
CI_ENV = {"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"}


def write_overlay(overlay: Overlay, root: Path) -> None:
    for f in overlay.files:
        p = root / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.content, "utf-8")


def assemble(t: Template, work: Path, *overlays: Overlay) -> None:
    """템플릿 project/ + 오버레이 (+ 숨은·공개 테스트가 있으면 잠긴 하네스·자체 점검) — 채점용 이미지 조립과 같은 순서.
    공개 테스트(PublicCase)도 하네스(GraderSupport)를 상속한다 — 2026-11-08: 숨은 테스트일 때만 하네스를 넣어 시작 코드 + 공개 테스트가
    컴파일되지 않았다 (실제 Gradle 로 확인)."""
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(t.project, work)
    hl = t.manifest.hidden_layout
    if hl is not None and any(o.kind in ("hidden", "public_test") for o in overlays):
        for sub, dest in (("hidden/harness", hl.harness), ("hidden/selftest", hl.selftest)):
            for rel in t.files(sub):
                target = work / dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(t.root / sub / rel, target)
    for o in overlays:
        write_overlay(o, work)


def run_logged(cmd: Sequence[str], cwd: Path, log: Path, timeout: int) -> tuple[int | None, float]:
    """명령 하나 — 출력은 log 에 덧붙인다. 시간 초과면 프로세스 묶음(vitest 일꾼 포함)을 끊고 None.
    환경은 지금 프로세스 환경 + `CI_ENV`."""
    start = time.monotonic()
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"$ {shlex.join(cmd)}\n")
        fh.flush()
        p = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=os.environ | CI_ENV,
            start_new_session=True,
        )
        try:
            code: int | None = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            code = None
    return code, time.monotonic() - start
