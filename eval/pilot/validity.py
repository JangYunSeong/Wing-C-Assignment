"""재료 유효성 — 요구마다 그 요구 하나만 어기는 돌연변이 구현이 ② 에서 떨어지는가 (docs/design/analysis-stage.md §9.1 재료).

#151 검토 4차(2026-11-13): 정답 구현이 통과하는 것만 보고 재료를 골랐더니, 시드가 모두 같은 시각이라 정렬·시각 필터 테스트가
어떤 구현에도 참인 재료가 들어왔다. 그래서 측정할 요구마다 돌연변이를 손으로 만들어, ② 에서 그 요구를 보는 메서드가 떨어지는지 본다.
떨어지지 않는(또는 돌연변이가 없는) 요구는 측정에서 빼고 `validity.json` 에 남긴다 — 고정 전에.

재료 폴더:
  mutants.json            {"<이름>": {"req": "r1", "note": "오름차순"}}
  mutants/<이름>/<경로>    정답 구현(알파 overlays/d1) 위에 덮어쓸 파일
  material.json method_req ② 메서드("<클래스>.<메서드>") → 요구

  uv run python -m eval.pilot.validity --mid <재료>
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from eval.pilot.grade import SuiteRun, overlay, run_suite, solution
from eval.pilot.model import DATA, Material, load_material
from task_spec.starter.templates import select_template

SKIP = ("build", ".gradle", ".idea", "out", "bin")
# 묻지 않고는 모르는 요구 중 ② 로 재는 것이 이보다 적으면 재료로 쓰지 않는다 (§9.1, #151 검토 4차)
MIN_MEASURED = 2


def _workspace(m: Material, dest: Path, mutant: Path | None) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(m.alpha_dir / "candidate", dest, ignore=shutil.ignore_patterns(*SKIP))
    shutil.copytree(m.alpha_dir / "overlays" / "d1", dest, dirs_exist_ok=True)  # 정답 구현
    if mutant is not None:
        shutil.copytree(mutant, dest, dirs_exist_ok=True)
    return dest


def failed_methods(run: SuiteRun) -> list[str]:
    return [f"{x.cls}.{x.method}" for x in run.methods if x.group == "public" and not x.ok]


def judge(
    m: Material, baseline: SuiteRun, runs: dict[str, SuiteRun], meta: dict[str, Any]
) -> dict[str, Any]:
    """돌연변이마다 떨어졌나 — 그 요구를 보는 메서드 하나 이상이 떨어지면 잡힘. 기준(정답)이 다 통과해야 판정한다.
    요구는 그 돌연변이가 모두 잡혀야 측정하고, 측정 요구 중 묻지 않고는 모르는 것(`Material.needs_asking`)이
    MIN_MEASURED 개 미만이면 재료로 쓰지 않는다."""
    reqs = sorted({r for rs in m.method_req.values() for r in rs})
    base_ok = (
        baseline.setup == "ok"
        and not failed_methods(baseline)
        and any(x.group == "public" for x in baseline.methods)
    )
    rows: dict[str, Any] = {}
    for name, run in runs.items():
        want = meta[name]["req"]
        failed = failed_methods(run)
        own = [f for f in failed if want in m.method_req.get(f, [])]
        other = [f for f in failed if want not in m.method_req.get(f, [])]
        rows[name] = {
            "req": want,
            "note": meta[name].get("note", ""),
            "setup": run.setup,
            "killed": bool(own),
            "own_failed": own,
            "other_failed": other,
        }
    by_req: dict[str, list[bool]] = {}
    for v in rows.values():
        by_req.setdefault(v["req"], []).append(v["killed"])
    # 요구의 돌연변이가 하나도 빠짐없이 잡혀야 측정한다 — 하나라도 통과하면 그 요구를 테스트가 다 보지 못하는 것
    measured = sorted(r for r, ks in by_req.items() if ks and all(ks)) if base_ok else []
    hidden = [r for r in measured if m.needs_asking(r)]  # 묻지 않고는 모르는 요구
    return {
        "baseline_ok": base_ok,
        "usable": len(hidden) >= MIN_MEASURED,
        "measured_unmentioned": hidden,
        "baseline_failed": failed_methods(baseline),
        "mutants": rows,
        "measured": measured,
        "excluded": [r for r in reqs if r not in measured],
    }


def check(mid: str) -> dict[str, Any]:
    m = load_material(mid)
    t = select_template("sw.backend")
    assert t is not None
    meta = json.loads((m.dir / "mutants.json").read_text("utf-8"))
    pub2 = overlay("public_test", m.dir / "public-behavior")
    work = DATA / "validity-work" / mid
    work.mkdir(parents=True, exist_ok=True)

    def grade(label: str, mutant: Path | None) -> SuiteRun:
        ws = _workspace(m, work / f"ws-{label}", mutant)
        return run_suite(t, work / f"run-{label}", (solution(ws, label), pub2), pub2, None)

    baseline = grade("baseline", None)
    runs = {name: grade(name, m.dir / "mutants" / name) for name in meta}
    out = judge(m, baseline, runs, meta)
    (m.dir / "validity.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True)
    a = ap.parse_args()
    out = check(a.mid)
    print(
        json.dumps(
            {
                k: out[k]
                for k in ("baseline_ok", "usable", "measured", "measured_unmentioned", "excluded")
            },
            ensure_ascii=False,
        )
    )
    for name, row in out["mutants"].items():
        print(
            f"  {name} ({row['req']}, {row['note']}): {'잡힘' if row['killed'] else '못 잡음'} {row['own_failed'] or ''} {('· 다른 요구도 떨어짐 ' + str(row['other_failed'])) if row['other_failed'] else ''}"
        )


if __name__ == "__main__":
    main()
