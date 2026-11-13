"""고정 — 첫 풀이(판 2 opus, 관문 먼저)를 돌리기 전에 한 번 (docs/design/analysis-stage.md §9.1 "고정").

해시 목록 `data/eval/analysis-pilot/FROZEN.json` 에 남기는 것:
- 손으로 만든 파일마다 sha256 — 재료 폴더 전부(material.json·README.thin.md·public-behavior/·starter-thin/·mutants/·validity.json·
  questions/·synonyms.json)와 지시문(prompts/)
- 설계 문서 커밋, 도구 커밋(진행자 함수·채점·판정 계산 코드) — 코드도 판정에 영향을 준다. 도구 경로에 커밋 안 된 변경이 있으면 고정하지 않는다.
- 돌리는 순서와 미리 정한 숫자 기준 (#151 검토 4차)

고정 전 점검 — 재료마다 손 재료 점검(`check_material`)이 깨끗하고, 유효성 검사(`validity.json`)가 쓸 수 있다고 나오고,
마지막 확인용 질문 목록(`questions/check*.result.json`)이 기준을 넘어야 고정한다.
첫 풀이 뒤에 움직일 수 있는 것은 미리 정한 절차뿐이다 — 그때는 `amend` 로 새 해시와 이유를 덧붙인다.

  uv run python -m eval.pilot.freeze write            (처음 한 번)
  uv run python -m eval.pilot.freeze add-material <mid> (교체·추가한 재료 — 그 재료의 첫 풀이 전에)
  uv run python -m eval.pilot.freeze amend <파일> --reason "…" (정해진 절차로 고친 파일)
  uv run python -m eval.pilot.freeze check            (지금 파일·코드가 고정과 같은지)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from eval.pilot.llm_facilitator import TASK as FACILITATOR_TASK
from eval.pilot.model import DATA, ROOT, check_material, load_material
from eval.pilot.simulate import (
    CORRECT_MIN,
    MIN_FACT_QUESTIONS,
    MIN_VAGUE_QUESTIONS,
    OFFICIAL,
    ONE_AT_A_TIME_MAX,
    VAGUE_OPEN_MAX,
)
from eval.pilot.validity import MIN_MEASURED
from task_spec.prompting import PromptRegistry

FROZEN = DATA / "FROZEN.json"
DESIGN = "docs/design/analysis-stage.md"
TOOLS = (
    "eval/pilot",
    "src/task_spec/starter/answer_rule.py",
    "src/task_spec/starter/facilitator.py",  # Answer·지금 규칙 한도 (판 3)
    "src/task_spec/starter/verify",  # 채점 — Gradle 실행·JUnit 결과
    "src/task_spec/starter/templates.py",
    "src/task_spec/spec/feature.py",
    "src/task_spec/schemas/pilot.py",
    "src/task_spec/llm/client.py",  # 진행자 LLM 호출 (Gemini 설정 허용)
    "src/task_spec/llm/openai_compat.py",
    "prompts/expand_match_terms",
    "prompts/facilitator_choose",  # 진행자 LLM — 응시 플랫폼 흉내 (판 1·3·4)
)
ORDER = "gate-first: 판 2 opus 2번 → 관문 → 판 0 → (라) → 판 2 나머지 → 판 1·3·4"
CRITERIA = {
    "min_measured_needs_asking": MIN_MEASURED,
    "check_questions_correct_min": CORRECT_MIN,
    "check_questions_one_at_a_time_max": ONE_AT_A_TIME_MAX,
    "vague_questions_concrete_open_max": VAGUE_OPEN_MAX,
    "min_fact_questions": MIN_FACT_QUESTIONS,
    "min_vague_questions": MIN_VAGUE_QUESTIONS,
}


def judged_version() -> int:
    """진행자 프롬프트의 지금 판 — 질문 목록 점검은 이 판으로 LLM 이 판정한 결과만 본다."""
    return PromptRegistry(ROOT / "prompts").current_version(FACILITATOR_TASK)


def _official_problems(qdir: Path, version: int, once: bool = False) -> list[str]:
    """공식 판정 기록(questions/official.json) — 지금 판으로 돌린 마지막 결과만 본다. 대상은 돌리기 전에 적었다 (simulate --official).
    한 번 쓴 재료(once)는 종류마다 선언이 하나뿐이어야 한다 (§9.1 과적합 막기 — 다시 재지 않는다)."""
    path = qdir / OFFICIAL
    log = json.loads(path.read_text("utf-8")) if path.exists() else []
    out: list[str] = []
    for kind, what in (("general", "확인용 질문 목록"), ("vague", "모호한 질문 목록")):
        declared = [r for r in log if r["kind"] == kind and r["stage"] == "declared"]
        if once and len(declared) > 1:
            out.append(f"{what} 공식 판정을 {len(declared)}번 선언함 — 한 번 쓴 재료는 한 번만")
        rows = [r for r in log if r["kind"] == kind and r["version"] == version]
        if not rows:
            out.append(
                f"{what} 공식 판정 없음 — simulate --official {kind} --llm (진행자 판 v{version}) 먼저"
            )
            continue
        last = rows[-1]
        if last["stage"] != "result":
            out.append(f"{what} 공식 판정이 끝나지 않음: {'+'.join(last['sets'])}")
        elif not last["passed"]:
            out.append(
                f"{what}이 기준에 못 미침 (질문 수가 모자라면 목록을 더 받아 합친다): {last['result']}"
            )
    return out


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def file_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            out[p.relative_to(DATA).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def material_hashes(mid: str) -> dict[str, str]:
    root = DATA / "materials" / mid
    if not (root / "material.json").exists():
        sys.exit(f"재료가 없음: {root}")
    return file_hashes(root)


def code_state() -> dict[str, Any]:
    dirty = _git("status", "--porcelain", "--", *TOOLS, DESIGN)
    if dirty:
        sys.exit(f"도구·설계 문서에 커밋 안 된 변경이 있어 고정하지 않음:\n{dirty}")
    return {
        "head": _git("rev-parse", "HEAD"),
        "design_commit": _git("log", "-1", "--format=%H", "--", DESIGN),
        "tools_commit": _git("log", "-1", "--format=%H", "--", *TOOLS),
    }


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def preflight(mid: str) -> list[str]:
    """고정 전 점검 (§9.1 재료). 빈 목록이면 고정할 수 있다."""
    m = load_material(mid)
    problems = [f"재료 점검: {x}" for x in check_material(m)]
    vpath = m.dir / "validity.json"
    if not vpath.exists():
        problems.append("validity.json 없음 — eval.pilot.validity 먼저")
    else:
        v = json.loads(vpath.read_text("utf-8"))
        if not v.get("baseline_ok"):
            problems.append("유효성: 정답 구현이 ② 를 다 통과하지 못함")
        elif not v.get("usable"):
            problems.append(
                f"유효성: 묻지 않고는 모르는 측정 요구 {v.get('measured_unmentioned')} — {MIN_MEASURED}개 미만이라 쓰지 않는 재료"
            )
    problems += _official_problems(m.dir / "questions", judged_version(), m.fact_tuning == "once")
    return problems


def _require_preflight(mids: list[str]) -> None:
    bad = {mid: ps for mid in mids if (ps := preflight(mid))}
    if bad:
        lines = [f"{mid}: {p}" for mid, ps in bad.items() for p in ps]
        sys.exit("고정 전 점검을 통과하지 못해 고정하지 않음:\n" + "\n".join(lines))


def write() -> None:
    if FROZEN.exists():
        sys.exit(
            f"이미 고정됨: {FROZEN} — 첫 풀이 뒤에는 목록 전체를 바꾸지 않는다 (amend·add-material 만)"
        )
    mids = sorted(p.parent.name for p in (DATA / "materials").glob("*/material.json"))
    _require_preflight(mids)
    data: dict[str, Any] = {
        "frozen_at": _now(),
        # §9.1 순서 (#151 검토 4차): 판 2 opus 를 판 0 보다 먼저 돌린다 — 판 2 결과를 근거로 한 고침은 없다
        "order": ORDER,
        "criteria": CRITERIA,
        # 진행자 — 응시 플랫폼처럼 LLM 이 항목을 고른다 (2026-11-13 사용자)
        "facilitator": {"judge": "llm", "prompt": f"{FACILITATOR_TASK}@{judged_version()}"},
        "code": code_state(),
        "prompts": file_hashes(DATA / "prompts"),
        "materials": {mid: material_hashes(mid) for mid in mids},
        "amendments": [],
    }
    FROZEN.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(
        f"고정: {FROZEN} — 재료 {mids}, 지시문 {len(data['prompts'])}개, 도구 {data['code']['tools_commit'][:7]}"
    )


def _load() -> dict[str, Any]:
    if not FROZEN.exists():
        sys.exit("아직 고정하지 않음 — freeze write 먼저")
    return json.loads(FROZEN.read_text("utf-8"))


def add_material(mid: str) -> None:
    data = _load()
    if mid in data["materials"]:
        sys.exit(f"이미 고정한 재료: {mid}")
    _require_preflight([mid])
    data["materials"][mid] = material_hashes(mid)
    data["amendments"].append({"t": _now(), "kind": "add-material", "mid": mid})
    FROZEN.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"재료 추가 고정: {mid}")


def amend(path: str, reason: str) -> None:
    data = _load()
    p = (DATA / path) if not Path(path).is_absolute() else Path(path)
    rel = p.resolve().relative_to(DATA.resolve()).as_posix()
    data["amendments"].append(
        {
            "t": _now(),
            "kind": "amend",
            "file": rel,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "reason": reason,
        }
    )
    FROZEN.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"고친 기록: {rel} — {reason}")


def expected(data: dict[str, Any]) -> dict[str, str]:
    """고정 + 정해진 절차로 고친 기록을 반영한 지금 기대 해시."""
    want = dict(data["prompts"])
    for files in data["materials"].values():
        want.update(files)
    for a in data["amendments"]:
        if a["kind"] == "amend":
            want[a["file"]] = a["sha256"]
    return want


def check() -> int:
    data = _load()
    want = expected(data)
    have = file_hashes(DATA / "prompts")
    for mid in data["materials"]:
        have.update(file_hashes(DATA / "materials" / mid))
    changed = sorted(k for k in want if have.get(k) != want[k])
    extra = sorted(k for k in have if k not in want)
    code = data["code"]
    moved = _git("diff", "--name-only", code["tools_commit"], "HEAD", "--", *TOOLS)
    design = _git("diff", "--name-only", code["design_commit"], "HEAD", "--", DESIGN)
    for title, items in (
        ("바뀐 파일", changed),
        ("고정에 없는 파일", extra),
        ("고정 뒤 바뀐 도구", moved.split()),
        ("고정 뒤 바뀐 설계 문서", design.split()),
    ):
        if items:
            print(f"{title}: {items}")
    ok = not (changed or extra or moved or design)
    print("고정과 같음" if ok else "고정과 다름")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("write")
    am = sub.add_parser("add-material")
    am.add_argument("mid")
    fx = sub.add_parser("amend")
    fx.add_argument("path")
    fx.add_argument("--reason", required=True)
    sub.add_parser("check")
    a = ap.parse_args()
    if a.cmd == "write":
        write()
    elif a.cmd == "add-material":
        add_material(a.mid)
    elif a.cmd == "amend":
        amend(a.path, a.reason)
    else:
        sys.exit(check())


if __name__ == "__main__":
    main()
