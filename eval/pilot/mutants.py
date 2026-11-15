"""손 시범 유효성 검사의 돌연변이를 빌드 관문과 같은 생성기(`gen_variant`)로 만든다 (docs/design/analysis-stage.md §9.1).

재료를 쓴 사람이 돌연변이까지 만들면 재료와 검사를 같은 손이 다듬는다 — 과적합 막기(§9.1)의 "질문 붙이기는 별도 에이전트"와 같은 이유로,
돌연변이는 별도 생성기가 정해진 갈래(빼먹기·뒤집기·경계)로 만든다 [2026-11-13 사용자 — 새 에이전트가 검사]. 생성기는 공개 테스트를 보지 않는다.
요구(r*)마다 한 번 부르고(그 요구의 공개 수용 기준 문장과 함께), 만든 변형을 재료의 `mutants/`·`mutants.json` 에 쓴다 — validity.py 가 그대로 돌린다.

  uv run python -m eval.pilot.mutants --mid <재료>
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from eval.pilot.grade import overlay
from eval.pilot.model import ROOT, Material, load_material
from eval.spec.run_feature import make_client
from eval.starter.run_plan_contract import JAVA
from task_spec.nodes.starter.generate import gen_variants
from task_spec.prompting import PromptRegistry
from task_spec.schemas import TaskContract, TaskItem
from task_spec.starter.templates import Template, load_template, select_template
from task_spec.starter.variants import OP_NAME, VariantTarget

TASK = "gen_variant"


def requirement_targets(m: Material, task: TaskItem) -> list[VariantTarget]:
    """측정 후보 요구(method_req 에 나온 r*)마다 — 그 요구 문장 + 그 요구를 보는 공개 수용 기준 문장."""
    reqs = sorted({r for rs in m.method_req.values() for r in rs})
    req_text = {r.req_id: r.text for r in task.requirements}
    ac_text = {a.ac_id: a.text for a in task.acceptance_public}
    out = []
    for r in reqs:
        acs = [f"{p} {ac_text[p]}" for p, rs in m.req_ids.items() if r in rs and p in ac_text]
        text = req_text.get(r, r) + (f" (공개 수용 기준: {' / '.join(acs)})" if acs else "")
        out.append(VariantTarget(r, text, tuple(p for p, rs in m.req_ids.items() if r in rs)))
    return out


def _template(report: dict[str, Any]) -> Template:
    return select_template(report.get("sub") or "") or load_template(JAVA)


async def generate(m: Material, llm: Any = None) -> dict[str, Any]:
    alpha = m.alpha_dir
    task = TaskItem.model_validate_json((alpha / "task.json").read_text("utf-8"))
    contract = TaskContract.model_validate(
        json.loads((alpha / "contract.json").read_text("utf-8"))["contract"]
    )
    template = _template(json.loads((alpha / "report.json").read_text("utf-8")))
    starter = overlay("starter", alpha / "overlays" / "starter")
    base = overlay("ref_accepted", alpha / "overlays" / "d1", "d1")
    prompts = PromptRegistry(ROOT / "prompts")
    made = await gen_variants(
        task,
        contract,
        template,
        starter,
        base,
        llm=llm or make_client(None),
        prompts=prompts,
        ontology_version="eval",
        only=requirement_targets(m, task),
    )
    meta: dict[str, Any] = {}
    for o, spec in made.variants:
        mine = {f.path: f.content for f in o.files if f.path in spec.paths}
        for path, content in mine.items():
            p = m.dir / "mutants" / spec.label / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, "utf-8")
        meta[spec.label] = {
            "req": spec.rule_id,
            "note": f"{OP_NAME[spec.op]}: {spec.note}",
            "by": prompts.get(TASK).ref,
            "changed": spec.changed,
        }
    (m.dir / "mutants.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    record = {
        "by": prompts.get(TASK).ref,
        "dropped": made.dropped,
        "failed": made.fail_reasons,
        "missing_boundary": made.missing_boundary,
        "provenance": [p.model_dump(mode="json") for p in made.provenance],
        "cost_usd": round(sum(p.cost_usd or 0.0 for p in made.provenance), 4),
    }
    (m.dir / "mutants.provenance.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), "utf-8"
    )
    return {"mutants": meta, **{k: record[k] for k in ("dropped", "failed", "cost_usd")}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True)
    ap.add_argument(
        "--force", action="store_true", help="이미 있는 mutants.json 을 덮어쓴다 (예전 손 돌연변이)"
    )
    a = ap.parse_args()
    m = load_material(a.mid)
    if (m.dir / "mutants.json").exists() and not a.force:
        raise SystemExit(f"{m.dir / 'mutants.json'} 이 이미 있음 — 덮어쓰려면 --force")
    out = asyncio.run(generate(m))
    for label, v in out["mutants"].items():
        print(f"  {label}: {v['req']} {v['note']} ({v['changed']}줄)")
    print(json.dumps({k: out[k] for k in ("dropped", "failed", "cost_usd")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
