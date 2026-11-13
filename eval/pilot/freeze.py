"""고정 — 판 0 을 돌리기 전에 한 번 (docs/design/analysis-stage.md §9.1 "고정").

해시 목록 `data/eval/analysis-pilot/FROZEN.json` 에 남기는 것:
- 손으로 만든 파일마다 sha256 — 재료(material.json·README.thin.md·public-behavior/·synonyms.json)와 지시문(prompts/)
- 설계 문서 커밋, 도구 커밋(진행자 함수·채점·판정 계산 코드) — 코드도 판정에 영향을 준다. 도구 경로에 커밋 안 된 변경이 있으면 고정하지 않는다.

판 0 뒤에 움직일 수 있는 것은 미리 정한 절차뿐이다 — 그때는 `amend` 로 새 해시와 이유를 덧붙인다.

  uv run python -m eval.pilot.freeze write            (처음 한 번)
  uv run python -m eval.pilot.freeze add-material <mid> (교체·추가한 재료 — 그 재료의 판 0 전에)
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

from eval.pilot.model import DATA, ROOT

FROZEN = DATA / "FROZEN.json"
DESIGN = "docs/design/analysis-stage.md"
TOOLS = (
    "eval/pilot",
    "src/task_spec/starter/answer_rule.py",
    "src/task_spec/spec/feature.py",
    "src/task_spec/schemas/pilot.py",
    "prompts/expand_match_terms",
)


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


def write() -> None:
    if FROZEN.exists():
        sys.exit(
            f"이미 고정됨: {FROZEN} — 판 0 뒤에는 목록 전체를 바꾸지 않는다 (amend·add-material 만)"
        )
    mids = sorted(p.parent.name for p in (DATA / "materials").glob("*/material.json"))
    data: dict[str, Any] = {
        "frozen_at": _now(),
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
