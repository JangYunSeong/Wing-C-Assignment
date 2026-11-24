"""손 구현 관문 오프라인 재생 (#256 3단계) — 저장된 알파 빌드의 테스트에 재료 손 구현을 돌려 관문 판정만 본다 (LLM 호출 없음).

알파 폴더(eval/starter/run_alpha 출력)의 `overlays/{starter,hidden,public}` 과 숨은 계획(`contract.orig.json` — 채점에서 뺀 케이스가
있으면 원래 계획, 없으면 `contract.json`)을 읽어, 빌드의 손 구현 관문(build._hand_gate)과 같은 검증(verify_variants)·판정(judge_hands)을
돈다. 저장된 숨은 테스트는 뺀 케이스의 파일이 이미 없어 그 케이스는 판정하지 않는다. 화면 출력만 — 알파 폴더에 쓰지 않는다.

사용 (worktree 코드를 레포 루트에서 — 설정 파일 때문에. 상대 경로 설정은 worktree 로):
  INCIDENTS_DIR=<wt>/incidents/public_v1 TEMPLATES_DIR=<wt>/templates PYTHONPATH=<wt> \\
      uv run --project <wt> python <wt>/eval/starter/replay_hand_gate.py data/eval/build/<알파> [...] --work <작업 폴더>
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from task_spec.config import Settings
from task_spec.schemas import GeneratedFile, TaskItem
from task_spec.schemas.build import HiddenPlan
from task_spec.spec.incidents import load_interfaces
from task_spec.starter.hand_refs import judge_hands, load_references
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import select_template
from task_spec.starter.verify import verifier_for

KINDS = {"starter": "starter", "hidden": "hidden", "public": "public_test"}


def _overlay(root: Path, key: str) -> Overlay | None:
    d = root / "overlays" / key
    if not d.is_dir():
        return None
    files = [
        GeneratedFile(path=p.relative_to(d).as_posix(), content=p.read_text("utf-8"))
        for p in sorted(d.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    ]
    return Overlay(kind=KINDS[key], files=tuple(files)) if files else None  # type: ignore[arg-type]


def _plan(root: Path) -> HiddenPlan:
    src = root / "contract.orig.json"
    if not src.is_file():
        src = root / "contract.json"
    return HiddenPlan.model_validate(json.loads(src.read_text("utf-8"))["hidden_plan"])


async def replay(alpha: Path, work: Path) -> None:
    s = Settings()  # type: ignore[call-arg]
    raw = json.loads((alpha / "task.json").read_text("utf-8"))
    task = TaskItem.model_validate(raw.get("task", raw))
    incident_id = task.source_incident or ""
    refs = load_references(s.incidents_dir).get(incident_id)
    if refs is None:
        print(f"== {alpha.name}: 재료 {incident_id} 의 손 구현 묶음이 없음")
        return
    refs = refs.checked_against(load_interfaces(s.incidents_dir).get(incident_id))
    starter, hidden, public = (_overlay(alpha, k) for k in KINDS)
    if starter is None or hidden is None or public is None:
        print(
            f"== {alpha.name}: overlays/starter·hidden·public 이 없음 — 빌드가 거기까지 가지 않은 실행"
        )
        return
    template = select_template(incident_id.split(":")[1])
    assert template is not None
    print(
        f"== {alpha.name} (템플릿 {template.manifest.ref}, 손 구현 {len(refs.refs)}개{', 관문 꺼짐: ' + refs.stale if refs.stale else ''})"
    )
    built = json.loads((alpha / "report.json").read_text("utf-8")).get("build") or {}
    if built.get("dropped_cases"):
        print(f"  빌드가 채점에서 뺀 케이스: {built['dropped_cases']} (저장된 숨은 테스트에 없음)")
    (work / alpha.name).mkdir(
        parents=True, exist_ok=True
    )  # verify() 를 먼저 부르지 않으므로 (빌드에서는 verify 가 만든다)
    report = await verifier_for(template, work / alpha.name).verify_variants(
        starter, refs.overlays(), public, hidden
    )
    j = judge_hands(report, refs, _plan(alpha))
    for line in j.lines:
        print(f"  {line}")
    for f in j.findings:
        print(
            f"  [수리 {f.rule} → {f.generator}] {f.test or '전체'} · {', '.join(f.refs)} · {f.message}"
        )
        if f.detail:
            print(f"      {f.detail}")
    for flag in j.flags:
        print(f"  [표시] {flag}")
    print(f"  구하기 대상(손 구현이 의도한 사유로 떨어뜨리는 케이스): {sorted(j.keep) or '-'}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("alphas", nargs="+", type=Path)
    ap.add_argument("--work", type=Path, required=True, help="검증 작업 폴더 (git 밖)")
    a = ap.parse_args()
    for alpha in a.alphas:
        await replay(alpha, a.work)


if __name__ == "__main__":
    asyncio.run(main())
