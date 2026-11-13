"""판별 작업 폴더와 풀이 지시문 (docs/design/analysis-stage.md §9.1 풀이 판).

작업 폴더는 레포 밖 `<RUNS>/ws/<label>` — 알파의 응시용 프로젝트(candidate/) 사본.
- README: 판 3 은 알파 그대로(지금 판), 나머지는 재료의 `README.thin.md`(B1) + 판별 진행 방식 절 (+ 판 0 은 사실 두 층).
- 진행자: 판 1·4 는 §4.6 답 규칙(한도 없음), 판 3 은 지금 규칙 모드(질문·자료 한도는 알파 명세 값). 판 0·2 는 진행자 없음.
- 지시문: 고정 파일 `data/eval/analysis-pilot/prompts/solver.md` 에 작업 폴더·Gradle·진행자 안내·예산을 채운다.

  uv run python -m eval.pilot.prepare --mid <재료> --arm 1 --models opus sonnet haiku --runs 1 2 [--budget 120] [--synonyms]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path

from eval.pilot.facilitator import FAC, LOGS, SESSIONS, Session, client_script
from eval.pilot.model import (
    ARMS,
    DATA,
    MODELS,
    RUNS,
    Arm,
    Material,
    facilitator_items,
    legacy_limits,
    load_material,
    run_label,
    stage1_materials,
)
from task_spec.starter.verify import tools

PROMPTS = DATA / "prompts"
GRADLE_HOME = RUNS / "gradle-home"
SKIP = ("build", ".gradle", ".idea", "out", "bin")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(name: str) -> str:
    return (PROMPTS / name).read_text("utf-8").strip()


def readme(m: Material, arm: Arm) -> str:
    """판별 README — 판 3 은 알파 그대로, 나머지는 B1 얇은 README + 진행 방식 (+ 판 0 의 사실 두 층)."""
    if arm.readme == "current":
        return (m.alpha_dir / "candidate" / "README.md").read_text("utf-8")
    body = (m.dir / "README.thin.md").read_text("utf-8").rstrip()
    titles = "\n".join(f"  - {x.title}" for x in stage1_materials(m.task()))
    process = {
        "new": _text("process-ask.md").replace("{MATERIALS}", titles),
        None: _text("process-none.md"),
    }[arm.ask]
    out = [body, "", "## 진행 방식", "", process]
    if arm.facts_in_readme:
        out += ["", "## 요청자에게 확인한 내용", "", *(f"- {f.topic}: {f.answer}" for f in m.facts)]
    return "\n".join(out) + "\n"


def solver_prompt(ws: Path, arm: Arm, budget: int | None) -> str:
    ask = _text("ask-tool.md") if arm.ask else ""
    extra = _text("arm4.md") if arm.n == 4 else ""
    limit = (
        f"- 이번 풀이의 예산은 도구 호출 {budget}번입니다. 그 안에서 마무리하세요."
        if budget
        else ""
    )
    return (
        _text("solver.md")
        .replace("{WS}", str(ws))
        .replace("{GRADLE}", str(GRADLE_HOME))
        .replace("{ASK}", ask)
        .replace("{EXTRA}", extra)
        .replace("{BUDGET}", limit)
        .replace("\n\n\n", "\n\n")
    )


def _ensure_gradle_home() -> None:
    if GRADLE_HOME.exists():
        return
    src = tools.gradle_user_home()
    if not src.exists():
        sys.exit(f"오프라인 Gradle 캐시가 없음: {src}")
    shutil.copytree(src, GRADLE_HOME, symlinks=True)


def apply_starter_thin(m: Material, ws: Path) -> list[str]:
    """B1 판(0·1·2·4) — 시작 코드의 계약 설명 주석을 모양만 남긴 손 사본(재료 `starter-thin/`)으로 바꾼다.
    B1 에서는 plan_contract 가 모양만 적으므로 시작 코드 주석에도 규칙이 없다 [확정: 2026-11-13 사용자]. 있던 파일만 바꾼다."""
    root = m.dir / "starter-thin"
    done: list[str] = []
    for p in sorted(root.rglob("*")) if root.exists() else []:
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        target = ws / rel
        if not target.exists():
            sys.exit(f"starter-thin 의 {rel} 이 응시용 프로젝트에 없음 — 바꾸기만 한다")
        shutil.copy2(p, target)
        done.append(rel.as_posix())
    return done


def prepare_one(
    m: Material, arm_n: int, model: str, run: int, budget: int | None, synonyms: bool
) -> Path:
    arm = ARMS[arm_n]
    label = run_label(m.mid, arm_n, model, run)
    ws = RUNS / "ws" / label
    if ws.exists():
        sys.exit(f"이미 있음: {ws} (다시 하려면 직접 지운 뒤)")
    shutil.copytree(m.alpha_dir / "candidate", ws, ignore=shutil.ignore_patterns(*SKIP))
    (ws / "README.md").write_text(readme(m, arm), "utf-8")
    if arm.readme == "thin":
        apply_starter_thin(m, ws)
    session = None
    if arm.ask is not None:
        task = m.task()
        q, r = legacy_limits(task) if arm.ask == "legacy" else (None, None)
        session = Session(
            label=label,
            token=secrets.token_hex(16),
            mode=arm.ask,
            items=facilitator_items(m, arm.ask, m.synonyms() if synonyms else None),
            request=m.request if arm.ask == "new" else "",
            materials=stage1_materials(task),
            max_questions=q,
            max_requests=r,
        )
        SESSIONS.mkdir(parents=True, exist_ok=True)
        (SESSIONS / f"{session.token}.json").write_text(session.model_dump_json(indent=1), "utf-8")
        (LOGS / f"{label}.jsonl").unlink(missing_ok=True)
        ask = ws / "ask"
        ask.write_text(client_script(session.token), "utf-8")
        ask.chmod(ask.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    prompt = solver_prompt(
        ws, arm, budget if arm_n != 0 else None
    )  # 판 0 은 예산의 기준 — 예산 없음
    out = DATA / "runs" / label
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.txt").write_text(prompt, "utf-8")
    meta = {
        "label": label,
        "mid": m.mid,
        "arm": arm_n,
        "model": model,
        "run": run,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "workspace": str(ws),
        "budget_tool_calls": budget if arm_n != 0 else None,
        "synonyms": synonyms,
        "material_sha256": sha256(m.dir / "material.json"),
        "readme_sha256": hashlib.sha256((ws / "README.md").read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "facilitator": None
        if session is None
        else {
            "mode": session.mode,
            "token_file": str(SESSIONS / f"{session.token}.json"),
            "log": str(LOGS / f"{label}.jsonl"),
        },
    }
    (out / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    return out / "prompt.txt"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True)
    ap.add_argument("--arm", type=int, required=True, choices=sorted(ARMS))
    ap.add_argument("--models", nargs="+", default=list(MODELS), choices=MODELS)
    ap.add_argument("--runs", nargs="+", type=int, default=[1])
    ap.add_argument(
        "--budget", type=int, default=None, help="도구 호출 예산 (판 0 opus 의 2배 — §9.1)"
    )
    ap.add_argument(
        "--synonyms",
        action="store_true",
        help="진행자 쪽 고치기 — 고정한 동의어 목록을 매칭 낱말에 더함",
    )
    a = ap.parse_args()
    m = load_material(a.mid)
    _ensure_gradle_home()
    FAC.mkdir(parents=True, exist_ok=True)
    for model in a.models:
        for run in a.runs:
            print(prepare_one(m, a.arm, model, run, a.budget, a.synonyms))


if __name__ == "__main__":
    main()
