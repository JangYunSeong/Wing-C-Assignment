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

Node 는 이미지와 같은 기반 컨테이너(`NODE_IMAGE`, node:22)에서 돌린다 — 설치·tsc·vitest 모두. 조립 폴더와 node_modules 를
같은 절대 경로로 붙여 링크가 그대로 풀린다. 2026-11-10 프론트 알파: 호스트 Node 24 에서는 jsdom 의 AbortSignal 을 fetch 가
거부해 요청을 취소하는 허용 구현이 모든 테스트에서 떨어졌다 — 이미지(Node 22)에서는 31/31 통과. `NODE_VERIFY_IMAGE=host` 면 호스트 Node.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from eval.starter import image_grade_node as judge
from eval.starter.gradle_verify import assemble, write_overlay
from task_spec.schemas import CompileError, TargetRun, TestResult, VerifyReport
from task_spec.starter.conformance import CONFORM_CONFIG
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template

ROOT = Path(__file__).resolve().parents[2]
NODE_CACHE = ROOT / "data" / ".node-modules"
# 응시용·채점용 이미지의 Node 기반과 같게 (build_mock_images 가 이 값을 쓴다)
NODE_IMAGE = "docker.io/library/node:22-bookworm-slim"


def node_env() -> dict[str, str]:
    return os.environ | node_env_vars()


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
    code, _ = run_node(
        ["npm", "ci", "--no-audit", "--no-fund", "--prefer-offline"],
        d,
        d / "install.log",
        timeout,
        network=True,
    )
    if code != 0:
        raise RuntimeError(f"npm ci 실패 ({code}) — {d / 'install.log'}")
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


def node_image() -> str:
    return os.environ.get("NODE_VERIFY_IMAGE", NODE_IMAGE)


def run_node(
    cmd: Sequence[str],
    cwd: Path,
    log: Path,
    timeout: int,
    mounts: Sequence[Path] = (),
    network: bool = False,
) -> tuple[int | None, float]:
    """Node 명령 하나를 이미지와 같은 기반 컨테이너에서 (`run_logged` 와 같은 반환). 경로는 같은 절대 경로로 붙인다."""
    image = node_image()
    if image == "host":
        return run_logged(cmd, cwd, log, timeout)
    name = f"nv-{uuid.uuid4().hex[:12]}"
    paths = sorted({str(p.resolve()) for p in (cwd, *mounts)})
    full = ["podman", "run", "--rm", "--name", name, "-w", str(cwd.resolve())]
    # SELinux 가 켜진 서버에서 붙인 폴더를 컨테이너가 못 읽는다 (2026-11-11 이 서버: 라벨 unlabeled_t → npm ci 'Permission denied').
    # `:z` 는 호스트 파일의 보안 라벨을 바꾸므로 쓰지 않고, 이 컨테이너만 라벨 분리를 끈다 (SELinux 가 없는 서버에서는 영향 없음).
    full += ["--security-opt", "label=disable"]
    full += [] if network else ["--network", "none"]
    full += [x for k, v in node_env_vars().items() for x in ("-e", f"{k}={v}")]
    full += [x for p in paths for x in ("-v", f"{p}:{p}")]
    code, seconds = run_logged([*full, image, *cmd], cwd, log, timeout)
    if code is None:  # 시간 초과 — 클라이언트만 끊겨 컨테이너가 남지 않게
        subprocess.run(["podman", "rm", "-f", name], capture_output=True, check=False)
    return code, seconds


def node_env_vars() -> dict[str, str]:
    return {"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"}


def tsc_errors(log_text: str, limit: int = 20) -> list[CompileError]:
    """tsc --pretty false 출력 → 오류 위치·메시지 (판정은 image_grade_node 와 같은 코드)."""
    return [CompileError(**e) for e in judge.tsc_errors(log_text, limit)]


clean_message = judge.clean_message


def layout_of(t: Template) -> dict[str, str]:
    """채점 폴더 배치 — 숨은 케이스·자체 점검·공개 테스트 폴더 (판정이 파일 경로로 그룹을 정한다)."""
    hl = t.manifest.hidden_layout
    public = t.manifest.public_helper
    return {
        "cases": hl.cases if hl is not None else "",
        "selftest": hl.selftest if hl is not None else "",
        "public": str(Path(public.path).parent) if public is not None else "src/__public__",
    }


def vitest_results(
    report: dict[str, Any], work: Path, t: Template, hidden: Overlay | None
) -> list[TestResult]:
    """vitest JSON 보고서 → 파일마다 결과 하나 (첫 실패를 대표로) — 판정은 채점용 이미지의 grade 와 같은 코드."""
    texts = {f.path: f.content for f in hidden.files} if hidden is not None else None
    rows = judge.vitest_results(
        report, work, layout_of(t), t.manifest.hidden_rules.case_id_pattern, texts, locate=True
    )
    return [TestResult.model_validate(r) for r in rows]


def _log_tail(log: Path, n: int = 6) -> str:
    lines = [
        ln.strip()
        for ln in judge.strip_ansi(log.read_text("utf-8", errors="ignore")).splitlines()
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
        code, _ = run_node(
            [*cmd, "--pretty", "false"], dest, log, self.timeout, [self.node_modules.parent]
        )
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
        code, _ = run_node(
            [*base, "--reporter=json", f"--outputFile={report}"],
            dest,
            log,
            self.timeout,
            [self.node_modules.parent],
        )
        if not report.is_file():
            return code, None
        return code, json.loads(report.read_text("utf-8"))

    def _target(
        self,
        target: str,
        kind: str,
        parts: Sequence[Overlay],
        hidden: Overlay | None,
        conform: Overlay | None = None,
    ) -> TargetRun:
        dest = self.work / f"r{self.rounds}-{target}"
        log = self.work / f"r{self.rounds}-{target}.log"
        log.write_text("", "utf-8")
        start = time.monotonic()
        self._assemble(dest, [*parts, *((conform,) if conform is not None else ())])
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
                got = vitest_results(report, dest, self.template, hidden)
                tests += got
                with log.open(
                    "a", encoding="utf-8"
                ) as fh:  # 실패 메시지 — 보고서 요약에는 결과만 남는다
                    for t in got:
                        if t.result != "PASS":
                            fh.write(
                                f"## {t.group} {t.name} {t.result} [{t.reason or '-'}] {t.message}\n"
                            )
            if setup == "ok" and conform is not None:
                tests += self._conform(dest, log)
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

    def _conform(self, dest: Path, log: Path) -> list[TestResult]:
        """계약 적합성 스모크 — 전용 설정으로 한 번. 화면(it)마다 결과 하나, 실패는 HARNESS_ERR (단언이 아니라 앱이 깨짐)."""
        report = dest / ".conform-report.json"
        code, _ = run_node(
            [
                "npx",
                "vitest",
                "run",
                "--config",
                CONFORM_CONFIG,
                "--reporter=json",
                f"--outputFile={report}",
            ],
            dest,
            log,
            self.timeout,
            [self.node_modules.parent],
        )
        if (
            not report.is_file()
        ):  # 스모크가 못 떴다 — 대상 탓이 아니므로 결과를 남기지 않는다 (로그에만)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"## conform 스모크가 돌지 못함 ({code})\n")
            return []
        out: list[TestResult] = []
        for f in json.loads(report.read_text("utf-8")).get("testResults", []):
            for a in f.get("assertionResults") or []:
                failed = a.get("status") == "failed"
                msg = clean_message(str((a.get("failureMessages") or [""])[0])) if failed else ""
                out.append(
                    TestResult(
                        group="conform",
                        name=str(a.get("title", "conform"))[:120] or "conform",
                        result="HARNESS_ERR" if failed else "PASS",
                        message=msg[:400],
                    )
                )
        with log.open("a", encoding="utf-8") as fh:
            for t in out:
                if t.result != "PASS":
                    fh.write(f"## conform {t.name} {t.result} {t.message}\n")
        return out

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
        conform: Overlay | None = None,
    ) -> VerifyReport:
        """GradleVerifier.verify 와 같은 대상 순서 — 시작 코드(+공개), 시작 코드 그대로 + 숨은, 참조 구현마다 (+공개 + 숨은).
        conform: 계약 적합성 스모크 (starter/conformance.py) — 시작 코드·참조 구현 대상에서 따로 한 번 더 돈다 (group conform)."""
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
        runs = [
            await asyncio.to_thread(
                self._target, "starter", "starter", (starter, *pub), None, conform
            )
        ]
        if runs[0].setup == "ok":
            runs.append(
                await asyncio.to_thread(
                    self._target, "starter+hidden", "starter_hidden", (starter, hidden), hidden
                )
            )
        for r in refs:
            runs.append(
                await asyncio.to_thread(
                    self._target,
                    r.label or r.kind,
                    r.kind,
                    (starter, r, *pub, hidden),
                    hidden,
                    conform,
                )
            )
        return VerifyReport(runs=runs)
