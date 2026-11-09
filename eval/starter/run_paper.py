"""직군별 종이 단계 — 기능형 명세 + 템플릿 manifest(초안)로 계약 생성(plan_contract)까지만. 빌드·하네스 없음.

목적: 백엔드 한 예시(배송 감사)에 맞춰 다듬은 계약 스키마·검사(시드·테이블·규칙·외래 키·숨은 케이스 data)가 다른 직군에서도 통하는지,
그 직군의 하네스가 무엇을 해야 하는지(조절값·실패 사유)를 미리 본다. 백엔드는 실제 Java 템플릿 manifest, 나머지는 eval/starter/paper/<직군>.yaml.

입력: 기능형 명세 v6 평가 결과(git 밖 data/eval/spec/2026-11-06-feature-subs-gpt41-v6/drafts.jsonl — 가상 공고·레포 재료로 만든 명세),
레포 재료 incidents/public_v1. 출력: data/eval/build/<날짜>-paper-<tag>/ (report.json · summary.md) — git 밖.

사용:
  uv run python -m eval.starter.run_paper --model openai:gpt-4.1 --openai-key-env OPENAI_PREMIUM_API_KEY --prompt-version 6 --budget-usd 0.5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from eval.spec.run_feature import ModelOverride, make_client
from eval.starter.run_plan_contract import PinnedPrompts
from task_spec.nodes.starter.plan_contract import PlanResult, plan_contract
from task_spec.prompting import PromptRegistry
from task_spec.schemas import TaskItem
from task_spec.schemas.template import TemplateManifest
from task_spec.spec.incidents import feature_incident, load_incidents
from task_spec.starter.seed_rows import allocate_rows, seed_markdown
from task_spec.starter.tables import contract_tables
from task_spec.starter.templates import Template, select_template

ROOT = Path(__file__).resolve().parents[2]
DRAFTS = ROOT / "data" / "eval" / "spec" / "2026-11-06-feature-subs-gpt41-v6" / "drafts.jsonl"
PAPER = Path(__file__).resolve().parent / "paper"
OUT = ROOT / "data" / "eval" / "build"
SUBS = ["sw.backend", "sw.frontend", "data.eng", "infra.dba", "data.ml"]
_RULE = re.compile(r"[A-Za-z_]+\s*(?:→|->)\s*[A-Za-z_]+")


def template_for(sub: str) -> Template:
    """실제 템플릿이 있는 직군은 그것(select_template), 없으면 종이 단계 manifest 초안."""
    real = select_template(sub)
    if real is not None:
        return real
    manifest = TemplateManifest.model_validate(
        yaml.safe_load((PAPER / f"{sub}.yaml").read_text("utf-8"))
    )
    return Template(root=PAPER, manifest=manifest)


def summarize(sub: str, t: Template, task: TaskItem, res: PlanResult) -> dict[str, Any]:
    row: dict[str, Any] = {
        "sub": sub,
        "template": t.manifest.ref,
        "title": task.title,
        "ok": res.ok,
        "calls": len(res.provenance),
        "cost_usd": round(sum(p.cost_usd or 0.0 for p in res.provenance), 4),
        "first_problems": res.first_problems,
        "problems": res.problems,
        "spec_gaps": res.spec_gaps,
        "fail_reasons": res.fail_reasons,
        "adjustments": res.adjustments,
    }
    c, plan = res.contract, res.hidden_plan
    if c is None or plan is None:
        return row
    row["raw"] = {
        "contract": c.model_dump(),
        "hidden_plan": plan.model_dump(),
    }  # 검사를 고친 뒤 LLM 없이 다시 판정하려고
    names = {m.entity_id: m.name for m in c.entities}
    text = json.dumps(c.model_dump(), ensure_ascii=False)
    row |= {
        "interface_kind": c.interface_kind,
        "endpoints": [
            f"{e.method or ''} {e.path}".strip() + f" — {e.summary}" for e in c.endpoints
        ],
        "entities": [
            {"name": m.name, "fields": [f.name for f in m.fields], "foreign_keys": m.foreign_keys}
            for m in c.entities
        ],
        "tables": contract_tables(c, t.manifest.naming.table),
        "seed": [seed_markdown(s, names.get(s.entity_id, s.entity_id)) for s in c.seed],
        "rules_in_contract": sorted(set(_RULE.findall(text)))[:12],
        "public_tests": [p.checks for p in c.public_tests],
        "screens": [
            {
                "route": sc.route,
                "test_ids": [el.test_id for el in sc.elements],
                "storage": [k.name for k in sc.storage],
            }
            for sc in c.screens
        ],
        "fixtures": [f"{fx.fixture_id} ({fx.kind}) {fx.summary}" for fx in c.fixtures],
        "evidence": plan.evidence,
        "harness": plan.harness,
        "cases": [
            {
                "case_id": x.case_id,
                "reason": x.intended_reason,
                "scenario": x.scenario,
                "expect": x.expect,
                "data": [d.model_dump() for d in x.data],
                "setup": x.setup,
            }
            for x in plan.cases
        ],
        "allocation": [
            f"{r.case_id} {names.get(r.entity_id, r.entity_id)} id {r.first}~{r.last}"
            for r in allocate_rows(c, plan).ranges
        ],
    }
    return row


def summary_md(rows: list[dict[str, Any]]) -> str:
    out = ["# 직군별 종이 단계 — 계약 생성까지", ""]
    for r in rows:
        out += [
            f"## {r['sub']} — {r['title']} ({r['template']})",
            f"- 결과: {'통과' if r['ok'] else '실패'} · 호출 {r['calls']} · ${r['cost_usd']}",
        ]
        for key, label in (
            ("first_problems", "첫 답 문제"),
            ("problems", "남은 문제"),
            ("spec_gaps", "명세 결함"),
            ("fail_reasons", "실패"),
        ):
            if r.get(key):
                out.append(f"- {label}: " + " / ".join(x[:200] for x in r[key]))
        if r.get("adjustments"):
            out.append("- 코드가 맞춘 것: " + " / ".join(r["adjustments"]))
        if "interface_kind" in r:
            out += [
                f"- 인터페이스: {r['interface_kind']} · " + " | ".join(r["endpoints"]),
                "- 엔티티: "
                + " | ".join(
                    f"{e['name']}({len(e['fields'])}) FK={e['foreign_keys']}" for e in r["entities"]
                ),
                f"- 테이블: {r['tables']} · 시드: " + " / ".join(r["seed"]),
                f"- 계약의 규칙(→): {r['rules_in_contract']}",
                f"- 화면: {r['screens']}",
                f"- 입력 환경: {r['fixtures']}",
                f"- 하네스 값: {r['harness']} · 근거: {r['evidence']}",
                "- 숨은 케이스: "
                + " / ".join(
                    f"{x['case_id']} [{x['reason']}] {x['scenario'][:90]}"
                    + (f" · setup {x['setup']}" if x["setup"] else "")
                    for x in r["cases"]
                ),
                f"- 행 배정: {r['allocation']}",
            ]
        out.append("")
    return "\n".join(out)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--openai-key-env", default=None)
    ap.add_argument(
        "--prompt-version", type=int, default=None, help="plan_contract 버전 (기본 CURRENT)"
    )
    ap.add_argument("--budget-usd", type=float, default=0.5)
    ap.add_argument("--subs", nargs="*", default=SUBS)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    drafts = {
        json.loads(line)["sub"].split(":")[0]: json.loads(line)
        for line in DRAFTS.read_text("utf-8").splitlines()
    }
    incidents = load_incidents(ROOT / "incidents" / "public_v1")
    llm = make_client(a.openai_key_env)
    prompts = ModelOverride(
        PinnedPrompts(PromptRegistry(ROOT / "prompts"), a.prompt_version), a.model
    )  # type: ignore[arg-type]
    rows: list[dict[str, Any]] = []
    spent = worst = 0.0
    for sub in a.subs:
        if worst and spent + worst > a.budget_usd:
            print(f"예산 {a.budget_usd} 때문에 건너뜀: {sub}")
            continue
        task = TaskItem.model_validate(drafts[sub]["task"])
        template = template_for(sub)
        res = await plan_contract(
            task,
            template,
            llm=llm,
            prompts=prompts,
            ontology_version="paper",
            seed=inc.feature if (inc := feature_incident(incidents, sub)) else None,
        )
        row = summarize(sub, template, task, res)
        rows.append(row)
        spent += row["cost_usd"]
        worst = max(worst, row["cost_usd"])
        print(
            f"  {sub}: ok={row['ok']} calls={row['calls']} ${row['cost_usd']} (누적 ${spent:.4f})",
            flush=True,
        )

    out = OUT / f"{date.today()}-paper{('-' + a.tag) if a.tag else ''}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(
            {
                "model": a.model,
                "prompt_version": a.prompt_version,
                "cost_usd": round(spent, 4),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=1,
        ),
        "utf-8",
    )
    (out / "summary.md").write_text(summary_md(rows), "utf-8")
    print(f"→ ${spent:.4f} · {out}")


if __name__ == "__main__":
    asyncio.run(main())
