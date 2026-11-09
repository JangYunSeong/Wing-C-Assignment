"""개발 서버 Node verifier — 프론트엔드(TypeScript) 템플릿 대상을 조립해 tsc → vitest 를 돌리고 `VerifyReport` 로 (평가 전용).

`GradleVerifier`(eval/starter/gradle_verify.py)와 같은 자리·같은 인터페이스(compile_tests · compile_overlays · verify):
템플릿 project/ + 오버레이(+ 숨은·공개 테스트면 잠긴 하네스·자체 점검)를 채점용 이미지와 같은 순서로 조립하고, 대상마다

1. 타입 검사 (`commands.test_compile`, tsc) — 오류면 compile_error 로 끝 (Java 의 compileJava 실패와 같은 자리).
2. 기존 테스트 — 지원자 vitest 설정(`vitest run`, 채점 폴더는 제외됨) → group existing.
3. 채점 테스트 — 채점용 설정(`commands.hidden_test`) → 자체 점검·숨은 케이스·공개 테스트 (조립에 하네스가 있을 때만).

판정 (2026-11-09 vitest 3.2 로 실패 모양을 확인): `AssertionError`(기본 매처)와 jest-dom 매처 실패(`Error: expect(…)`)는 ASSERT,
사유는 메시지의 `[reason:…]`. 요소를 못 찾음(`Unable to find role`)·하네스의 `요청이 오지 않음`·TypeError·파일 로드 실패는 HARNESS_ERR —
Java 의 "단언이 아닌 예외"와 같은 자리라 수리는 구현끼리 비교해 탓을 정한다 (repair._hidden_broken).
파일 하나 = 결과 하나 (첫 실패를 대표로) — Java 의 테스트 클래스 하나 = 결과 하나와 같다.

의존성: 템플릿 package-lock.json 으로 한 번 설치한 node_modules 를 대상마다 링크한다
(data/.node-modules/<템플릿>@v<n>-<lock 해시>/, git 밖. 환경 변수 NODE_MODULES_DIR 로 바꿀 수 있다).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from eval.starter.gradle_verify import assemble, write_overlay
from task_spec.schemas import CompileError, TargetRun, TestResult, VerifyReport
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template

ROOT = Path(__file__).resolve().parents[2]
NODE_CACHE = ROOT / "data" / ".node-modules"
# tsc --pretty false: `src/App.tsx(3,7): error TS2322: Type 'number' is not assignable to type 'string'.` (+ 들여쓴 이어지는 줄)
_TSC = re.compile(r"^(?P<path>[^\s(][^(]*)\((?P<line>\d+),\d+\): error (?P<msg>TS\d+: .+)$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_REASON = re.compile(r"\[reason:([a-z0-9_]+)\]")
_PT = re.compile(r"\bpt\d{1,2}\b")
# 실패 메시지 뒤에 붙는 DOM 덤프·스택 — 앞부분만 남긴다
_TAIL = re.compile(r"\n\s*\n\s*Ignored nodes:|\n\s+at |\n\s*\n\s*<")


def node_env() -> dict[str, str]:
    return os.environ | {"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"}


def node_modules_for(t: Template, timeout: int = 1800) -> Path:
    """템플릿 의존성을 lock 그대로 한 번 설치한 node_modules — 이미 있으면 그대로 쓴다 (lock 이 바뀌면 새 폴더)."""
    env = os.environ.get("NODE_MODULES_DIR")
    if env:
        return Path(env)
    lock = t.project / "package-lock.json"
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()[:10]
    name = t.manifest.template_id.replace("/", "__")
    d = NODE_CACHE / f"{name}@v{t.manifest.version}-{digest}"
    if (d / ".installed").is_file():
        return d / "node_modules"
    d.mkdir(parents=True, exist_ok=True)
    for f in ("package.json", "package-lock.json"):
        shutil.copy2(t.project / f, d / f)
    with (d / "install.log").open("w", encoding="utf-8") as log:
        subprocess.run(
            ["npm", "ci", "--no-audit", "--no-fund", "--prefer-offline"],
            cwd=d,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=timeout,
            env=node_env(),
        )
    (d / ".installed").write_text("", "utf-8")
    return d / "node_modules"


def run_logged(cmd: Sequence[str], cwd: Path, log: Path, timeout: int) -> tuple[int | None, float]:
    """명령 하나 — 출력은 log 에 덧붙인다. 시간 초과면 프로세스 묶음(vitest 일꾼 포함)을 끊고 None."""
    start = time.monotonic()
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"$ {shlex.join(cmd)}\n")
        fh.flush()
        p = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=node_env(),
            start_new_session=True,
        )
        try:
            code: int | None = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            code = None
    return code, time.monotonic() - start


def tsc_errors(log_text: str, limit: int = 20) -> list[CompileError]:
    """tsc --pretty false 출력 → 오류 위치·메시지 (이어지는 들여쓴 줄은 detail)."""
    out: list[CompileError] = []
    lines = _ANSI.sub("", log_text).splitlines()
    for i, raw in enumerate(lines):
        m = _TSC.match(raw.strip())
        if m is None:
            continue
        more: list[str] = []
        for nxt in lines[i + 1 : i + 4]:
            if not nxt.startswith("  ") or _TSC.match(nxt.strip()):
                break
            more.append(nxt.strip())
        err = CompileError(
            path=m.group("path").strip(),
            line=int(m.group("line")),
            message=m.group("msg")[:300],
            detail=" ".join(more)[:300],
        )
        if err not in out:
            out.append(err)
    return out[:limit]


def clean_message(text: str) -> str:
    """실패 메시지 앞부분 — 색 코드·DOM 덤프·스택을 뗀다."""
    text = _ANSI.sub("", text or "")
    m = _TAIL.search(text)
    if m is not None:
        text = text[: m.start()]
    return " ".join(text.split())[:400]


def _is_assertion(message: str) -> bool:
    head = message.lstrip()
    return (
        head.startswith("AssertionError")
        or head.startswith(
            "Error: expect("
        )  # jest-dom 매처 (사유 메시지는 버려진다 — manifest reason_not_on)
        or "[reason:" in head.split("\n", 1)[0]
    )


def _rel(name: str, work: Path) -> str:
    try:
        return Path(name).resolve().relative_to(work.resolve()).as_posix()
    except ValueError:
        return Path(name).as_posix()


def _group(rel: str, t: Template) -> str:
    hl = t.manifest.hidden_layout
    public = t.manifest.public_helper
    public_dir = str(Path(public.path).parent) if public is not None else "src/__public__"
    if hl is not None and rel.startswith(hl.cases.rstrip("/") + "/"):
        return "hidden"
    if hl is not None and rel.startswith(hl.selftest.rstrip("/") + "/"):
        return "selftest"
    if rel.startswith(public_dir.rstrip("/") + "/"):
        return "public"
    return "existing"


def _name(
    group: str, rel: str, titles: Sequence[str], case_rx: re.Pattern[str], hidden: Overlay | None
) -> str:
    """숨은 케이스는 케이스 id (describe 제목 → 파일 본문), 공개 테스트는 pt id, 그 밖은 src 기준 파일 경로."""
    if group == "hidden":
        for title in titles:
            m = case_rx.search(title)
            if m:
                return m.group(0)
        for f in hidden.files if hidden is not None else ():
            if f.path == rel and (m := case_rx.search(f.content)):
                return m.group(0)
    if group == "public":
        for title in titles:
            if m := _PT.search(title):
                return m.group(0)
    return rel.removeprefix("src/")


def vitest_results(
    report: dict[str, Any], work: Path, t: Template, hidden: Overlay | None
) -> list[TestResult]:
    """vitest JSON 보고서 → 파일마다 결과 하나 (첫 실패를 대표로)."""
    case_rx = re.compile(t.manifest.hidden_rules.case_id_pattern)
    out: list[TestResult] = []
    for f in sorted(report.get("testResults", []), key=lambda x: str(x.get("name", ""))):
        rel = _rel(str(f.get("name", "")), work)
        group = _group(rel, t)
        tests = f.get("assertionResults") or []
        titles = [x for a in tests for x in (a.get("ancestorTitles") or [])] + [
            str(a.get("title", "")) for a in tests
        ]
        name = _name(group, rel, titles, case_rx, hidden)
        failed = [a for a in tests if a.get("status") == "failed"]
        if (
            not failed and f.get("status") == "failed"
        ):  # 파일을 못 읽음 (import·변환 실패) 또는 훅 실패
            msg = clean_message(str(f.get("message") or "테스트 파일 실행 실패"))
            out.append(TestResult(group=group, name=name, result="HARNESS_ERR", message=msg))  # type: ignore[arg-type]
            continue
        if not failed:
            out.append(TestResult(group=group, name=name, result="PASS"))  # type: ignore[arg-type]
            continue
        first = failed[0]
        raw = str((first.get("failureMessages") or [""])[0])
        msg = clean_message(raw)
        reason = _REASON.search(_ANSI.sub("", raw))
        out.append(
            TestResult(
                group=group,  # type: ignore[arg-type]
                name=name,
                result="ASSERT" if _is_assertion(_ANSI.sub("", raw)) else "HARNESS_ERR",
                reason=reason.group(1) if reason else None,
                message=clean_message(f"{first.get('title', '')} — {msg}") if msg else "",
            )
        )
    return out


def _log_tail(log: Path, n: int = 6) -> str:
    lines = [
        ln.strip()
        for ln in _ANSI.sub("", log.read_text("utf-8", errors="ignore")).splitlines()
        if ln.strip() and not ln.startswith("$ ")
    ]
    return " | ".join(lines[-n:])[:400]


class NodeVerifier:
    """대상마다 조립 → tsc → vitest(기존) → vitest(채점) → TargetRun. 조립 폴더는 지우고 로그만 남긴다."""

    def __init__(
        self,
        template: Template,
        work: Path,
        timeout: int = 600,
        keep_code: bool = False,
        node_modules: Path | None = None,
    ) -> None:
        self.template, self.work, self.timeout = template, work, timeout
        self.keep_code = keep_code
        self._node_modules = node_modules
        self.rounds = 0
        self.compiles = 0

    @property
    def node_modules(self) -> Path:
        if self._node_modules is None:
            self._node_modules = node_modules_for(self.template)
        return self._node_modules

    def _assemble(self, dest: Path, parts: Sequence[Overlay]) -> None:
        assemble(self.template, dest, *parts)
        (dest / "node_modules").symlink_to(self.node_modules.resolve(), target_is_directory=True)

    def _tsc(self, dest: Path, log: Path) -> tuple[int | None, list[CompileError]]:
        cmds = self.template.manifest.commands
        cmd = shlex.split((cmds.test_compile if cmds is not None else None) or "npx tsc --noEmit")
        start = log.stat().st_size if log.exists() else 0
        code, _ = run_logged([*cmd, "--pretty", "false"], dest, log, self.timeout)
        with log.open("r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(start)
            return code, tsc_errors(fh.read())

    def _vitest(
        self, dest: Path, log: Path, grading: bool
    ) -> tuple[int | None, dict[str, Any] | None]:
        cmds = self.template.manifest.commands
        base = (
            shlex.split(cmds.hidden_test)
            if grading and cmds is not None and cmds.hidden_test
            else ["npx", "vitest", "run"]
        )
        report = dest / (".grading-report.json" if grading else ".existing-report.json")
        code, _ = run_logged(
            [*base, "--reporter=json", f"--outputFile={report}"], dest, log, self.timeout
        )
        if not report.is_file():
            return code, None
        return code, json.loads(report.read_text("utf-8"))

    def _target(
        self, target: str, kind: str, parts: Sequence[Overlay], hidden: Overlay | None
    ) -> TargetRun:
        dest = self.work / f"r{self.rounds}-{target}"
        log = self.work / f"r{self.rounds}-{target}.log"
        log.write_text("", "utf-8")
        start = time.monotonic()
        self._assemble(dest, parts)
        setup, detail = "ok", ""
        tests: list[TestResult] = []
        code, errors = self._tsc(dest, log)
        if code is None:
            setup = "timeout"
        elif code != 0:
            setup = "compile_error"
        else:
            hl = self.template.manifest.hidden_layout
            grading = hl is not None and (dest / hl.harness).is_dir()
            for g in (False, True) if grading else (False,):
                c, report = self._vitest(dest, log, g)
                if c is None:
                    setup = "timeout"
                    break
                if report is None:  # vitest 자체가 못 떴다 (설정·의존성) — 앱 기동 실패와 같은 자리
                    setup, detail = "boot_error", _log_tail(log)
                    break
                tests += vitest_results(report, dest, self.template, hidden)
        shutil.rmtree(dest, ignore_errors=True)
        return TargetRun.model_validate(
            {
                "target": target,
                "kind": kind,
                "setup": setup,
                "compile_errors": [e.model_dump() for e in errors]
                if setup == "compile_error"
                else [],
                "setup_detail": detail,
                "tests": [t.model_dump() for t in tests],
                "seconds": round(time.monotonic() - start, 1),
            }
        )

    def _compile(self, parts: Sequence[Overlay], tag: str = "tests") -> list[CompileError]:
        """템플릿 project/ + 오버레이들(숨은·공개 테스트가 있으면 하네스도)을 조립해 타입 검사만 — 템플릿 commands.test_compile."""
        self.compiles += 1
        dest = self.work / f"c{self.compiles}-{tag}"
        log = self.work / f"c{self.compiles}-{tag}.log"
        log.write_text("", "utf-8")
        if self.keep_code:
            for o in parts:
                name = {"hidden": "hidden", "public_test": "public"}.get(o.kind, o.label or o.kind)
                write_overlay(o, self.work / f"c{self.compiles}-code" / name)
        self._assemble(dest, parts)
        code, errors = self._tsc(dest, log)
        shutil.rmtree(dest, ignore_errors=True)
        if (
            code != 0 and not errors
        ):  # 시간 초과·tsc 자체 실패 — 생성기 탓이 아니므로 오류 줄 없이 알린다
            return [
                CompileError(
                    path="(tsc)", line=0, message=f"타입 검사 실행 실패 (종료 코드 {code})"
                )
            ]
        return errors

    async def compile_tests(self, hidden: Overlay, public: Overlay | None) -> list[CompileError]:
        """채점 코드(숨은·공개 테스트 + 하네스 + 도우미)만 구현 없이 타입 검사."""
        self.work.mkdir(parents=True, exist_ok=True)
        parts = [hidden, *((public,) if public is not None else ())]
        return await asyncio.to_thread(self._compile, parts, "tests")

    async def compile_overlays(self, overlays: Sequence[Overlay]) -> list[CompileError]:
        """구현(시작 코드·참조 구현)을 둘레 산출물과 함께 타입 검사 — 테스트 실행 전에 (build.compile_impl_and_fix)."""
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
    ) -> VerifyReport:
        """GradleVerifier.verify 와 같은 대상 순서 — 시작 코드(+공개), 시작 코드 그대로 + 숨은, 참조 구현마다 (+공개 + 숨은)."""
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
        if runs[0].setup == "ok":
            runs.append(
                await asyncio.to_thread(
                    self._target, "starter+hidden", "starter_hidden", (starter, hidden), hidden
                )
            )
        for r in refs:
            runs.append(
                await asyncio.to_thread(
                    self._target, r.label or r.kind, r.kind, (starter, r, *pub, hidden), hidden
                )
            )
        return VerifyReport(runs=runs)
