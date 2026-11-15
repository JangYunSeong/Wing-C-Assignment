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
from task_spec.starter.variants import verdict_of

SKIP = ("build", ".gradle", ".idea", "out", "bin")
# 묻지 않고는 모르는 요구 중 ② 로 재는 것이 이보다 적으면 재료로 쓰지 않는다 (§9.1, #151 검토 4차)
MIN_MEASURED = 2
_VERDICT_NAME = {"caught": "잡힘", "survived": "못 잡음", "invalid": "무효"}


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
    """돌연변이마다 잡힘·살아남음·무효 — 판정은 빌드의 관문 ⑩ 과 같은 `variants.verdict_of` (#151): 그 요구를 보는 메서드 하나 이상이
    떨어지면 잡힘, 공개 메서드가 모두 통과하면 살아남음, 컴파일·기동 실패·결과 없음·다른 요구의 메서드만 떨어짐은 무효.
    기준(정답)이 다 통과해야 판정한다. 요구는 유효한 돌연변이가 하나 이상이고 모두 잡혀야 측정하고, 측정 요구 중 묻지 않고는
    모르는 것(`Material.needs_asking`)이 MIN_MEASURED 개 미만이면 재료로 쓰지 않는다."""
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
        ran = any(x.group == "public" for x in run.methods)
        verdict, detail = verdict_of(run.setup, ran, failed, own)
        rows[name] = {
            "req": want,
            "note": meta[name].get("note", ""),
            "setup": run.setup,
            "verdict": verdict,
            "detail": detail,
            "own_failed": own,
            "other_failed": other,
        }
    by_req: dict[str, list[str]] = {}
    for v in rows.values():
        by_req.setdefault(v["req"], []).append(v["verdict"])
    why = {r: _excluded_why(by_req.get(r, [])) for r in reqs}
    # 유효한 돌연변이가 하나 이상이고 모두 잡혀야 측정한다 — 하나라도 살아남으면 그 요구를 테스트가 다 보지 못하는 것.
    # 무효(컴파일·기동 실패·다른 요구만 떨어짐)는 셈에서 뺀다 — 빌드의 관문 ⑩ 과 같은 판정 (#151)
    measured = sorted(r for r in reqs if why[r] is None) if base_ok else []
    hidden = [r for r in measured if m.needs_asking(r)]  # 묻지 않고는 모르는 요구
    return {
        "baseline_ok": base_ok,
        "usable": len(hidden) >= MIN_MEASURED,
        "measured_unmentioned": hidden,
        "baseline_failed": failed_methods(baseline),
        "mutants": rows,
        "measured": measured,
        "excluded": [r for r in reqs if r not in measured],
        "excluded_why": {
            r: (why[r] or "정답 구현이 공개 테스트를 다 통과하지 못함")
            for r in reqs
            if r not in measured
        },
    }


def _excluded_why(verdicts: list[str]) -> str | None:
    """요구를 측정에서 빼는 이유 — 없으면 측정한다."""
    if not verdicts:
        return "돌연변이 없음"
    if "survived" in verdicts:
        return "살아남은 돌연변이 있음"
    if "caught" not in verdicts:
        return "유효한 돌연변이 없음 (모두 무효)"
    return None


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
                for k in (
                    "baseline_ok",
                    "usable",
                    "measured",
                    "measured_unmentioned",
                    "excluded_why",
                )
            },
            ensure_ascii=False,
        )
    )
    for name, row in out["mutants"].items():
        print(
            f"  {name} ({row['req']}, {row['note']}): {_VERDICT_NAME[row['verdict']]} {row['own_failed'] or ''} "
            f"{('· 다른 요구도 떨어짐 ' + str(row['other_failed'])) if row['other_failed'] else ''}"
            f"{(' — ' + row['detail']) if row['detail'] else ''}"
        )


if __name__ == "__main__":
    main()
