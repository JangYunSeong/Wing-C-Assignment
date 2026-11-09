"""상류 평가 — 계약 → 채점 코드까지만 (구현 코드 없이). spec-to-code §9 (2026-11-08 사용자 결정: 상류는 자주·많이, 전체는 가끔).

전체 실행(시작 코드·참조 구현·수리 루프)은 회당 $0.2~0.35 에 실패 원인이 서로 얽힌다 (시작 코드 버그가 숨은 테스트 판정을 망치는 등).
상류만 돌려 계약·채점 코드의 품질을 따로 잰다 — 회당 약 $0.10, Gradle 은 테스트 컴파일만 (숨은 테스트는 HTTP 로만 판정해 구현 없이 컴파일된다).

재는 것: 계약 통과(첫 답·재요청 뒤) · 재료가 선언한 필요 요소 충족 · 채점 코드 생성 통과(정적 검사 첫 답·재요청 뒤) · 구현 없이 컴파일 · 비용.
재지 못하는 것: 숨은 테스트가 흔한 구현·허용 구현을 실제로 가르는지, 단언이 통과 조건과 맞는지 — 전체 실행이나 사람 검토 몫.

입력: 이미 만든 명세(task.json — run_alpha 출력, 공고·재료로만 만든 공통 과제라 지원자 데이터 아님). 출력: data/eval/build/<날짜>-grading-<tag>/ — git 밖.

사용:
  uv run python -m eval.starter.run_grading \\
      --case data/eval/build/2026-11-07-alpha-kakaobank005/task.json --case <다른 task.json> --runs 3 \\
      --model openai:gpt-4.1 --openai-key-env OPENAI_PREMIUM_API_KEY --pin plan_contract=9 --pin gen_hidden=3 \\
      --budget-usd 1.5 --tag base
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from eval.spec.run_feature import ModelOverride, make_client
from eval.starter.gradle_verify import write_overlay as _write
from eval.starter.run_generate import PinnedVersions
from eval.starter.run_paper import template_for
from eval.starter.verifiers import verifier_for
from task_spec.nodes.starter.build import compile_and_fix
from task_spec.nodes.starter.generate import BuildInputs, GenResult, gen_hidden, gen_public
from task_spec.nodes.starter.plan_contract import plan_contract
from task_spec.prompting import PromptRegistry
from task_spec.schemas import CompileError, HiddenPlan, TaskContract, TaskItem
from task_spec.spec.incidents import feature_incident, incident_by_id, load_incidents

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "eval" / "build"
VERSION = "grading"
NEEDS = "숨은 요구에 필요한 것"


def _cost(prov: list[Any]) -> float:
    return round(sum(p.cost_usd or 0.0 for p in prov), 4)


def _kind(problem: str) -> str:
    """문제 문장 → 종류 (집계용) — 첫 ' — ' 앞, 파일 경로·숫자는 지움."""
    head = problem.split(" — ")[0]
    for word in ("숨은 테스트 ", "숨은 케이스 "):
        if head.startswith(word):
            head = word + head.split(" ", 2)[-1]
    return " ".join(w for w in head.split() if not any(c.isdigit() for c in w) and "/" not in w)[
        :80
    ]


def _gen_row(r: GenResult) -> dict[str, Any]:
    return {
        "ok": r.ok,
        "first_ok": r.ok and not r.first_problems,
        "calls": len(r.provenance),
        "cost_usd": _cost(r.provenance),
        "first_problems": r.first_problems,
        "problems": r.problems,
        "fail_reasons": r.fail_reasons,
        "files": [f.path for f in r.overlay.files] if r.overlay else [],
    }


async def run_one(
    case: dict[str, Any],
    n: int,
    out: Path,
    kw: dict[str, Any],
    incidents: dict[str, Any],
    stop_after: str | None = None,
    reuse: Path | None = None,
) -> dict[str, Any]:
    """stop_after="contract": 계약만 (계약 프롬프트를 바꿀 때). reuse: 이전 평가 폴더의 같은 케이스·회차 계약을 다시 써서
    계약 호출을 건너뜀 (숨은·공개 테스트 쪽만 바꿀 때) — 2026-11-08 비용 점검: 계약이 상류 평가 비용의 약 55%."""
    task: TaskItem = case["task"]
    sub = (task.source_incident or "").split(":")[1]
    # 명세가 쓴 재료 그대로 (없으면 직군 템플릿이 시험할 수 있는 사례)
    inc = incident_by_id(incidents, task.source_incident) or feature_incident(incidents, sub)
    assert inc is not None, sub
    template = template_for(sub)
    run_dir = out / f"{case['key']}-{n}"
    run_dir.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "case": case["key"],
        "n": n,
        "sub": sub,
        "template": template.manifest.ref,
    }
    t0 = time.monotonic()
    if reuse is not None:
        d = json.loads((reuse / f"{case['key']}-{n}" / "contract.json").read_text("utf-8"))
        contract, plan = (
            TaskContract.model_validate(d["contract"]),
            HiddenPlan.model_validate(d["hidden_plan"]),
        )
        (run_dir / "contract.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        row["contract"] = {
            "ok": True,
            "first_ok": True,
            "calls": 0,
            "cost_usd": 0.0,
            "reused": str(reuse.relative_to(ROOT) if reuse.is_relative_to(ROOT) else reuse),
        }
        return await _tests(row, task, contract, plan, template, inc, run_dir, kw, t0, stop_after)
    res = await plan_contract(task, template, seed=inc.feature, **kw)
    row["contract"] = {
        "ok": res.ok,
        "first_ok": res.ok and not res.first_problems,
        "calls": len(res.provenance),
        "cost_usd": _cost(res.provenance),
        "first_problems": res.first_problems,
        "problems": res.problems,
        "spec_gaps": res.spec_gaps,
        "needs_first": [p for p in res.first_problems if NEEDS in p],
        "needs_final": [p for p in res.problems if NEEDS in p],
        "adjustments": res.adjustments,
        "flags": res.flags,
        "fail_reasons": res.fail_reasons,
    }
    if res.contract is not None and res.hidden_plan is not None:
        (run_dir / "contract.json").write_text(
            json.dumps(
                {
                    "contract": res.contract.model_dump(mode="json"),
                    "hidden_plan": res.hidden_plan.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=1,
            ),
            "utf-8",
        )
    if not res.ok or res.contract is None or res.hidden_plan is None:
        row["stopped"] = "계약 실패"
        row["seconds"] = round(time.monotonic() - t0, 1)
        row["cost_usd"] = row["contract"]["cost_usd"]
        return row
    return await _tests(
        row, task, res.contract, res.hidden_plan, template, inc, run_dir, kw, t0, stop_after
    )


async def _tests(
    row: dict[str, Any],
    task: TaskItem,
    contract: TaskContract,
    plan: HiddenPlan,
    template: Any,
    inc: Any,
    run_dir: Path,
    kw: dict[str, Any],
    t0: float,
    stop_after: str | None,
) -> dict[str, Any]:
    if stop_after == "contract":
        row["seconds"] = round(time.monotonic() - t0, 1)
        row["cost_usd"] = row["contract"]["cost_usd"]
        return row
    b = BuildInputs(
        task=task,
        contract=contract,
        plan=plan,
        template=template,
        seed=inc.feature,
        avoid_terms=tuple(inc.avoid_terms),
    )
    use_public = template.manifest.public_helper is not None
    results = await asyncio.gather(  # 테스트 먼저 — 공개·숨은 테스트는 서로 보지 않고 따로
        gen_hidden(b, **kw), *([gen_public(b, **kw)] if use_public else [])
    )
    hid = results[0]
    pub: GenResult | None = results[1] if use_public else None
    row["hidden"] = _gen_row(hid)
    if pub is not None:
        row["public"] = _gen_row(pub)
    fix_cost = 0.0
    if hid.overlay is not None and template.project.is_dir():
        # 빌드와 같은 컴파일 루프 — 템플릿 project/ + 하네스 + 숨은·공개 테스트 (구현 없음), 오류는 쓴 생성기에 돌려준다
        t1 = time.monotonic()
        compiler = verifier_for(template, run_dir / "compile", timeout=600)
        fix = await compile_and_fix(b, hid.overlay, pub.overlay if pub else None, compiler, kw)
        fix_cost = sum(_cost(r.provenance) for r in fix.results)
        for o in (fix.hidden, fix.public):
            if o is not None:
                _write(o, run_dir / ("hidden" if o.kind == "hidden" else "public"))
        row["compile"] = {
            "ok": not fix.errors,
            "first_ok": not fix.first_errors,
            "compiles": fix.compiles,
            "repairs": [f"{x.target}: {x.reason} ({x.result})" for x in fix.repairs],
            "first_errors": [_err(e) for e in fix.first_errors[:8]],
            "errors": [_err(e) for e in fix.errors[:8]],
            "cost_usd": round(fix_cost, 4),
            "seconds": round(time.monotonic() - t1, 1),
        }
    row["seconds"] = round(time.monotonic() - t0, 1)
    row["cost_usd"] = round(
        row["contract"]["cost_usd"]
        + row["hidden"]["cost_usd"]
        + (row["public"]["cost_usd"] if "public" in row else 0.0)
        + fix_cost,
        4,
    )
    return row


def _err(e: CompileError) -> str:
    return f"{e.path.rsplit('/', 1)[-1]}:{e.line} {e.message}" + (
        f" ({e.detail})" if e.detail else ""
    )


def _rate(rows: list[dict[str, Any]], pred: Any) -> str:
    hit = sum(1 for r in rows if pred(r))
    return f"{hit}/{len(rows)}"


def summary_md(rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    def c(r: dict[str, Any], k: str) -> Any:
        return r.get("contract", {}).get(k)

    def h(r: dict[str, Any], k: str) -> Any:
        return (r.get("hidden") or {}).get(k)

    def pb(r: dict[str, Any], k: str) -> Any:
        return (r.get("public") or {}).get(k)

    def mark(r: dict[str, Any], part: str, k: str) -> str:
        if r.get(part) is None:
            return "-"
        return "✓" if (r.get(part) or {}).get(k) else "✗"

    lines = [
        f"# 상류 평가 — 계약 → 채점 코드 ({meta['tag'] or '-'})",
        "",
        f"모델 {meta['model']} · 고정 {meta['pins'] or '-'} · 구현 코드 없이 (숨은·공개 테스트는 템플릿·하네스·도우미로만 컴파일)",
        "",
        "| 케이스 | 회 | 계약 첫 답 | 계약 | 필요 요소 첫 답 빠짐 | 숨은 첫 답 | 숨은 | 공개 첫 답 | 공개 | 컴파일 첫 | 컴파일 | 비용 | 초 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        comp = r.get("compile")
        lines.append(
            f"| {r['case']} | {r['n']} | {'✓' if c(r, 'first_ok') else '✗'} | {'✓' if c(r, 'ok') else '✗'} | "
            f"{len(c(r, 'needs_first') or []) or '-'} | "
            f"{mark(r, 'hidden', 'first_ok')} | {mark(r, 'hidden', 'ok')} | "
            f"{mark(r, 'public', 'first_ok')} | {mark(r, 'public', 'ok')} | "
            f"{'-' if comp is None else ('✓' if comp.get('first_ok', comp['ok']) else '✗')} | "
            f"{'-' if comp is None else ('✓' if comp['ok'] else '✗')} | ${r['cost_usd']} | {r['seconds']} |"
        )
    ok_all = [
        r
        for r in rows
        if c(r, "ok")
        and h(r, "ok")
        and (r.get("public") is None or pb(r, "ok"))
        and (r.get("compile") or {}).get("ok")
    ]
    if meta.get("stop_after") == "contract":  # 계약만 — 뒤 단계 비율은 의미 없음
        lines += [
            "",
            f"- 실행 {len(rows)} (계약만) · 계약 첫 답 {_rate(rows, lambda r: c(r, 'first_ok'))} · 계약 {_rate(rows, lambda r: c(r, 'ok'))}"
            f" · 비용 합 ${round(sum(r['cost_usd'] for r in rows), 4)} · 회당 평균 "
            f"${round(sum(r['cost_usd'] for r in rows) / max(len(rows), 1), 4)}",
            "",
            "## 첫 답 문제 종류 (재요청 사유)",
        ]
    lines += (
        []
        if meta.get("stop_after") == "contract"
        else [
            "",
            f"- 실행 {len(rows)} · 계약 첫 답 {_rate(rows, lambda r: c(r, 'first_ok'))} · 계약 {_rate(rows, lambda r: c(r, 'ok'))}"
            f" · 숨은 첫 답 {_rate(rows, lambda r: h(r, 'first_ok'))} · 숨은 {_rate(rows, lambda r: h(r, 'ok'))}"
            f" · 공개 첫 답 {_rate(rows, lambda r: pb(r, 'first_ok'))} · 공개 {_rate(rows, lambda r: pb(r, 'ok'))}"
            f" · 컴파일 첫 {_rate(rows, lambda r: (r.get('compile') or {}).get('first_ok'))}"
            f" · 컴파일 {_rate(rows, lambda r: (r.get('compile') or {}).get('ok'))} · **끝까지 {len(ok_all)}/{len(rows)}**",
            f"- 비용 합 ${round(sum(r['cost_usd'] for r in rows), 4)} · 회당 평균 "
            f"${round(sum(r['cost_usd'] for r in rows) / max(len(rows), 1), 4)}",
            "",
            "## 첫 답 문제 종류 (재요청 사유)",
        ]
    )
    kinds: Counter[str] = Counter()
    for r in rows:
        kinds.update(f"계약: {_kind(p)}" for p in c(r, "first_problems") or [])
        kinds.update(f"숨은: {_kind(p)}" for p in h(r, "first_problems") or [])
        kinds.update(f"공개: {_kind(p)}" for p in pb(r, "first_problems") or [])
        kinds.update(
            f"컴파일: {e.split(' ', 1)[1][:80]}"
            for e in (r.get("compile") or {}).get("first_errors") or []
        )
    lines += [f"- {n}× {k}" for k, n in kinds.most_common(15)] or ["- 없음"]
    lines += ["", "## 남은 실패"]
    for r in rows:
        left = [
            *(c(r, "problems") or []),
            *(c(r, "spec_gaps") or []),
            *(h(r, "problems") or []),
            *(pb(r, "problems") or []),
        ]
        left += (r.get("compile") or {}).get("errors") or []
        if left or r.get("stopped"):
            lines.append(f"- {r['case']} #{r['n']}: " + " / ".join(x[:200] for x in left[:4]))
    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", action="append", required=True, help="명세 task.json (여러 번)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default=None)
    ap.add_argument("--openai-key-env", default=None)
    ap.add_argument("--pin", action="append", default=[], help="예: --pin plan_contract=9")
    ap.add_argument("--budget-usd", type=float, default=1.5)
    ap.add_argument("--tag", default="")
    ap.add_argument(
        "--stop-after", choices=["contract"], default=None, help="계약만 (계약 프롬프트를 바꿀 때)"
    )
    ap.add_argument(
        "--reuse-contracts",
        default=None,
        help="이전 평가 폴더 — 같은 케이스·회차의 contract.json 을 다시 씀 (숨은·공개 테스트만 바꿀 때)",
    )
    a = ap.parse_args()

    cases = []
    for path in a.case:
        p = Path(path).resolve()
        task = TaskItem.model_validate_json(p.read_text("utf-8"))
        key = (task.task_id or p.parent.name).removeprefix("alpha-").replace("#", "_")
        cases.append({"key": key, "task": task, "src": str(p.relative_to(ROOT))})
    out = OUT / f"{date.today()}-grading{('-' + a.tag) if a.tag else ''}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    pins = {k: int(v) for k, _, v in (x.partition("=") for x in a.pin)}
    prompts = ModelOverride(PinnedVersions(PromptRegistry(ROOT / "prompts"), pins), a.model)  # type: ignore[arg-type]
    kw = {"llm": make_client(a.openai_key_env), "prompts": prompts, "ontology_version": VERSION}
    incidents = load_incidents(ROOT / "incidents" / "public_v1")
    meta = {
        "tag": a.tag,
        "model": a.model,
        "pins": a.pin,
        "cases": [c["src"] for c in cases],
        "stop_after": a.stop_after,
        "reuse_contracts": a.reuse_contracts,
    }
    rows: list[dict[str, Any]] = []
    spent = worst = 0.0
    for n in range(1, a.runs + 1):  # 케이스를 번갈아 — 예산이 끊겨도 고르게 남게
        for case in cases:
            if worst and spent + worst > a.budget_usd:
                print(f"예산 {a.budget_usd} 때문에 건너뜀: {case['key']} #{n}")
                continue
            row = await run_one(
                case,
                n,
                out,
                kw,
                incidents,
                a.stop_after,
                Path(a.reuse_contracts).resolve() if a.reuse_contracts else None,
            )
            rows.append(row)
            spent += row["cost_usd"]
            worst = max(worst, row["cost_usd"])
            comp = row.get("compile")
            print(
                f"  {case['key']} #{n}: 계약 {'✓' if row['contract']['ok'] else '✗'}"
                f" · 숨은 {'-' if 'hidden' not in row else ('✓' if row['hidden']['ok'] else '✗')}"
                f" · 공개 {'-' if 'public' not in row else ('✓' if row['public']['ok'] else '✗')}"
                f" · 컴파일 {'-' if comp is None else ('✓' if comp['ok'] else '✗')}"
                f" · ${row['cost_usd']} (누적 ${spent:.4f})",
                flush=True,
            )
            (out / "report.json").write_text(
                json.dumps(
                    {**meta, "cost_usd": round(spent, 4), "rows": rows},
                    ensure_ascii=False,
                    indent=1,
                ),
                "utf-8",
            )
    (out / "summary.md").write_text(summary_md(rows, meta), "utf-8")
    print(f"→ ${spent:.4f} · {out}")


if __name__ == "__main__":
    asyncio.run(main())
