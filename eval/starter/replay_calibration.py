"""조절값 보정 오프라인 재생 (#301 평가 1단계) — 저장된 알파 빌드의 흔한 구현·허용 구현·테스트로 보정이 창을 찾는지 본다 (LLM 호출 없음).

알파 폴더(eval/starter/run_alpha 출력)의 `overlays/{starter,hidden,public,<참조 구현 라벨>}` 과 숨은 계획(`contract.orig.json` 이 있으면
원래 계획, 없으면 `contract.json`)을 읽어, 빌드의 보정(build._calibrate)과 같은 절차(starter/calibrate.calibrate_refs — 검증기
verify_variants)를 사유마다 돈다. 보정할 숨긴 조절값은 --template-version 판의 매니페스트에서 (기본 4 — tunes 가 있는 첫 판. 알파가
v2·v3 로 돌았어도 v4 는 매니페스트만 다르다). 화면에 출력하고, --json 이 있으면 결과를 그 파일에 쓴다 (git 밖 data/eval/build/…).
알파 폴더에는 쓰지 않는다.

재료(나열한 조절값·사유 묶음): 공개 재료는 재료 폴더(INCIDENTS_DIR)에서 task.json 의 source_incident 로 찾는다. 회사 재료(co:)는 DB 에만
있으니 --incident <재료 JSON(PublicIncident)> 으로 준다 — 없으면 나열 없음·묶음 없음으로 돌리고 그렇게 알린다.

사용 (알파 폴더가 있는 서버에서, 레포 루트):
  uv run python eval/starter/replay_calibration.py data/eval/build/<알파> --work <작업 폴더> [--incident co.json] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from task_spec.config import Settings
from task_spec.schemas import FeatureSeed, GeneratedFile, TaskItem
from task_spec.schemas.build import HiddenPlan
from task_spec.schemas.incident import PublicIncident
from task_spec.spec.incidents import incident_by_id, load_incidents
from task_spec.starter import calibrate as calib
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import Template, load_template, select_template
from task_spec.starter.verify import verifier_for

KINDS = {"starter": "starter", "hidden": "hidden", "public": "public_test"}
NO_REFS = ("conform",)  # 오버레이 폴더 중 참조 구현이 아닌 것


def _overlay(root: Path, key: str, kind: str, label: str = "") -> Overlay | None:
    d = root / "overlays" / key
    if not d.is_dir():
        return None
    files = [
        GeneratedFile(path=p.relative_to(d).as_posix(), content=p.read_text("utf-8"))
        for p in sorted(d.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]
    return Overlay(kind=kind, files=tuple(files), label=label) if files else None  # type: ignore[arg-type]


def _refs(root: Path) -> list[Overlay]:
    """참조 구현 — 오버레이 폴더 이름이 라벨 (naive… 는 흔한 구현, 그 밖은 허용 설계)."""
    out: list[Overlay] = []
    base = root / "overlays"
    for d in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        if d.name in KINDS or d.name in NO_REFS:
            continue
        kind = "ref_naive" if d.name.startswith("naive") else "ref_accepted"
        if (o := _overlay(root, d.name, kind, d.name)) is not None:
            out.append(o)
    return out


def _plan(root: Path) -> HiddenPlan:
    src = root / "contract.orig.json"
    if not src.is_file():
        src = root / "contract.json"
    return HiddenPlan.model_validate(json.loads(src.read_text("utf-8"))["hidden_plan"])


def _seed(task: TaskItem, incidents_dir: Path, incident: Path | None) -> FeatureSeed | None:
    if incident is not None:
        return PublicIncident.model_validate(json.loads(incident.read_text("utf-8"))).feature
    found = incident_by_id(load_incidents(incidents_dir), task.source_incident)
    return found.feature if found is not None else None


async def replay(
    alpha: Path,
    work: Path,
    *,
    version: int = 4,
    incidents_dir: Path = Path("incidents/public_v1"),
    incident: Path | None = None,
    make_verifier: Callable[[Template, Path], Any] = verifier_for,
) -> list[dict[str, Any]]:
    """사유마다 보정 한 번 — 결과(상태·고른 값·창·조합별 결과) 목록."""
    raw = json.loads((alpha / "task.json").read_text("utf-8"))
    task = TaskItem.model_validate(raw.get("task", raw))
    starter, hidden, public = (_overlay(alpha, k, v) for k, v in KINDS.items())
    refs = _refs(alpha)
    if starter is None or hidden is None or public is None or not refs:
        print(
            f"== {alpha.name}: overlays 의 starter·hidden·public·참조 구현이 없음 — 빌드가 거기까지 가지 않은 실행"
        )
        return []
    sub = (task.source_incident or "").split(":")[1] if task.source_incident else ""
    current = select_template(sub)
    assert current is not None, f"직군 {sub} 의 템플릿이 없음"
    template = load_template(current.manifest.template_id, version)
    m = template.manifest
    seed = _seed(task, incidents_dir, incident)
    listed = (seed.harness or ()) if seed is not None else ()
    family = seed.reasons if seed is not None else ()
    plan = _plan(alpha)
    print(
        f"== {alpha.name} (보정 칸: {m.ref}, 재료 {task.source_incident}"
        + (f", 나열 {list(listed)}" if listed else "")
        + ("" if seed is not None else " — 재료를 못 찾음: 나열 없음·묶음 없음으로 돎 (--incident)")
        + ")"
    )
    naive = [o.label for o in refs if o.kind == "ref_naive"]
    by_reason: dict[str, list[str]] = {}
    for c in plan.cases:
        if calib.knobs(c.intended_reason, m, plan, listed) is not None:
            by_reason.setdefault(c.intended_reason, []).append(c.case_id)
    if not by_reason or not naive:
        print("  보정할 케이스 없음 (사유에 보정할 숨긴 조절값이 없거나 흔한 구현이 없음)")
        return []
    (work / alpha.name).mkdir(parents=True, exist_ok=True)  # verify() 를 먼저 부르지 않으므로
    verifier = make_verifier(template, work / alpha.name)
    results: list[dict[str, Any]] = []
    for reason, cases in by_reason.items():
        k = calib.knobs(reason, m, plan, listed)
        assert k is not None
        request = [(label, c) for label in naive for c in cases]
        cal = await calib.calibrate_refs(
            verifier.verify_variants, starter, refs, public, hidden, plan, m, k, request, family
        )
        print(f"  {reason} — 케이스 {', '.join(cases)} · 결과 {cal.status}")
        for t in cal.trials:
            values = " ".join(f"{n}={v}" for n, v in t.values.items())
            print(
                f"    {values}: 흔한 구현 {'떨어짐' if t.caught else '통과'} · 허용 구현 {'모두 통과' if t.clean else '떨어짐'}"
            )
        if cal.status == "calibrated":
            print(f"  [고른 값] {cal.flag()}")
        elif cal.status == "no_window":
            print(f"  [재료 판정] {cal.verdict()}")
        results.append({"alpha": alpha.name, **cal.row()})
    return results


async def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("alphas", nargs="+", type=Path)
    ap.add_argument("--work", type=Path, required=True, help="검증 작업 폴더 (git 밖)")
    ap.add_argument(
        "--template-version", type=int, default=4, help="보정 칸을 읽을 템플릿 판 (기본 4)"
    )
    ap.add_argument(
        "--incident", type=Path, help="회사 재료(co:) JSON — 공개 재료는 재료 폴더에서 찾는다"
    )
    ap.add_argument("--json", type=Path, help="결과를 쓸 파일 (git 밖)")
    a = ap.parse_args()
    s = Settings()  # type: ignore[call-arg]
    results: list[dict[str, Any]] = []
    for alpha in a.alphas:
        results += await replay(
            alpha,
            a.work,
            version=a.template_version,
            incidents_dir=s.incidents_dir,
            incident=a.incident,
        )
    if a.json is not None:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")
        print(f"결과: {a.json}")


if __name__ == "__main__":
    asyncio.run(main())
