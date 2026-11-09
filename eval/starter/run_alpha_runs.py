"""알파 테스트 (기존 run) — 파이프라인 run 이 만든 기능형 공통 과제 하나로 계약 생성 → (Java 템플릿이면) 수리 루프까지 (spec-to-code §9·§11).

공고에서 명세부터 새로 만드는 `run_alpha` 와 달리, 이미 돈 run 의 명세를 입력으로 쓰고 계약·루프를 여러 번 돌려 통과율을 본다. 입력은 run 의 **공통 과제만** 뽑은 json
(`{"run_id", "posting_id", "task": TaskItem}`) — 공통 과제는 공고·재료로만 만들어 지원자 문서가 들어가지 않는다(불변식 3).
뽑은 파일과 출력은 모두 data/ 아래(git 밖).

- 직군은 `task.source_incident`(`pub:<직군>:<사례>`)에서, 재료는 incidents/public_v1 의 그 직군 `feature`.
- 백엔드는 실제 Java 템플릿 → `--loop` 이면 `build_and_repair` + 개발 서버 Gradle verifier. 다른 직군은 종이 단계 manifest 초안이라 계약까지만.

출력: data/eval/build/<날짜>-alpha-<tag>/<run 앞 8자리>/ (contract-<n>.json · plan.json · loop-<n>/report.json · summary.json)

사용:
  uv run python -m eval.starter.run_alpha_runs data/eval/build/2026-11-07-alpha/input/2df170e2.json \
      --model openai:gpt-4.1 --openai-key-env OPENAI_PREMIUM_API_KEY --contracts 1 --loops 2 --designs 1 --budget-usd 1.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from eval.spec.run_feature import ModelOverride, make_client
from eval.starter.gradle_verify import write_overlay as _write
from eval.starter.run_generate import PinnedVersions, _run_row, usage_by_prompt
from eval.starter.run_paper import template_for
from eval.starter.verifiers import verifier_for
from task_spec.nodes.starter.build import build_and_repair
from task_spec.nodes.starter.generate import BuildInputs
from task_spec.nodes.starter.plan_contract import plan_contract
from task_spec.prompting import PromptRegistry
from task_spec.schemas import TaskItem
from task_spec.spec.incidents import load_incidents

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "eval" / "build"
VERSION = "alpha"


def _cost(provenance: list[Any]) -> float:
    return round(sum(p.cost_usd or 0.0 for p in provenance), 4)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="run 의 공통 과제만 뽑은 json")
    ap.add_argument("--model", default=None, help="예: openai:gpt-4.1 (기본: 프롬프트 머리말 모델)")
    ap.add_argument("--openai-key-env", default=None)
    ap.add_argument("--contracts", type=int, default=1, help="계약 생성 횟수")
    ap.add_argument(
        "--loops", type=int, default=0, help="통과한 첫 계약으로 돌릴 수리 루프 횟수 (Java 만)"
    )
    ap.add_argument("--designs", type=int, default=1, help="루프에서 만들 허용 설계 수")
    ap.add_argument("--pin", action="append", default=[], help="예: --pin gen_starter=2")
    ap.add_argument(
        "--model-for",
        action="append",
        default=[],
        help="단계별 모델 (--model 보다 우선) — 예: --model-for gen_hidden=opencode:glm-5.3",
    )
    ap.add_argument("--budget-usd", type=float, default=1.0)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    src = json.loads(Path(a.input).read_text("utf-8"))
    task = TaskItem.model_validate(src["task"])
    sub = (task.source_incident or "").split(":")[1]
    inc = load_incidents(ROOT / "incidents" / "public_v1")[sub]
    seed = inc.feature
    assert seed is not None, f"{sub} 에 기능형 재료가 없음"
    template = template_for(sub)
    out = OUT / f"{date.today()}-alpha{('-' + a.tag) if a.tag else ''}" / src["run_id"][:8]
    out.mkdir(parents=True, exist_ok=True)

    llm = make_client(a.openai_key_env)
    pins = {k: int(v) for k, _, v in (x.partition("=") for x in a.pin)}
    per_task = dict(x.split("=", 1) for x in a.model_for)
    prompts = ModelOverride(
        PinnedVersions(PromptRegistry(ROOT / "prompts"), pins),  # type: ignore[arg-type]
        a.model,
        per_task=per_task,
    )
    kw = {"llm": llm, "prompts": prompts, "ontology_version": VERSION}
    print(
        f"{src['run_id'][:8]} · {sub} · 템플릿 {template.manifest.ref} · 허용 설계 {len(task.accepted_designs)}"
    )

    spent = 0.0
    summary: dict[str, Any] = {
        "run_id": src["run_id"],
        "sub": sub,
        "template": template.manifest.ref,
        "model": a.model,
        "pins": a.pin,
        "models": a.model_for,
        "contracts": [],
        "loops": [],
    }
    chosen = None
    for n in range(1, a.contracts + 1):
        t0 = time.monotonic()
        res = await plan_contract(task, template, seed=seed, **kw)  # type: ignore[arg-type]
        cost = _cost(res.provenance)
        spent += cost
        summary["contracts"].append(
            {
                "n": n,
                "ok": res.ok,
                "calls": len(res.provenance),
                "cost_usd": cost,
                "seconds": round(time.monotonic() - t0, 1),
                "model": res.provenance[0].model if res.provenance else None,
                "usage": usage_by_prompt(res.provenance),
                "first_problems": res.first_problems,
                "problems": res.problems,
                "spec_gaps": res.spec_gaps,
                "fail_reasons": res.fail_reasons,
                "adjustments": res.adjustments,
                "flags": res.flags,
            }
        )
        print(
            f"  계약 {n}: ok={res.ok} 호출 {len(res.provenance)} ${cost} (누적 ${spent:.4f})",
            flush=True,
        )
        if res.contract is not None and res.hidden_plan is not None:
            (out / f"contract-{n}.json").write_text(
                json.dumps(
                    {
                        "contract": res.contract.model_dump(),
                        "hidden_plan": res.hidden_plan.model_dump(),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                "utf-8",
            )
            if res.ok and chosen is None:
                chosen = (n, res.contract, res.hidden_plan)
        if spent > a.budget_usd:
            print(f"예산 {a.budget_usd} 초과 — 멈춤")
            break

    if a.loops and chosen is None:
        print("통과한 계약이 없어 루프를 돌리지 않음")
    elif a.loops and not (template.root / "project").is_dir():
        print(f"{sub} 는 manifest 초안뿐(빌드할 템플릿 없음) — 루프 생략")
    elif a.loops and chosen is not None:
        n_contract, contract, plan = chosen
        b = BuildInputs(
            task=task,
            contract=contract,
            plan=plan,
            template=template,
            seed=seed,
            avoid_terms=tuple(inc.avoid_terms),
        )
        designs = task.accepted_designs[: a.designs]
        worst = 0.0
        for n in range(1, a.loops + 1):
            if spent + max(worst, 0.2) > a.budget_usd:
                print(f"예산 {a.budget_usd} 때문에 루프 {n}부터 건너뜀")
                break
            lo = out / f"loop-{n}"
            t0 = time.monotonic()
            g = verifier_for(template, lo / "builds", keep_code=True)
            res = await build_and_repair(b, designs, verifier=g, compiler=g, **kw)  # type: ignore[arg-type]
            seconds = round(time.monotonic() - t0, 1)
            spent += res.cost_usd
            worst = max(worst, res.cost_usd)
            for key, o in res.overlays.items():
                _write(o, lo / "overlays" / key)
            row = {
                "n": n,
                "contract": n_contract,
                "designs": [d.design_id for d in designs],
                "status": res.status,
                "rounds": res.rounds,
                "calls": len(res.provenance),
                "cost_usd": res.cost_usd,
                "seconds": seconds,
                "regen_counts": res.regen_counts,
                "repairs": [r.model_dump() for r in res.repairs],
                "review_flags": res.review_flags,
                "usage": usage_by_prompt(res.provenance),
                "verify": [[_run_row(r) for r in rep.runs] for rep in res.reports],
            }
            (lo / "report.json").write_text(json.dumps(row, ensure_ascii=False, indent=1), "utf-8")
            summary["loops"].append(row)
            print(
                f"  루프 {n}: {res.status} · 회차 {res.rounds} · 호출 {len(res.provenance)} · ${res.cost_usd} · {seconds}s",
                flush=True,
            )
            for name, u in row["usage"].items():
                print(
                    f"    토큰 {name}: 호출 {u['calls']} · 재시도 {u['retries']} · 출력 {u['out']}(추론 {u['reasoning']}) · 최대 {u['max_out']}"
                )
            for rep in res.reports[-1:]:
                for r in rep.runs:
                    print(f"    {r.target} {r.setup} {_run_row(r)['tests']}")
            for r in res.repairs:
                print(f"    수리 {r.gate} {r.target} {r.result}: {r.reason[:120]}")
            for f in res.review_flags:
                print(f"    검토: {f[:200]}")

    summary["cost_usd"] = round(spent, 4)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    print(f"→ ${spent:.4f} · {out}")


if __name__ == "__main__":
    asyncio.run(main())
