"""AI 단독 풀이 테스트 — 강한 모델이 과제의 보이는 부분만 보고 혼자 풀 때 함정에 빠지는가 (context.md §4.3).

표본 (이미 만든 과제, git 밖 data/eval/spec/):
- 공통 과제 12: 2026-11-03-{dev,test}-v1v2b 의 draft_task v2 결과 (infra.network 는 사례 교체 후 all-net2)
- 확장 문항 22: 2026-11-03-verify-{dev,test}
풀이: solve_task (openai:gpt-5.5), 문항마다 --runs 회. 채점: grade_solo (gpt-4.1).

지표
- 공통 과제: trap_catch(함정을 알아챈 비율 — 낮을수록 함정이 작동) · concept_hit(핵심 개념을 AI 가 먼저 짚은 비율)
  · level(행동 기준 수준 평균) · broken(수준 5 이거나 함정·개념을 모두 맞힘 = AI 가 대신 풀 수 있는 과제) 비율
- 확장 문항: level · pass3(수준 3 이상 = 경험 없는 AI 가 '경험 확인' 수준 답을 냄 — 낮을수록 좋음)
사용: uv run python -m eval.spec.run_solo --runs 3 --budget 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

from eval.spec.run_draft import ROOT
from task_spec.config import Settings
from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.llm.factory import build_llm_client
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    GradeSoloInput,
    GradeSoloOutput,
    SolveSelectInput,
    SolveSelectOutput,
    SolveTaskInput,
    SolveTaskOutput,
)

EVAL = ROOT / "data" / "eval" / "spec"
PROMPTS = PromptRegistry(ROOT / "prompts")  # 풀이·채점 프롬프트 (평가 전용)
NONE = "(없음)"


def _visible(d: dict[str, Any]) -> str:
    return f"# {d['title']}\n\n{d['scenario']}\n\n## 제출물\n" + "\n".join(
        f"- {x}" for x in d["deliverables"]
    )


def _anchors(d: dict[str, Any]) -> str:
    a = d["behavioral_anchors"]
    return "\n".join(
        f"- 수준 {k[-1]}: {a[k]['behavior']}" for k in ("level_1", "level_3", "level_5")
    )


def load_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for split in ("dev", "test"):
        for line in (EVAL / f"2026-11-03-{split}-v1v2b" / "drafts.jsonl").open(encoding="utf-8"):
            r = json.loads(line)
            if not r["model"].endswith("@2") or not r.get("draft") or r["sub"] == "infra.network":
                continue
            items.append(
                {"kind": "common", "sub": r["sub"], "posting": r["posting"], "draft": r["draft"]}
            )
    net = json.loads(
        (EVAL / "2026-11-03-all-net2" / "drafts.jsonl").read_text("utf-8").splitlines()[0]
    )
    items.append(
        {"kind": "common", "sub": net["sub"], "posting": net["posting"], "draft": net["draft"]}
    )
    for split in ("dev", "test"):
        rep = json.loads((EVAL / f"2026-11-03-verify-{split}" / "report.json").read_text("utf-8"))
        for r in rep["rows"]:
            if r.get("draft"):
                items.append(
                    {
                        "kind": "extension",
                        "sub": r["sub"],
                        "posting": r["posting"],
                        "claim": r["claim"],
                        "common_title": r["common_title"],
                        "draft": r["draft"],
                    }
                )
    for it in items:
        _prepare(it)
    return items


def _prepare(it: dict[str, Any]) -> None:
    d = it["draft"]
    vis = _visible(d)
    if it["kind"] == "extension":
        vis = f"(앞서 푼 공통 과제: {it['common_title']})\n\n" + vis
    arts = d.get("artifacts") or []
    if arts:
        it["menu"] = "\n".join(f"- {a['artifact_id']}: {a['title']}" for a in arts)
        vis += f"\n\n## 자료 목록 (최대 {d['max_requests']}개 요청 가능)\n" + it["menu"]
    it["visible"] = vis
    it["tech_env"] = ", ".join(d.get("tech_used") or []) or "(제한 없음)"
    it["traps"] = (
        "\n".join(
            f"- {t['trap_id']}: {t['description']} / AI 가 틀리는 이유: {t['why_ai_fails']}"
            for t in d.get("traps", [])
        )
        or NONE
    )
    it["concepts"] = [o["value"] for o in d["observables"] if o["kind"] == "first_mention_of"]
    it["anchors"] = _anchors(d)


async def generate_items(split: str, prompts_root: Path) -> list[dict[str, Any]]:
    """지금 프롬프트(예: draft_task v3)로 공통 과제를 새로 만든다 (qwen, run_draft 와 같은 표본)."""
    from eval.spec.run_draft import pick_postings
    from task_spec.ontology.repository import OntologyRepository
    from task_spec.spec.draft import draft_task
    from task_spec.spec.incidents import load_incidents

    st = Settings()
    repo = await OntologyRepository.open(st.database_url.get_secret_value(), "dev")
    picked = await pick_postings(repo, repo.version.version, load_incidents(st.incidents_dir))
    await repo.close()
    idx = {"dev": 0, "test": 1}[split]
    picked = [x for i, x in enumerate(picked) if i % 2 == idx]
    llm = build_llm_client(st)
    reg = PromptRegistry(prompts_root)
    results = await asyncio.gather(
        *(draft_task(b.ctx, llm=llm, prompts=reg) for _, _, b in picked if b.ctx)
    )
    items: list[dict[str, Any]] = []
    for (sub, pid, _), r in zip(picked, results, strict=True):
        if r.output is None:
            print(f"생성 실패: {sub} {pid} {r.fail_reasons}")
            continue
        it = {
            "kind": "common",
            "sub": sub,
            "posting": pid,
            "draft": r.output.model_dump(mode="json"),
            "gen_flags": r.flags,
            "gen_fail_reasons": r.fail_reasons,
            "gen_first_problems": r.first_problems,
        }
        _prepare(it)
        items.append(it)
    return items


SOLVER: str | None = (
    None  # --solver 로 풀이 모델(solve_select·solve_task)을 바꾼다 — 채점(grade_solo)은 그대로
)


def _solve_tpl(task: str) -> Any:
    t = PROMPTS.get(task)
    if not SOLVER or t.meta.model == SOLVER:
        return t
    return t.model_copy(update={"meta": t.meta.model_copy(update={"model": SOLVER})})


def load_report_items(path: Path, subs: set[str] | None, kind: str | None) -> list[dict[str, Any]]:
    """이전 실행 report.json 의 과제를 그대로 다시 쓴다 (풀이 모델끼리 같은 과제로 비교)."""
    items = []
    for it in json.loads(path.read_text("utf-8"))["items"]:
        if (subs and it["sub"] not in subs) or (kind and it["kind"] != kind):
            continue
        it = {k: v for k, v in it.items() if k not in ("runs", "stats")}
        _prepare(it)
        items.append(it)
    return items


class Budget:
    def __init__(self, limit: float) -> None:
        self.limit, self.spent = limit, 0.0

    def add(self, c: float | None) -> None:
        self.spent += c or 0.0

    @property
    def over(self) -> bool:
        return self.spent >= self.limit


async def one(
    llm: LLMClient, it: dict[str, Any], run: int, budget: Budget, sem: asyncio.Semaphore
) -> dict[str, Any]:
    async with sem:
        if budget.over:
            return {"run": run, "skipped": "budget"}
        task_text = it["visible"]
        picked: list[str] = []
        arts = {a["artifact_id"]: a for a in it["draft"].get("artifacts") or []}
        if arts:
            limit = it["draft"]["max_requests"]
            sel = assemble(
                _solve_tpl("solve_select"),
                SolveSelectInput(task=_visible(it["draft"]), menu=it["menu"], max_requests=limit),
                ontology_version="eval",
            )
            try:
                sr = await llm.call(sel, SolveSelectOutput)
            except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
                return {"run": run, "error": f"select:{type(exc).__name__}"}
            budget.add(sr.provenance.cost_usd)
            picked = [x for x in dict.fromkeys(sr.parsed.artifact_ids) if x in arts][:limit]
            task_text += "\n\n## 요청해서 받은 자료\n" + "\n\n".join(
                f"### {x}: {arts[x]['title']}\n{arts[x]['content']}" for x in picked
            )
        solve = assemble(
            _solve_tpl("solve_task"),
            SolveTaskInput(
                task=task_text,
                time_limit_min=60 if it["kind"] == "common" else 30,
                tech_env=it["tech_env"],
            ),
            ontology_version="eval",
        )
        try:
            s = await llm.call(solve, SolveTaskOutput)
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            return {"run": run, "error": f"solve:{type(exc).__name__}"}
        budget.add(s.provenance.cost_usd)
        grade = assemble(
            PROMPTS.get("grade_solo"),
            GradeSoloInput(
                task=task_text,
                answer=s.parsed.submission,
                traps=it["traps"],
                key_concepts="\n".join(f"- {c}" for c in it["concepts"]) or NONE,
                anchors=it["anchors"],
            ),
            ontology_version="eval",
        )
        try:
            g = await llm.call(grade, GradeSoloOutput)
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            return {
                "run": run,
                "error": f"grade:{type(exc).__name__}",
                "answer": s.parsed.submission,
            }
        budget.add(g.provenance.cost_usd)
        gv = g.parsed
        keys = [x for x, a in arts.items() if a["role"] == "key"]
        return {
            "run": run,
            "picked": picked,
            "key_requested": (sum(x in picked for x in keys) / len(keys)) if keys else None,
            "answer": s.parsed.submission,
            "solve_cost": s.provenance.cost_usd,
            "solve_out_tokens": s.provenance.tokens.out,
            "grade_cost": g.provenance.cost_usd,
            "traps": [t.model_dump() for t in gv.traps],
            "concepts": [c.model_dump() for c in gv.concepts],
            "level": gv.level,
            "rationale": gv.rationale,
        }


def item_stats(it: dict[str, Any]) -> dict[str, Any]:
    runs = [r for r in it["runs"] if "level" in r]
    if not runs:
        return {"n_runs": 0}
    tc = [mean(t["caught"] for t in r["traps"]) for r in runs if r["traps"]]
    ch = [mean(c["mentioned"] for c in r["concepts"]) for r in runs if r["concepts"]]
    broken = [
        r["level"] >= 5
        or (
            bool(r["traps"])
            and all(t["caught"] for t in r["traps"])
            and all(c["mentioned"] for c in r["concepts"])
        )
        for r in runs
    ]
    kr = [r["key_requested"] for r in runs if r.get("key_requested") is not None]
    return {
        "n_runs": len(runs),
        "key_requested": round(mean(kr), 3) if kr else None,
        "trap_catch": round(mean(tc), 3) if tc else None,
        "concept_hit": round(mean(ch), 3) if ch else None,
        "level": round(mean(r["level"] for r in runs), 2),
        "broken": round(mean(broken), 3),
        "pass3": round(mean(r["level"] >= 3 for r in runs), 3),
    }


def summarize(items: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    st = [i["stats"] for i in items if i["kind"] == kind and i["stats"].get("n_runs")]

    def avg(k: str) -> float | None:
        xs = [s[k] for s in st if s.get(k) is not None]
        return round(mean(xs), 3) if xs else None

    out = {"n_items": len(st), "level": avg("level")}
    if kind == "common":
        out |= {
            "trap_catch": avg("trap_catch"),
            "concept_hit": avg("concept_hit"),
            "broken": avg("broken"),
        }
    else:
        out |= {"pass3": avg("pass3")}
    return out


def sample_md(it: dict[str, Any]) -> str:
    st = it["stats"]
    runs = [r for r in it["runs"] if "level" in r]
    head = f"### {it['kind']} · {it['sub']} · {it['posting']}{' · ' + it['claim'] if it.get('claim') else ''}\n\n"
    head += f"**{it['draft']['title']}**  \n지표: {json.dumps(st, ensure_ascii=False)}\n\n"
    if not runs:
        return head + "(풀이 없음)\n"
    r = runs[0]
    traps = (
        "\n".join(
            f"- {t['trap_id']}: {'알아챔' if t['caught'] else '빠짐'} — {t['evidence']}"
            for t in r["traps"]
        )
        or "(함정 없음)"
    )
    concepts = ", ".join(f"{c['concept']}({'O' if c['mentioned'] else 'X'})" for c in r["concepts"])
    return (
        head
        + f"1회차 판정: 수준 {r['level']} · 개념 {concepts}\n\n함정\n{traps}\n\n채점 근거: {r['rationale']}\n\n"
        + "<details><summary>AI 답 (1회차)</summary>\n\n"
        + r["answer"][:6000]
        + "\n\n</details>\n"
    )


def _client(key_env: str | None) -> LLMClient:
    st = Settings()
    if not key_env:
        return build_llm_client(st)
    from dotenv import dotenv_values
    from pydantic import SecretStr

    from task_spec.llm.openai_compat import OpenAICompatBackend

    key = dotenv_values(".env").get(key_env) or os.environ.get(key_env)
    if not key:
        raise SystemExit(f"{key_env} 가 .env·환경 변수에 없습니다")
    return LLMClient(
        {
            "openai": OpenAICompatBackend(api_key=SecretStr(key), timeout=st.llm_timeout_seconds),
            "opencode": OpenAICompatBackend(
                api_key=st.opencode_api_key,
                base_url=st.opencode_base_url,
                timeout=st.llm_timeout_seconds,
            ),
        }
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument(
        "--budget", type=float, default=20.0, help="누적 비용(USD) 이 넘으면 새 호출을 멈춘다"
    )
    ap.add_argument("--limit", type=int, default=0, help="앞에서 N 문항만 (시험용)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument(
        "--generate",
        choices=["dev", "test"],
        default=None,
        help="저장된 과제 대신 지금 프롬프트로 새로 생성",
    )
    ap.add_argument("--prompts-root", type=Path, default=ROOT / "prompts")
    ap.add_argument(
        "--openai-key-env",
        default=None,
        help="이번 실행에서 OpenAI 키를 읽을 .env 변수 이름 (별도 예산 키 — 값은 출력하지 않음)",
    )
    ap.add_argument("--tag", default="")
    ap.add_argument(
        "--from-report", type=Path, default=None, help="이전 실행 report.json 의 과제를 다시 씀"
    )
    ap.add_argument("--subs", nargs="*", default=None)
    ap.add_argument("--kind", choices=["common", "extension"], default=None)
    ap.add_argument("--solver", default=None, help="풀이 모델 바꾸기 (예: openai:gpt-5.4-mini)")
    a = ap.parse_args()
    global SOLVER
    SOLVER = a.solver

    if a.from_report:
        items = load_report_items(a.from_report, set(a.subs) if a.subs else None, a.kind)
    elif a.generate:
        items = await generate_items(a.generate, a.prompts_root)
    else:
        items = load_items()
    if a.limit:
        items = items[: a.limit]
    print(
        f"문항 {len(items)} (공통 {sum(i['kind'] == 'common' for i in items)}, 확장 {sum(i['kind'] == 'extension' for i in items)}) × {a.runs}회"
    )
    llm = _client(a.openai_key_env)
    budget = Budget(a.budget)
    sem = asyncio.Semaphore(a.concurrency)
    results = await asyncio.gather(
        *(one(llm, it, r, budget, sem) for it in items for r in range(1, a.runs + 1))
    )
    k = 0
    for it in items:
        it["runs"] = list(results[k : k + a.runs])
        k += a.runs
        it["stats"] = item_stats(it)
    report = {
        "runs": a.runs,
        "spent_usd": round(budget.spent, 3),
        "skipped_budget": sum(1 for r in results if r.get("skipped")),
        "errors": [r["error"] for r in results if r.get("error")],
        "common": summarize(items, "common"),
        "extension": summarize(items, "extension"),
        "solve_out_tokens_mean": round(
            mean(r["solve_out_tokens"] for r in results if "solve_out_tokens" in r), 0
        )
        if any("solve_out_tokens" in r for r in results)
        else None,
    }
    out = EVAL / f"{date.today()}-solo{('-' + a.tag) if a.tag else ''}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({**report, "items": items}, ensure_ascii=False, indent=1), "utf-8"
    )
    (out / "samples.md").write_text("\n---\n\n".join(sample_md(i) for i in items), "utf-8")
    print(json.dumps(report, ensure_ascii=False))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
