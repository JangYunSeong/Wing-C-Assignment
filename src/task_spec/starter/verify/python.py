"""Python verifier — 데이터(Python) 템플릿 대상을 조립해 컴파일 확인 → pytest 를 돌리고 `VerifyReport` 로 (알파·builder 공용, #126).

`GradleVerifier`·`NodeVerifier` 와 같은 자리·같은 인터페이스(compile_tests · compile_overlays · verify):
템플릿 project/ + 오버레이(+ 숨은·공개 테스트면 잠긴 하네스·자체 점검)를 채점용 이미지와 같은 순서로 조립하고, 대상마다

1. 컴파일 확인 — `compileall`(문법) + 채점 테스트 `pytest --collect-only`(import 실패·없는 이름) → compile_error 면 끝.
2. 기존 테스트 — 지원자 pytest 설정(pyproject, tests/) → group existing.
3. 채점 테스트 — 채점 설정(`commands.hidden_test`, grading/harness/grader-pytest.ini) → 자체 점검·숨은 케이스·공개 테스트.

판정 함수는 채점용 이미지의 grade(starter/grade/image_grade_py.py)와 같은 코드 한 벌이다.
의존성: 템플릿 uv.lock 으로 한 번 만든 가상 환경을 모든 대상이 쓴다 (<BUILD_TOOLS_DIR>/.py-envs/<템플릿>@v<n>-<lock 해시>/, git 밖 —
환경 변수 PY_ENV_DIR 로 바꿀 수 있다, verify/tools.py). 2026-11-09 데이터 템플릿.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from task_spec.schemas import CompileError, TargetRun, TestResult, VerifyReport
from task_spec.starter.grade import image_grade_py as judge
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template
from task_spec.starter.verify import tools
from task_spec.starter.verify.workspace import assemble, run_logged, write_overlay

GRADING_INI = "grading/harness/grader-pytest.ini"
GRADING_ROOTDIR = "grading/harness"
COMPILE_PATHS = ("pipeline", "tests", "scripts")  # 문법 확인 대상 (있는 것만)


def python_for(t: Template, timeout: int = 1800) -> Path:
    """템플릿 의존성을 lock 그대로 한 번 설치한 가상 환경의 python — 이미 있으면 그대로 (lock 이 바뀌면 새 폴더)."""
    env = os.environ.get("PY_ENV_DIR")
    if env:
        return Path(env) / "bin" / "python"
    lock = t.project / "uv.lock"
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()[:10]
    d = (
        tools.py_cache()
        / f"{t.manifest.template_id.replace('/', '__')}@v{t.manifest.version}-{digest}"
    )
    py = d / "venv" / "bin" / "python"
    if (d / ".installed").is_file() and py.is_file():
        return py
    d.mkdir(parents=True, exist_ok=True)
    for f in ("pyproject.toml", "uv.lock"):
        shutil.copy2(t.project / f, d / f)
    with (d / "install.log").open("w", encoding="utf-8") as log:
        subprocess.run(
            ["uv", "sync", "--frozen", "--offline", "--project", str(d)],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=timeout,
            env=os.environ | {"UV_PROJECT_ENVIRONMENT": str(d / "venv")},
        )
    (d / ".installed").write_text("", "utf-8")
    return py


def grading_paths(t: Template) -> list[str]:
    """채점 명령(`commands.hidden_test`)의 대상 폴더 — 설정 파일 뒤의 인자들 (채점용 이미지 config 와 같이 쓴다)."""
    cmds = t.manifest.commands
    words = shlex.split(cmds.hidden_test) if cmds is not None and cmds.hidden_test else []
    after = words[words.index(GRADING_INI) + 1 :] if GRADING_INI in words else []
    return [w for w in after if not w.startswith("-")]


def layout_of(t: Template) -> dict[str, str]:
    hl = t.manifest.hidden_layout
    public = t.manifest.public_helper
    return {
        "cases": hl.cases if hl is not None else "",
        "selftest": hl.selftest if hl is not None else "",
        "public": str(Path(public.path).parent) if public is not None else "public_tests",
    }


class PythonVerifier:
    """대상마다 조립 → 컴파일 확인 → pytest(기존) → pytest(채점) → TargetRun. 조립 폴더는 지우고 로그만 남긴다."""

    def __init__(
        self,
        template: Template,
        work: Path,
        timeout: int = 600,
        keep_code: bool = False,
        python: Path | None = None,
    ) -> None:
        self.template, self.work, self.timeout = template, work, timeout
        self.keep_code = keep_code
        self._python = python
        self.rounds = 0
        self.compiles = 0

    @property
    def python(self) -> Path:
        if self._python is None:
            self._python = python_for(self.template)
        return self._python

    def _grading_targets(self, dest: Path) -> list[str]:
        """채점 명령의 대상 폴더 중 이 조립에 있는 것 (설정 파일 뒤의 인자들)."""
        return [w for w in grading_paths(self.template) if (dest / w).exists()]

    def _compile_check(self, dest: Path, log: Path) -> tuple[int | None, list[CompileError]]:
        py = str(self.python)
        start = log.stat().st_size if log.exists() else 0
        code_paths = [p for p in COMPILE_PATHS if (dest / p).exists()]
        codes = [
            run_logged([py, "-m", "compileall", "-q", *code_paths], dest, log, self.timeout)[0]
        ]
        collect = [t for t in self._grading_targets(dest) if not t.endswith("selftest")]
        if collect:
            codes.append(
                run_logged(
                    [py, "-m", "pytest", "-c", GRADING_INI, "--collect-only", "-q", *collect],
                    dest,
                    log,
                    self.timeout,
                )[0]
            )
        if any(c is None for c in codes):
            return None, []
        bad = next((c for c in codes if c not in (0, 5)), 0)  # 5 = 수집된 테스트 없음
        with log.open("r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(start)
            errors = [CompileError(**e) for e in judge.compile_errors(fh.read(), root=str(dest))]
        return bad, errors

    def _pytest(
        self, dest: Path, log: Path, grading: bool
    ) -> tuple[int | None, list[TestResult] | None]:
        py = str(self.python)
        report = dest / (".grading.xml" if grading else ".existing.xml")
        if grading:
            cmd = [py, "-m", "pytest", "-c", GRADING_INI, *self._grading_targets(dest)]
        else:
            cmd = [py, "-m", "pytest", "-o", "junit_family=xunit1"]
        code, _ = run_logged([*cmd, f"--junitxml={report}"], dest, log, self.timeout)
        if not report.is_file():
            return code, None
        rows = judge.junit_results(
            report,
            GRADING_ROOTDIR if grading else "",
            layout_of(self.template),
            self.template.manifest.hidden_rules.case_id_pattern,
            dest,
        )
        return code, [TestResult.model_validate(r) for r in rows]

    def _target(
        self, target: str, kind: str, parts: Sequence[Overlay], hidden: Overlay | None
    ) -> TargetRun:
        dest = self.work / f"r{self.rounds}-{target}"
        log = self.work / f"r{self.rounds}-{target}.log"
        log.write_text("", "utf-8")
        start = time.monotonic()
        assemble(self.template, dest, *parts)
        setup, detail = "ok", ""
        tests: list[TestResult] = []
        code, errors = self._compile_check(dest, log)
        if code is None:
            setup = "timeout"
        elif code != 0:
            setup = "compile_error"
        else:
            hl = self.template.manifest.hidden_layout
            grading = hl is not None and (dest / hl.harness).is_dir()
            for g in (False, True) if grading else (False,):
                c, got = self._pytest(dest, log, g)
                if c is None:
                    setup = "timeout"
                    break
                if got is None:  # pytest 자체가 못 떴다 — 기동 실패와 같은 자리
                    setup, detail = "boot_error", log.read_text("utf-8", errors="ignore")[-400:]
                    break
                tests += got
                with log.open(
                    "a", encoding="utf-8"
                ) as fh:  # 실패 메시지 — 보고서 요약에는 결과만 남는다
                    for t in got:
                        if t.result != "PASS":
                            fh.write(
                                f"## {t.group} {t.name} {t.result} [{t.reason or '-'}] {t.message}\n"
                            )
        shutil.rmtree(dest, ignore_errors=True)
        return TargetRun.model_validate(
            {
                "target": target,
                "kind": kind,
                "setup": setup,
                "compile_errors": [e.model_dump() for e in errors]
                if setup == "compile_error"
                else [],
                "setup_detail": " ".join(detail.split())[:400],
                "tests": [t.model_dump() for t in tests],
                "seconds": round(time.monotonic() - start, 1),
            }
        )

    def _compile(self, parts: Sequence[Overlay], tag: str = "tests") -> list[CompileError]:
        self.compiles += 1
        dest = self.work / f"c{self.compiles}-{tag}"
        log = self.work / f"c{self.compiles}-{tag}.log"
        log.write_text("", "utf-8")
        if self.keep_code:
            for o in parts:
                name = {"hidden": "hidden", "public_test": "public"}.get(o.kind, o.label or o.kind)
                write_overlay(o, self.work / f"c{self.compiles}-code" / name)
        assemble(self.template, dest, *parts)
        code, errors = self._compile_check(dest, log)
        shutil.rmtree(dest, ignore_errors=True)
        if (
            code != 0 and not errors
        ):  # 시간 초과·도구 자체 실패 — 생성기 탓이 아니므로 오류 줄 없이 알린다
            return [
                CompileError(
                    path="(python)", line=0, message=f"컴파일 확인 실행 실패 (종료 코드 {code})"
                )
            ]
        return errors

    async def compile_tests(self, hidden: Overlay, public: Overlay | None) -> list[CompileError]:
        """채점 코드(숨은·공개 테스트 + 하네스 + 도우미)만 구현 없이 — 문법·import 확인."""
        self.work.mkdir(parents=True, exist_ok=True)
        parts = [hidden, *((public,) if public is not None else ())]
        return await asyncio.to_thread(self._compile, parts, "tests")

    async def compile_overlays(self, overlays: Sequence[Overlay]) -> list[CompileError]:
        """구현(시작 코드·참조 구현)을 둘레 산출물과 함께 — 테스트 실행 전에 (build.compile_impl_and_fix)."""
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
        """GradleVerifier.verify 와 같은 대상 순서 — 시작 코드(+공개), 시작 코드 그대로 + 숨은, 참조 구현마다 (+공개 + 숨은)."""
        del (
            conform
        )  # 계약 적합성 스모크는 TypeScript 템플릿만 (starter/conformance.py) — Python 은 2단계
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
