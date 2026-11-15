"""채점 하네스 — 숨은·공개 테스트가 import 하는 것은 여기뿐이다 (`from harness.grader import …`).

숨은 테스트는 지원자 작업을 **공개 계약으로만** 두드린다: 명령(스크립트·옵션)·종료 코드·입력 파일 형식·출력 파일 형식.
지원자 모듈(`pipeline/…`)은 import 하지 않는다. 케이스마다 작은 입력을 직접 쓰고(`Workspace.write_*`) 작업을 실행한 뒤(`run`)
출력을 읽어(`Workspace.read_*`) 기대 결과와 비교한다.

- 작업 실행은 별도 프로세스 — 저장소 루트에서 `python <스크립트> <옵션…>`. 환경은 고정(TZ=Asia/Seoul, 해시 시드 0 등).
- 크래시 주입 `run(..., crash_after_bytes=N)`: 파일 크기 상한(RLIMIT_FSIZE) + SIGXFSZ 기본 동작 → 상한을 넘는 첫 쓰기에서
  프로세스가 그 자리에서 죽는다 (정리 코드도 돌지 않는다). 손 예시 daily-agg-pipeline 에서 검증한 방식이다.
- 실패 사유: `assert 값 == 기대, reason('late_data_missed', '무엇이 틀렸는지')`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # 저장소 루트 (grading/harness/grader.py)
INJECT = HERE / "inject"
PARAMS_FILE = HERE.parent / "grader-params.json"
DEFAULT_TIMEOUT_S = 120.0

__all__ = [
    "JobResult",
    "Workspace",
    "params",
    "reason",
    "run",
    "tag",
    "workspace",
]


# ── 실패 사유·조절값 ────────────────────────────────────────────────────


def tag(code: str) -> str:
    return f"[reason:{code}]"


def reason(code: str, description: str) -> str:
    """단언 메시지 — `assert 값 == 기대, reason('late_data_missed', '…')`. 코드는 숨은 테스트 입력의 실패 사유 목록 안의 것만."""
    return f"{tag(code)} {description}"


class _Params:
    """숨은 테스트 계획의 조절값 (grading/grader-params.json — 빌드 파이프라인이 쓴다)."""

    def _all(self) -> dict[str, Any]:
        try:
            return json.loads(PARAMS_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def int(self, name: str, fallback: int) -> int:
        v = self._all().get(name, fallback)
        try:
            return int(v)
        except (TypeError, ValueError):
            return fallback

    def str(self, name: str, fallback: str) -> str:
        v = self._all().get(name, fallback)
        return v if isinstance(v, str) else fallback

    def list(self, name: str, fallback: list[Any]) -> list[Any]:
        v = self._all().get(name, fallback)
        return v if isinstance(v, list) else fallback


params = _Params()


# ── 작업 실행 ───────────────────────────────────────────────────────────


class CrashNotTriggered(RuntimeError):
    """크래시를 요청했는데(crash_after_bytes) 작업이 끝까지 성공함 — 그 케이스의 전제(쓰는 도중에 죽음)가 서지 않아 아무것도 시험하지 못한다.
    단언이 아니라 하네스 오류(HARNESS_ERR)로 남아 테스트 쪽 문제로 보인다 (v2 — 2026-11-15 #187 알파 g187b-de-kb002: 출력 파일이 119~124B 라
    crash_after_bytes=128 에서 아무 구현도 죽지 않았고, 허용 구현 셋이 'Crash run unexpectedly succeeded' 단언에서 떨어졌다)."""


@dataclass(frozen=True)
class JobResult:
    code: int | None  # 종료 코드 (시간 초과면 None, 시그널로 죽으면 음수)
    stdout: str
    stderr: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def crashed(self) -> bool:
        """크래시 주입으로(또는 시그널로) 프로세스가 죽었다."""
        return self.code is not None and self.code < 0

    @property
    def timed_out(self) -> bool:
        return self.code is None

    def last_error(self) -> str:
        """오류 출력의 마지막 줄 (단언 메시지에 붙일 때)."""
        lines = self.stderr.strip().splitlines()
        return lines[-1][:200] if lines else ""


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TZ": "Asia/Seoul",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def run(
    script: str,
    *args: str | Path,
    timeout: float | None = None,
    crash_after_bytes: int | None = None,
) -> JobResult:
    """저장소 루트에서 `python <script> <args…>` — 계약의 명령 그대로 (계약 도우미 run_…() 가 이것을 부른다).
    crash_after_bytes: 파일 하나의 크기 상한(RLIMIT_FSIZE) — 어떤 파일이 그 크기를 넘으려는 쓰기에서 프로세스가 죽는다 (파일마다 따로,
    여러 파일 합이 아님 · DuckDB·pyarrow 같은 네이티브 쓰기도 걸림). 쓰는 파일이 모두 그보다 작으면 죽지 않는다 — 그때 작업이 끝까지
    성공하면 CrashNotTriggered (케이스가 시험하려던 '쓰는 도중 실패'가 일어나지 않음). 작업이 오류 종료 코드로 먼저 끝나면 그대로 돌려준다."""
    home = Path(tempfile.mkdtemp(prefix="home-"))
    env = _env(home)
    cmd: list[str] = [sys.executable, script, *(str(a) for a in args)]
    if crash_after_bytes is not None:
        env["PYTHONPATH"] = str(INJECT)  # sitecustomize: 파이썬이 무시하는 SIGXFSZ 를 기본 동작으로
        cmd = [sys.executable, "-I", str(INJECT / "grader_limited.py"), str(int(crash_after_bytes)), *cmd]
    limit = (
        timeout
        if timeout is not None
        else float(params.int("job_timeout_s", int(DEFAULT_TIMEOUT_S)))
    )
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=limit)
        code: int | None = p.returncode
        out, err = p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        code = None
        out = (
            (e.stdout or b"").decode("utf-8", "replace")
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")
        )
        err = f"{limit}초 안에 끝나지 않음"
    finally:
        shutil.rmtree(home, ignore_errors=True)
    result = JobResult(code, out[-4000:], err[-4000:], round(time.monotonic() - t0, 2))
    if crash_after_bytes is not None and result.ok:
        raise CrashNotTriggered(
            f"크래시를 요청했지만 일어나지 않음 — crash_after_bytes={int(crash_after_bytes)} 는 파일 하나의 크기 상한이라, 이 실행이 쓴 파일이 "
            "모두 그보다 작아 끝까지 성공했다. 출력 파일이 그보다 커지게 입력을 늘리거나 상한을 줄인다"
        )
    return result


# ── 입력·출력 파일 ──────────────────────────────────────────────────────

_ARROW = {
    "string": pa.string(),
    "str": pa.string(),
    "int": pa.int64(),
    "integer": pa.int64(),
    "bigint": pa.int64(),
    "float": pa.float64(),
    "double": pa.float64(),
    "bool": pa.bool_(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "timestamp": pa.timestamp("us"),
    "datetime": pa.timestamp("us"),
}


class Workspace:
    """케이스 하나의 임시 작업 폴더 — 입력 파일을 쓰고 출력 파일을 읽는다. 폴더 이름은 과제를 드러내지 않는다."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # pytest tmp_path 처럼 써도 작업 폴더 경로가 되게 — ws / "raw" · f"{ws}/raw" · str(ws) · os.fspath(ws).
    # 2026-11-09 데이터 알파 8회차: 공개 테스트가 ws / "raw" (TypeError), 다시 만들자 f"{ws}/raw" (객체 이름이 경로로) — 모든 구현이 '입력 없음'.
    def __truediv__(self, other: str | Path) -> Path:
        return self.root / other

    def __fspath__(self) -> str:
        return str(self.root)

    def __str__(self) -> str:
        return str(self.root)

    def path(self, *parts: str | Path) -> Path:
        """작업 폴더 안의 경로 (폴더는 만들지 않는다 — 출력 폴더를 작업이 만들게 둘 때)."""
        return self.root.joinpath(*(str(p) for p in parts))

    def dir(self, *parts: str | Path) -> Path:
        """작업 폴더 안의 폴더 — 없으면 만든다."""
        d = self.path(*parts)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _target(self, rel: str | Path) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # 쓰기 — 계약의 입력 형식 그대로 (필드 이름·값)
    def write_jsonl(self, rel: str | Path, rows: Iterable[Mapping[str, Any]]) -> Path:
        p = self._target(rel)
        p.write_text(
            "".join(json.dumps(dict(r), ensure_ascii=False, default=str) + "\n" for r in rows),
            "utf-8",
        )
        return p

    def write_json(self, rel: str | Path, value: Any) -> Path:
        p = self._target(rel)
        p.write_text(json.dumps(value, ensure_ascii=False, default=str, indent=1), "utf-8")
        return p

    def write_csv(
        self, rel: str | Path, rows: Iterable[Mapping[str, Any]], columns: list[str] | None = None
    ) -> Path:
        rows = [dict(r) for r in rows]
        cols = columns or (list(rows[0]) if rows else [])
        p = self._target(rel)
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        return p

    def write_parquet(
        self,
        rel: str | Path,
        rows: Iterable[Mapping[str, Any]],
        types: Mapping[str, str] | None = None,
    ) -> Path:
        """types: 열 이름 → string·int·float·bool·date·timestamp (없으면 값에서 짐작)."""
        rows = [dict(r) for r in rows]
        p = self._target(rel)
        if types:
            schema = pa.schema([(k, _ARROW.get(v.lower(), pa.string())) for k, v in types.items()])
            table = pa.Table.from_pylist(rows, schema=schema)
        else:
            table = pa.Table.from_pylist(rows)
        pq.write_table(table, p)
        return p

    def write_text(self, rel: str | Path, text: str) -> Path:
        p = self._target(rel)
        p.write_text(text, "utf-8")
        return p

    # 읽기 — 계약의 출력 형식 그대로. 뜻은 형식마다 같다: 없는 경로는 FileNotFoundError, 깨진 파일(반쯤 쓴 CSV·JSONL·parquet)은 예외,
    # 있는 폴더에 데이터 파일이 없으면 빈 목록. 없는지 볼 때는 exists — 읽기의 빈 목록을 '없음'으로 읽지 않는다
    # (v2 — v1 은 없는 경로도 빈 목록이라 크래시 뒤 '출력 없음'(바른 구현)과 '빈 출력'을 가를 수 없었다)
    def exists(self, rel: str | Path) -> bool:
        """그 경로(파일·폴더)가 있는가 — 출력이 아직 없는지 볼 때."""
        return self.path(rel).exists()

    def _targets(self, rel: str | Path, suffix: str) -> list[Path]:
        p = self.path(rel)
        if p.is_file():
            return [p]
        if not p.exists():
            raise FileNotFoundError(f"출력이 없음: {rel}")
        return self.files(rel, suffix)

    def files(self, rel: str | Path, suffix: str = "") -> list[Path]:
        """폴더 아래의 데이터 파일 경로(Path) — 하위 폴더(파티션 `date=…/`)까지, 이름이 `_`·`.` 로 시작하는 파일·폴더는 뺀다, 경로 순.
        읽는 쪽(pandas·DuckDB·Spark 의 파티션 데이터셋 읽기)이 보는 파일과 같다. 2026-11-09 데이터 알파 7회차: 숨은 테스트가
        read_parquet("out/daily_sales")(파티션 상위 폴더)를 읽었는데 하위 폴더를 안 읽어 올바른 구현도 늘 빈 목록."""
        d = self.path(rel)
        if not d.is_dir():
            return []
        return sorted(
            f
            for f in d.rglob(f"*{suffix}")
            if f.is_file()
            and not any(part.startswith(("_", ".")) for part in f.relative_to(d).parts)
        )

    def read_parquet(
        self, rel: str | Path, columns: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """파일 하나, 또는 폴더 아래(하위 파티션 폴더 포함)의 *.parquet 를 모두 읽어 행 목록으로 (경로가 없으면 FileNotFoundError)."""
        out: list[dict[str, Any]] = []
        for f in self._targets(rel, ".parquet"):
            out += pq.read_table(f, columns=columns).to_pylist()
        return out

    def read_jsonl(self, rel: str | Path) -> list[dict[str, Any]]:
        """JSONL 파일·폴더의 행 목록 (경로가 없으면 FileNotFoundError, 반쯤 쓴 줄은 JSONDecodeError)."""
        return [
            json.loads(line)
            for f in self._targets(rel, ".jsonl")
            for line in f.read_text("utf-8").splitlines()
            if line.strip()
        ]

    def read_json(self, rel: str | Path) -> Any:
        return json.loads(self.path(rel).read_text("utf-8"))

    def read_csv(self, rel: str | Path) -> list[dict[str, str]]:
        """CSV 파일·폴더의 행 목록 (경로가 없으면 FileNotFoundError). 칸 수가 머리글과 다른 행은 깨진 파일 — ValueError
        (반쯤 쓴 CSV 는 마지막 줄이 잘려 None 칸이 된다. parquet·jsonl 처럼 읽기에서 드러나게)."""
        out: list[dict[str, str]] = []
        for f in self._targets(rel, ".csv"):
            with f.open(newline="", encoding="utf-8") as fh:
                for i, row in enumerate(csv.DictReader(fh, restkey="\x00extra"), start=2):
                    if None in row.values() or "\x00extra" in row:
                        raise ValueError(f"깨진 CSV {f.name} {i}번째 줄: 칸 수가 머리글과 다름")
                    out.append(row)
        return out

    def partitions(self, rel: str | Path, key: str) -> list[str]:
        """`<rel>/<key>=<값>/` 폴더의 값들, 정렬 (예: partitions('daily_sales', 'date') → ['2026-09-01', …])."""
        d = self.path(rel)
        if not d.is_dir():
            return []
        pre = f"{key}="
        return sorted(
            p.name[len(pre) :] for p in d.iterdir() if p.is_dir() and p.name.startswith(pre)
        )

    def snapshot(self, rel: str | Path = ".") -> dict[str, str]:
        """폴더 아래 파일마다 sha256 — 실패한 실행 전후로 출력이 그대로인지 비교할 때."""
        base = self.path(rel)
        if not base.exists():
            return {}
        return {
            str(f.relative_to(base)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(base.rglob("*"))
            if f.is_file()
        }

    def copy_tree(self, src: str | Path, dst: str | Path) -> Path:
        """폴더째 복사 (실행 전 출력 보관 등)."""
        d = self.path(dst)
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(self.path(src), d)
        return d


_ROOTS: list[Path] = []


def workspace() -> Workspace:
    """케이스마다 새 임시 작업 폴더 (`ws = workspace()`)."""
    if not _ROOTS:
        _ROOTS.append(Path(tempfile.mkdtemp(prefix="grade-")))
    return Workspace(Path(tempfile.mkdtemp(prefix="case-", dir=_ROOTS[0])))
