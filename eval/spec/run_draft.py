"""공통 과제 서술(draft_task) 모델 비교.

표본: 온톨로지 시범 공고 중 직군마다 1건 (공고 수가 많은 직군 순 최대 12) — 지원자 데이터 아님.
      직군 순서대로 번갈아 dev / test.
자동 지표
- ok_first   첫 답이 자동 검증 통과
- ok_final   재요청 후 최종 통과 (고쳐 쓰기·실패 없이)
- failed     서술 없음 (LLM 실패)
- cite_cov   측정 역량 중 행동 기준이 그 역량의 수행준거를 인용한 비율
- traps, scen_len, cost_usd, latency_s (평균)
판정: judge_draft_task (gpt-4.1) 1~5점 — alignment·trap_quality·anchors·feasibility·ai_axes.
출력: data/eval/spec/<날짜>-<tag>/ (report.json, samples.md, drafts.jsonl) — git 밖.

사용: uv run python -m eval.spec.run_draft --split dev --models openai:gpt-5.5 openai:gpt-5.4-mini \
        opencode:qwen3.8-flash openai:gpt-4.1 [--prompts-root DIR] [--no-judge]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

from task_spec.config import Settings
from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.llm.factory import build_llm_client
from task_spec.ontology.queries import evidence as query_evidence
from task_spec.ontology.queries import posting_profile
from task_spec.ontology.relatedness import load_relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry, PromptTemplate, assemble
from task_spec.schemas import DraftTaskOutput, JudgeDraftInput, JudgeDraftOutput
from task_spec.spec.build import CommonDraft, build_common
from task_spec.spec.draft import FLAG_DRAFT_FAILED, FLAG_DRAFT_INVALID, DraftContext, draft_task

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = PromptRegistry(ROOT / "prompts")
MAX_POSTINGS = 12
JUDGE_DIMS = ("alignment", "trap_quality", "anchors", "feasibility", "ai_axes")
# 모델별 설정 (평가 전용). GPT-5 계열은 temperature 를 받지 않는다 (llm/client.py 가 뺀다).
PARAMS: dict[str, dict[str, Any]] = {
    "openai:gpt-5.5": {"max_tokens": 16000, "reasoning_effort": "medium"},
    "openai:gpt-5.4-mini": {"max_tokens": 16000, "reasoning_effort": "medium"},
    "opencode:qwen3.8-flash": {"temperature": 0.3, "max_tokens": 16000, "reasoning_effort": "low"},
    "openai:gpt-4.1": {"temperature": 0.3, "max_tokens": 8000},
}


class ModelOverride:
    """PromptRegistry 처럼 보이되 모델·설정만 바꾼 템플릿을 준다 (평가 전용)."""

    def __init__(self, base: PromptRegistry, model: str) -> None:
        self.base, self.model = base, model

    def get(self, task: str, version: int | None = None) -> PromptTemplate:
        t = self.base.get(task, version)
        params = PARAMS.get(self.model, t.meta.params)
        meta = t.meta.model_copy(update={"model": self.model, "params": params})
        return t.model_copy(update={"meta": meta})


async def pick_postings(
    repo: OntologyRepository, version: str
) -> list[tuple[str, str, CommonDraft]]:
    """직군마다 서술 입력이 만들어지는 첫 공고 (id 순)."""
    rel = await load_relatedness(repo)
    rows = await repo.fetch(
        "SELECT id, sub FROM position WHERE in_pilot AND sub IS NOT NULL ORDER BY sub, id"
    )
    by_sub: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        by_sub[r["sub"]].append(r["id"])
    subs = sorted(by_sub, key=lambda s: (-len(by_sub[s]), s))
    out: list[tuple[str, str, CommonDraft]] = []
    for sub in subs:
        for pid in by_sub[sub]:
            p = await posting_profile(repo, pid)
            if p is None or not (p.sub and p.industry_id):
                continue
            ev = await query_evidence(
                repo,
                p.competency_ids,
                sub=p.sub,
                industry_id=p.industry_id,
                secondary_sub=p.secondary_sub,
                in_pilot=p.in_pilot,
            )
            built = await build_common(
                p, ev, task_id=f"eval-{pid}", ontology_version=version, ontology=repo, rel=rel
            )
            if built.ctx is not None:
                out.append((sub, pid, built))
                break
        if len(out) >= MAX_POSTINGS:
            break
    return out


def conditions_text(ctx: DraftContext) -> str:
    comps = "\n".join(f"- {t.competency_id} {t.label} (요구 수준 {t.level})" for t in ctx.targets)
    crit = "\n".join(f"[{c.criterion_id}] {c.element_name}: {c.text}" for c in ctx.criteria)
    env = ", ".join(ctx.env.languages + ctx.env.frameworks) or "(언어 없음)"
    return (
        f"직군: {ctx.sub_label}\n업종: {ctx.industry_label}\n난이도: {ctx.difficulty}\n"
        f"제한 시간: {ctx.time_limit_min}분\n기술 환경: {env}\n\n측정 역량:\n{comps}\n\n"
        f"근거 수행준거:\n{crit}"
    )


async def judge_posting(
    llm: LLMClient, ctx: DraftContext, drafts: dict[str, DraftTaskOutput], seed: str
) -> dict[str, dict[str, Any]]:
    """같은 공고의 모델별 초안을 이름을 가리고 섞어 한 번에 비교 채점. 반환: 모델 → 점수·순위."""
    models = sorted(drafts)
    random.Random(seed).shuffle(models)
    refs = {chr(ord("A") + i): m for i, m in enumerate(models)}
    block = "\n\n".join(
        f"[{r}]\n" + json.dumps(drafts[m].model_dump(mode="json"), ensure_ascii=False, indent=1)
        for r, m in refs.items()
    )
    payload = JudgeDraftInput(conditions=conditions_text(ctx), drafts=block)
    prompt = assemble(
        PROMPTS.get("judge_draft_task"), payload, ontology_version=ctx.ontology_version
    )
    try:
        r = await llm.call(prompt, JudgeDraftOutput)
    except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
        return {m: {"error": type(exc).__name__} for m in models}
    cost = (r.provenance.cost_usd or 0.0) / len(models)
    rank = {ref: i + 1 for i, ref in enumerate(r.parsed.ranking)}
    out: dict[str, dict[str, Any]] = {}
    for item in r.parsed.items:
        m = refs.get(item.ref)
        if m is not None:
            out[m] = {
                **item.model_dump(exclude={"ref"}),
                "rank": rank.get(item.ref),
                "cost_usd": cost,
            }
    return out


def cite_cov(ctx: DraftContext, out: DraftTaskOutput, anchors_ok: bool) -> float:
    if not anchors_ok:
        return 0.0
    a = out.behavioral_anchors
    cited = (
        set(a.level_1.criterion_ids) | set(a.level_3.criterion_ids) | set(a.level_5.criterion_ids)
    )
    hit = sum(1 for t in ctx.targets if cited & set(t.ncs_criterion_ids))
    return hit / len(ctx.targets)


async def run_one(llm: LLMClient, reg: ModelOverride, ctx: DraftContext) -> dict[str, Any]:
    res = await draft_task(ctx, llm=llm, prompts=reg)  # type: ignore[arg-type]
    first_invalid = any(e.code == "draft_invalid" for e in res.errors)
    failed = FLAG_DRAFT_FAILED in res.flags
    invalid = [f for f in res.flags if f.startswith(FLAG_DRAFT_INVALID)]
    row: dict[str, Any] = {
        "ok_first": not failed
        and not first_invalid
        and not any(e.code != "draft_invalid" for e in res.errors),
        "ok_final": not failed and not invalid,
        "failed": failed,
        "problems": invalid,
        "errors": [e.code for e in res.errors],
        "cost_usd": sum(p.cost_usd or 0.0 for p in res.provenance),
        "latency_s": sum(p.latency_ms for p in res.provenance) / 1000,
        "calls": len(res.provenance),
        "out_tokens": sum(p.tokens.out for p in res.provenance),
    }
    if res.output is not None:
        row["cite_cov"] = cite_cov(ctx, res.output, res.anchors_ok)
        row["traps"] = len(res.output.traps)
        row["scen_len"] = len(res.output.scenario)
        row["draft"] = res.output.model_dump(mode="json")
        row["_output"] = res.output
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def avg(key: str) -> float:
        xs = [r[key] for r in rows if key in r]
        return round(mean(xs), 3) if xs else 0.0

    s: dict[str, Any] = {
        "n": len(rows),
        "ok_first": avg("ok_first"),
        "ok_final": avg("ok_final"),
        "failed": avg("failed"),
        "cite_cov": avg("cite_cov"),
        "traps": avg("traps"),
        "scen_len": avg("scen_len"),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "latency_s": avg("latency_s"),
        "out_tokens": avg("out_tokens"),
    }
    judged = [r["judge"] for r in rows if "judge" in r and "error" not in r["judge"]]
    for d in JUDGE_DIMS:
        s[f"j_{d}"] = round(mean(j[d] for j in judged), 2) if judged else None
    ranks = [j["rank"] for j in judged if j.get("rank")]
    s["j_rank"] = round(mean(ranks), 2) if ranks else None
    if judged:
        s["j_mean"] = round(mean(mean(j[d] for d in JUDGE_DIMS) for j in judged), 2)
        s["judge_cost_usd"] = round(sum(j["cost_usd"] for j in judged), 4)
    return s


def sample_md(model: str, sub: str, pid: str, row: dict[str, Any]) -> str:
    d = row.get("draft")
    if d is None:
        return f"### {model} · {sub} · {pid}\n\n(서술 없음: {row['errors']})\n"
    a = d["behavioral_anchors"]
    traps = "\n".join(
        f"- **{t['trap_id']}** {t['description']} — AI: {t['why_ai_fails']}" for t in d["traps"]
    )
    j = row.get("judge", {})
    js = " · ".join(f"{k} {j[k]}" for k in (*JUDGE_DIMS, "rank") if k in j)
    return (
        f"### {model} · {sub} · {pid}\n\n**{d['title']}**  \n판정: {js}  \n"
        f"자동 검증: {'통과' if row['ok_final'] else '; '.join(row['problems']) or row['errors']}\n\n"
        f"{d['scenario']}\n\n제출물: " + " / ".join(d["deliverables"]) + "\n\n"
        f"심층 구조: {d['invariant']}  \n표면: {', '.join(d['variable'])}\n\n함정\n{traps}\n\n"
        f"행동 기준\n- 1: {a['level_1']['behavior']} {a['level_1']['criterion_ids']}\n"
        f"- 3: {a['level_3']['behavior']} {a['level_3']['criterion_ids']}\n"
        f"- 5: {a['level_5']['behavior']} {a['level_5']['criterion_ids']}\n\n"
        f"판정 지적: " + " / ".join(j.get("issues", [])) + "\n"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--models", nargs="+", default=list(PARAMS))
    ap.add_argument("--prompts-root", type=Path, default=ROOT / "prompts")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    s = Settings()
    repo = await OntologyRepository.open(s.database_url.get_secret_value(), "dev")
    version = repo.version.version
    picked = await pick_postings(repo, version)
    await repo.close()
    split = {"dev": 0, "test": 1}
    picked = [x for i, x in enumerate(picked) if a.split == "all" or i % 2 == split[a.split]]
    print(f"표본 {len(picked)}건: " + ", ".join(f"{sub}={pid}" for sub, pid, _ in picked))

    llm = build_llm_client(s)
    base = PromptRegistry(a.prompts_root)
    out_dir = (
        ROOT
        / "data"
        / "eval"
        / "spec"
        / f"{date.today()}-{a.split}{('-' + a.tag) if a.tag else ''}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"split": a.split, "postings": [p for _, p, _ in picked], "models": {}}
    rows: dict[str, list[dict[str, Any]]] = {}
    for model in a.models:
        reg = ModelOverride(base, model)
        rows[model] = list(
            await asyncio.gather(*(run_one(llm, reg, b.ctx) for _, _, b in picked if b.ctx))
        )
    if not a.no_judge:
        for i, (_, pid, b) in enumerate(picked):
            assert b.ctx is not None
            drafts = {m: rows[m][i]["_output"] for m in a.models if "_output" in rows[m][i]}
            if not drafts:
                continue
            for m, j in (await judge_posting(llm, b.ctx, drafts, seed=pid)).items():
                rows[m][i]["judge"] = j
    samples: list[str] = []
    with (out_dir / "drafts.jsonl").open("w", encoding="utf-8") as f:
        for model in a.models:
            for (sub, pid, _), row in zip(picked, rows[model], strict=True):
                row.pop("_output", None)
                rec = {"model": model, "sub": sub, "posting": pid, **row}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                samples.append(sample_md(model, sub, pid, row))
            report["models"][model] = summarize(rows[model])
            print(model, json.dumps(report["models"][model], ensure_ascii=False))
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    (out_dir / "samples.md").write_text("\n---\n\n".join(samples), "utf-8")
    print(f"→ {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
