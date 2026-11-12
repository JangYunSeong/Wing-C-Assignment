"""Gradle verifier — Java 템플릿 대상을 조립해 `gradle test` 를 돌리고 `VerifyReport` 로 (알파·builder 공용, #126).

실제 verifier(#55)는 네트워크 없는 채점용 이미지·반복·자원 축소까지 한다 (spec-to-code §7.1). 이것은 그 전에 수리 루프를 실제 빌드로 돌려 보는 대용품이다.
템플릿 project/ + 오버레이(+ 숨은 테스트면 잠긴 하네스·자체 점검)를 채점용 이미지와 같은 순서로 조립한다.

격리 (#127): 기본은 검증용 템플릿 층(`layers.ensure_verify_layer` — 채점 이미지와 같은 JDK·Gradle wrapper·오프라인 의존성) 위
`--network none` 자식 컨테이너에서 `./gradlew --offline` (채점 이미지의 TEST_COMMAND 와 같은 방식). `VERIFY_ISOLATION=host` 면
예전처럼 호스트 Gradle — 실행 파일·의존성 캐시 경로는 `verify/tools.py` (GRADLE_BIN·GRADLE_USER_HOME·BUILD_TOOLS_DIR).
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
from task_spec.starter.layers import ensure_verify_layer
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template
from task_spec.starter.verify import tools
from task_spec.starter.verify.workspace import (
    CI_ENV,
    assemble,
    isolated,
    run_in_container,
    write_overlay,
)

_JAVAC = re.compile(r"^(/\S+\.java):(\d+): error: (.+)$")
_REASON = re.compile(r"\[reason:([a-z0-9_]+)\]")
_ASSERTION_TYPES = ("AssertionError", "AssertionFailedError", "MultipleFailuresError")
_BOOT = ("Failed to load ApplicationContext", "ApplicationContext failure threshold")


def gradle_env() -> dict[str, str]:
    return os.environ | {"GRADLE_USER_HOME": str(tools.gradle_user_home())}


GRADLE_ARGS = ("--console=plain", "--no-daemon", "--continue")


def run_gradle_in(
    image: str, work: Path, log: Path, timeout: int = 1200, task: str = "test"
) -> tuple[int | None, float]:
    """검증용 템플릿 층에서 `./gradlew --offline` — 조립 폴더만 같은 절대 경로로 붙이고 네트워크 없음."""
    log.write_text("", "utf-8")
    code, seconds = run_in_container(
        image,
        ["sh", "./gradlew", task, "--offline", *GRADLE_ARGS],
        work,
        log,
        timeout,
        env=dict(CI_ENV),  # 호스트 실행과 같은 환경 변수
    )
    return code, round(seconds, 1)


def run_gradle(
    work: Path, log: Path, timeout: int = 1200, task: str = "test"
) -> tuple[int | None, float]:
    """호스트 Gradle (`VERIFY_ISOLATION=host`). (종료 코드 — 시간 초과면 None, 걸린 초).
    task 는 Gradle 작업 — 상류 평가는 compileTestJava (구현 없이 채점 코드 컴파일만)."""
    t0 = time.monotonic()
    with log.open("w") as fh:
        try:
            proc = subprocess.run(
                [str(tools.gradle_bin()), task, *GRADLE_ARGS],
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


_DETAIL = re.compile(r"^\s*(symbol|location):\s*(.+)$")


def compile_errors(log_text: str, work: Path, limit: int = 20) -> list[CompileError]:
    """javac 오류 — 경로는 작업 폴더 기준, 없는 이름이면 뒤따르는 symbol·location 줄을 detail 로."""
    out: list[CompileError] = []
    prefix = str(work.resolve()) + "/"
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        m = _JAVAC.match(line)
        if not m:
            continue
        path = m.group(1)
        path = path[len(prefix) :] if path.startswith(prefix) else path
        detail = []
        for nxt in lines[i + 1 : i + 6]:
            if _JAVAC.match(nxt):
                break
            if d := _DETAIL.match(nxt):
                detail.append(f"{d.group(1)}: {d.group(2).strip()}")
        err = CompileError(
            path=path,
            line=int(m.group(2)),
            message=m.group(3)[:300],
            detail=", ".join(detail)[:300],
        )
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


_CAUSED = re.compile(r"^\s*Caused by:\s*(.+)$", re.M)


def boot_cause(results_dir: Path) -> str:
    """기동 실패 보고서의 실패 스택에서 가장 깊은 `Caused by` 문장 — 앱 컨텍스트를 못 띄운 진짜 이유 (없으면 빈 문자열)."""
    for xml in sorted(results_dir.glob("TEST-*.xml")):
        for bad in ET.parse(xml).getroot().iter():
            if bad.tag not in ("failure", "error"):
                continue
            text = bad.text or ""
            if not any(b in (bad.get("message", "") + text) for b in _BOOT):
                continue
            causes = _CAUSED.findall(text)
            if causes:
                return " ".join(causes[-1].split())[:400]
    return ""


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
        elif ".publictests." in classname:
            group, name = "public", simple
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

    def __init__(
        self, template: Template, work: Path, timeout: int = 1200, keep_code: bool = False
    ) -> None:
        self.template, self.work, self.timeout = template, work, timeout
        self.keep_code = (
            keep_code  # True 면 회차마다 생성 코드(오버레이)를 r<n>-code/<대상>/ 에 남긴다
        )
        self.rounds = 0
        self.compiles = 0
        self._image: str | None = None

    def _gradle(self, dest: Path, log: Path, task: str = "test") -> tuple[int | None, float]:
        if not isolated():
            return run_gradle(dest, log, self.timeout, task)
        if self._image is None:  # 처음 한 번 — 템플릿 내용이 같으면 있는 층을 쓴다
            self._image = ensure_verify_layer(self.template, tools.tools_dir())
        return run_gradle_in(self._image, dest, log, self.timeout, task)

    def _target(
        self, target: str, kind: str, parts: Sequence[Overlay], hidden: Overlay | None
    ) -> TargetRun:
        dest = self.work / f"r{self.rounds}-{target}"
        log = self.work / f"r{self.rounds}-{target}.log"
        assemble(self.template, dest, *parts)
        code, seconds = self._gradle(dest, log)
        text = log.read_text("utf-8", errors="ignore")
        errors = compile_errors(text, dest)
        ids = case_ids(hidden, self.template.manifest.hidden_rules.case_id_pattern)
        tests = junit_results(dest / "build" / "test-results" / "test", ids)
        cause = boot_cause(dest / "build" / "test-results" / "test")
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
                "setup_detail": cause if setup == "boot_error" else "",
                "tests": [t.model_dump() for t in tests],
                "seconds": seconds,
            }
        )

    def _compile(self, parts: Sequence[Overlay], tag: str = "tests") -> list[CompileError]:
        """템플릿 project/ + 오버레이들(숨은·공개 테스트가 있으면 하네스도)을 조립해 컴파일만 — 템플릿 commands.test_compile."""
        self.compiles += 1
        dest = self.work / f"c{self.compiles}-{tag}"
        log = self.work / f"c{self.compiles}-{tag}.log"
        if self.keep_code:
            for o in parts:
                name = {"hidden": "hidden", "public_test": "public"}.get(o.kind, o.label or o.kind)
                write_overlay(o, self.work / f"c{self.compiles}-code" / name)
        assemble(self.template, dest, *parts)
        cmds = self.template.manifest.commands
        cmd = (cmds.test_compile if cmds is not None else None) or "gradlew compileTestJava"
        task = next(t for t in cmd.split()[1:] if not t.startswith("-"))
        code, _ = self._gradle(dest, log, task)
        text = log.read_text("utf-8", errors="ignore")
        errors = compile_errors(text, dest)
        shutil.rmtree(dest, ignore_errors=True)
        if (
            code != 0 and not errors
        ):  # 시간 초과·Gradle 자체 실패 — 생성기 탓이 아니므로 오류 줄 없이 알린다
            return [
                CompileError(
                    path="(gradle)", line=0, message=f"컴파일 실행 실패 (종료 코드 {code})"
                )
            ]
        return errors

    async def compile_tests(self, hidden: Overlay, public: Overlay | None) -> list[CompileError]:
        """채점 코드(숨은·공개 테스트 + 하네스 + 도우미)만 구현 없이 컴파일 — 템플릿 commands.test_compile."""
        self.work.mkdir(parents=True, exist_ok=True)
        parts = [hidden, *((public,) if public is not None else ())]
        return await asyncio.to_thread(self._compile, parts, "tests")

    async def compile_overlays(self, overlays: Sequence[Overlay]) -> list[CompileError]:
        """구현(시작 코드·참조 구현)을 둘레 산출물과 함께 컴파일 — 무거운 검증(기동·테스트) 전에 (build.compile_impl_and_fix)."""
        self.work.mkdir(parents=True, exist_ok=True)
        tag = "-".join(
            o.label or o.kind for o in overlays if o.kind not in ("hidden", "public_test")
        )
        return await asyncio.to_thread(self._compile, list(overlays), tag or "parts")

    async def verify(
        self,
        starter: Overlay,
        hidden: Overlay,
        refs: Sequence[Overlay],
        public: Overlay | None = None,
        conform: Overlay | None = None,
    ) -> VerifyReport:
        """public 이 있으면 시작 코드 실행에 공개 테스트를 넣고(저장 확인은 통과·새 기능 공개 테스트는 실패해야), 참조 구현 실행에도 넣는다."""
        del (
            conform
        )  # 계약 적합성 스모크는 TypeScript 템플릿만 (starter/conformance.py) — Java 는 2단계
        self.rounds += 1
        self.work.mkdir(parents=True, exist_ok=True)
        pub = (public,) if public is not None else ()
        if self.keep_code:
            code = self.work / f"r{self.rounds}-code"
            for name, o in (("starter", starter), ("hidden", hidden), ("public", public)):
                if o is not None:
                    write_overlay(o, code / name)
            for r in refs:
                write_overlay(r, code / (r.label or r.kind))
        runs = [await asyncio.to_thread(self._target, "starter", "starter", (starter, *pub), None)]
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
                    self._target, r.label or r.kind, r.kind, (starter, r, *pub, hidden), hidden
                )
            )
        return VerifyReport(runs=runs)
