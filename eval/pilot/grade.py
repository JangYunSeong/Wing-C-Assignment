"""풀이 채점 — 공개 테스트 ①(원래)·②(행동만)과 숨은 테스트를 테스트 메서드 단위로 (docs/design/analysis-stage.md §9.1 채점).

빌드 검증과 같은 조립·실행(템플릿 project/ + 오버레이, 네트워크 없는 자식 컨테이너)을 쓰되, 검증기는 테스트 클래스 단위로만
결과를 내므로 JUnit 보고서를 직접 읽어 메서드마다 남긴다. 한 풀이에 Gradle 두 번:
  A = 풀이 + 공개 테스트① + 숨은 테스트    B = 풀이 + 공개 테스트②
풀이 오버레이 = 작업 폴더의 src/ (채점용 패키지 grading·publictests 는 뺀다 — 풀이가 만들 수 없는 자리).

  uv run python -m eval.pilot.grade <label...>
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from pydantic import Field

from eval.pilot.model import DATA, RUNS, load_material, parse_label
from task_spec.schemas import GeneratedFile
from task_spec.schemas.base import Schema
from task_spec.starter.grade.image_grade import SKIPPED, group_of, junit_suites
from task_spec.starter.images import PUBLIC_ID
from task_spec.starter.layers import ensure_verify_layer
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template, select_template
from task_spec.starter.verify import tools
from task_spec.starter.verify.gradle import case_ids, run_gradle, run_gradle_in
from task_spec.starter.verify.workspace import assemble, isolated

TIMEOUT = 1200
GRADING = (
    "src/test/java/com/example/service/grading/",
    "src/test/java/com/example/service/publictests/",
)
SKIP_DIRS = {"build", ".gradle", ".idea", "out", "bin"}
PERSISTENCE = (
    "PersistenceCheckTest"  # 시작 코드 저장 확인 — 공개 수용 기준이 아니라 통과율에서 뺀다
)
_BOOT = ("Failed to load ApplicationContext", "ApplicationContext failure threshold")


class Method(Schema):
    group: str  # public · hidden · persistence · other
    case: str  # 공개 테스트 id(pt#)·숨은 케이스 id·클래스 이름
    cls: str
    method: str
    ok: bool
    message: str = ""


class SuiteRun(Schema):
    setup: str  # ok · compile_error · boot_error · timeout
    seconds: float | None = None
    methods: list[Method] = Field(default_factory=list)


class Graded(Schema):
    label: str
    a: SuiteRun  # 풀이 + 공개① + 숨은
    b: SuiteRun  # 풀이 + 공개②


def overlay(kind: str, root: Path, label: str = "", only=None) -> Overlay:  # type: ignore[no-untyped-def]
    files = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if only is not None and not only(rel):
            continue
        files.append(GeneratedFile(path=rel, content=p.read_text("utf-8", errors="ignore")))
    return Overlay(kind=kind, files=tuple(files), label=label)  # type: ignore[arg-type]


def solution(ws: Path, label: str) -> Overlay:
    return overlay(
        "ref_accepted",
        ws,
        label=f"pilot-{label}",
        only=lambda r: r.startswith("src/") and not r.startswith(GRADING),
    )


def method_name(name: str) -> str:
    """JUnit 보고서의 메서드 이름 — Gradle 은 끝에 매개변수 목록을 붙인다(`최신순정렬()`). `method_req` 키는 괄호 없이 적는다."""
    return re.sub(r"\([^()]*\)$", "", name)


def methods(results: Path, public_ids: dict[str, str], hidden_ids: dict[str, str]) -> list[Method]:
    """JUnit 보고서 → 메서드마다 결과. 공개 테스트 클래스 → pt id (@DisplayName), 숨은 테스트 클래스 → 케이스 id.
    판독(통과·실패·건너뜀)과 자리는 채점과 같은 `image_grade.junit_suites`·`group_of` — 건너뛴 메서드는 통과가 아니다 (#173)."""
    out: list[Method] = []
    for cls, cases in junit_suites(results):
        simple = cls.rsplit(".", 1)[-1]
        group = group_of(cls)
        if group == "public":
            group, case = (
                ("persistence", simple)
                if simple == PERSISTENCE
                else ("public", public_ids.get(simple, simple))
            )
        elif group == "hidden":
            case = hidden_ids.get(simple, simple)
        else:
            group, case = "other", simple
        for c in cases:
            why = " ".join(c["message"].split())
            if c["outcome"] == "skipped":
                why = f"{SKIPPED}: {why}" if why else SKIPPED
            out.append(
                Method(
                    group=group,
                    case=case,
                    cls=simple,
                    method=method_name(c["name"]),
                    ok=c["outcome"] == "pass",
                    message=why[:300],
                )
            )
    return out


def _gradle(t: Template, dest: Path, log: Path) -> tuple[int | None, float]:
    if not isolated():
        return run_gradle(dest, log, TIMEOUT, "test")
    return run_gradle_in(ensure_verify_layer(t, tools.tools_dir()), dest, log, TIMEOUT, "test")


def run_suite(
    t: Template, work: Path, parts: tuple[Overlay, ...], public: Overlay, hidden: Overlay | None
) -> SuiteRun:
    dest, log = work.with_suffix(""), work.with_suffix(".log")
    assemble(t, dest, *parts)
    code, seconds = _gradle(t, dest, log)
    text = log.read_text("utf-8", errors="ignore")
    hidden_ids = (
        case_ids(hidden, t.manifest.hidden_rules.case_id_pattern) if hidden is not None else {}
    )
    found = methods(
        dest / "build" / "test-results" / "test", case_ids(public, PUBLIC_ID), hidden_ids
    )
    shutil.rmtree(dest, ignore_errors=True)
    if code is None:
        setup = "timeout"
    elif re.search(r"Task :compile(Test)?Java FAILED", text):
        setup = "compile_error"
    elif any(b in m.message for m in found for b in _BOOT) or (not found and code != 0):
        setup = "boot_error"
    else:
        setup = "ok"
    return SuiteRun(setup=setup, seconds=seconds, methods=found)


def grade(label: str) -> Graded:
    mid = parse_label(label)[0]
    m = load_material(mid)
    t = select_template("sw.backend")
    assert t is not None
    hidden = overlay("hidden", m.alpha_dir / "overlays" / "hidden")
    public1 = overlay("public_test", m.alpha_dir / "overlays" / "public")
    public2 = overlay("public_test", m.dir / "public-behavior")
    sol = solution(RUNS / "ws" / label, label)
    work = DATA / "grade-work" / label
    work.mkdir(parents=True, exist_ok=True)
    a = run_suite(t, work / "a", (sol, public1, hidden), public1, hidden)
    b = run_suite(t, work / "b", (sol, public2), public2, None)
    g = Graded(label=label, a=a, b=b)
    out = DATA / "results" / "grade" / f"{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(g.model_dump_json(indent=1), "utf-8")
    return g


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", nargs="+")
    a = ap.parse_args()
    for label in a.labels:
        g = grade(label)

        def rate(run: SuiteRun, group: str) -> str:
            ms = [x for x in run.methods if x.group == group]
            return f"{sum(x.ok for x in ms)}/{len(ms)}"

        print(
            json.dumps(
                {
                    "label": label,
                    "setup": [g.a.setup, g.b.setup],
                    "public1": rate(g.a, "public"),
                    "public2": rate(g.b, "public"),
                    "hidden": rate(g.a, "hidden"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
