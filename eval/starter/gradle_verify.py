"""개발 서버 Gradle verifier — Java 템플릿 대상을 조립해 `gradle test` 를 돌리고 `VerifyReport` 로 (평가 전용).

실제 verifier(#55)는 네트워크 없는 채점용 이미지·반복·자원 축소까지 한다 (spec-to-code §7.1). 이것은 그 전에 수리 루프를 실제 빌드로 돌려 보는 대용품이다.
템플릿 project/ + 오버레이(+ 숨은 테스트면 잠긴 하네스·자체 점검)를 채점용 이미지와 같은 순서로 조립한다.

Gradle 의존성 캐시: 환경 변수 GRADLE_USER_HOME, 없으면 data/.gradle-home (git 밖).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

from task_spec.schemas import CompileError, TargetRun, TestResult, VerifyReport
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template

ROOT = Path(__file__).resolve().parents[2]
GRADLE = ROOT / "data" / "feature_starters" / ".tools" / "dist" / "gradle-8.10.2" / "bin" / "gradle"
_JAVAC = re.compile(r"^(/\S+\.java):(\d+): error: (.+)$")
_REASON = re.compile(r"\[reason:([a-z0-9_]+)\]")
_ASSERTION_TYPES = ("AssertionError", "AssertionFailedError", "MultipleFailuresError")
_BOOT = ("Failed to load ApplicationContext", "ApplicationContext failure threshold")


def write_overlay(overlay: Overlay, root: Path) -> None:
    for f in overlay.files:
        p = root / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.content, "utf-8")


def assemble(t: Template, work: Path, *overlays: Overlay) -> None:
    """템플릿 project/ + 오버레이 (+ hidden 이면 잠긴 하네스·자체 점검) — 채점용 이미지 조립과 같은 순서."""
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(t.project, work)
    for o in overlays:
        if o.kind == "hidden" and t.manifest.hidden_layout is not None:
            hl = t.manifest.hidden_layout
            for sub, dest in (("hidden/harness", hl.harness), ("hidden/selftest", hl.selftest)):
                for rel in t.files(sub):
                    target = work / dest / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(t.root / sub / rel, target)
        write_overlay(o, work)


def gradle_env() -> dict[str, str]:
    home = os.environ.get("GRADLE_USER_HOME") or str(ROOT / "data" / ".gradle-home")
    return os.environ | {"GRADLE_USER_HOME": home}


def run_gradle(work: Path, log: Path, timeout: int = 1200) -> tuple[int | None, float]:
    """(종료 코드 — 시간 초과면 None, 걸린 초)."""
    t0 = time.monotonic()
    with log.open("w") as fh:
        try:
            proc = subprocess.run(
                [str(GRADLE), "test", "--console=plain", "--no-daemon", "--continue"],
                cwd=work,
                env=gradle_env(),
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            code = None
    return code, round(time.monotonic() - t0, 1)


def compile_errors(log_text: str, work: Path, limit: int = 20) -> list[CompileError]:
    out: list[CompileError] = []
    prefix = str(work.resolve()) + "/"
    for line in log_text.splitlines():
        m = _JAVAC.match(line)
        if not m:
            continue
        path = m.group(1)
        path = path[len(prefix) :] if path.startswith(prefix) else path
        err = CompileError(path=path, line=int(m.group(2)), message=m.group(3)[:300])
        if err not in out:
            out.append(err)
    return out[:limit]


def case_ids(hidden: Overlay | None, pattern: str) -> dict[str, str]:
    """숨은 테스트 클래스 이름 → 케이스 id (파일마다 처음 나온 id)."""
    out: dict[str, str] = {}
    for f in hidden.files if hidden is not None else ():
        if f.path.endswith(".java"):
            m = re.search(pattern, f.content)
            if m:
                out[Path(f.path).stem] = m.group(0)
    return out


def junit_results(results_dir: Path, ids: dict[str, str]) -> list[TestResult]:
    """Gradle JUnit XML → 클래스(기존 테스트)·케이스(숨은 테스트)마다 결과 하나. 첫 실패를 대표로."""
    out: list[TestResult] = []
    for xml in sorted(results_dir.glob("TEST-*.xml")):
        root = ET.parse(xml).getroot()  # Gradle 이 쓴 보고서 (값은 이스케이프됨)
        first: tuple[str, str] | None = None
        classname = root.get("name", xml.stem)
        for tc in root.iter("testcase"):
            classname = tc.get("classname", classname)
            bad = tc.find("failure")
            if bad is None:
                bad = tc.find("error")
            if bad is not None and first is None:
                first = (bad.get("type", ""), bad.get("message", "") or (bad.text or ""))
        simple = classname.rsplit(".", 1)[-1]
        if ".grading.cases." in classname:
            group, name = "hidden", ids.get(simple, simple)
        elif ".grading." in classname:
            group, name = "selftest", simple
        else:
            group, name = "existing", simple
        if first is None:
            out.append(TestResult(group=group, name=name, result="PASS"))  # type: ignore[arg-type]
            continue
        kind, message = first
        assertion = kind.rsplit(".", 1)[-1] in _ASSERTION_TYPES
        reason = _REASON.search(message)
        out.append(
            TestResult(
                group=group,  # type: ignore[arg-type]
                name=name,
                result="ASSERT" if assertion else "HARNESS_ERR",
                reason=reason.group(1) if reason else None,
                message=" ".join(message.split())[:400],
            )
        )
    return out


class GradleVerifier:
    """대상마다 조립 → gradle test → TargetRun. 조립 폴더는 지우고 로그만 남긴다."""

    def __init__(self, template: Template, work: Path, timeout: int = 1200) -> None:
        self.template, self.work, self.timeout = template, work, timeout
        self.rounds = 0

    def _target(
        self, target: str, kind: str, parts: Sequence[Overlay], hidden: Overlay | None
    ) -> TargetRun:
        dest = self.work / f"r{self.rounds}-{target}"
        log = self.work / f"r{self.rounds}-{target}.log"
        assemble(self.template, dest, *parts)
        code, seconds = run_gradle(dest, log, self.timeout)
        text = log.read_text("utf-8", errors="ignore")
        errors = compile_errors(text, dest)
        ids = case_ids(hidden, self.template.manifest.hidden_rules.case_id_pattern)
        tests = junit_results(dest / "build" / "test-results" / "test", ids)
        shutil.rmtree(dest, ignore_errors=True)
        if code is None:
            setup = "timeout"
        elif re.search(r"Task :compile(Test)?Java FAILED", text):
            setup = "compile_error"
        elif any(b in t.message for t in tests for b in _BOOT) or (not tests and code != 0):
            setup = "boot_error"
        else:
            setup = "ok"
        return TargetRun.model_validate(
            {
                "target": target,
                "kind": kind,
                "setup": setup,
                "compile_errors": [e.model_dump() for e in errors],
                "tests": [t.model_dump() for t in tests],
                "seconds": seconds,
            }
        )

    async def verify(
        self, starter: Overlay, hidden: Overlay, refs: Sequence[Overlay]
    ) -> VerifyReport:
        self.rounds += 1
        self.work.mkdir(parents=True, exist_ok=True)
        runs = [await asyncio.to_thread(self._target, "starter", "starter", (starter,), None)]
        if (
            runs[0].setup == "ok"
        ):  # 시작 코드 그대로 + 숨은 테스트 — 이미 풀려 있으면 안 된다 (repair._starter_solves)
            runs.append(
                await asyncio.to_thread(
                    self._target, "starter+hidden", "starter_hidden", (starter, hidden), hidden
                )
            )
        for r in refs:  # 개발 서버 한 대 — 차례로
            runs.append(
                await asyncio.to_thread(
                    self._target, r.label or r.kind, r.kind, (starter, r, hidden), hidden
                )
            )
        return VerifyReport(runs=runs)
