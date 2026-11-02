"""T1 선택(select_competency) 평가 — 검색(hybrid, 범위 soft) 상위 10 → LLM 0~2개 선택.

gold: eval/retrieve/gold/claims_gold_v1.jsonl (가상 주장 — 지원자 데이터 아님). dev = 홀수 id, test = 짝수 id.
지표
- precision      고른 역량 중 정답 비율
- recall         정답 역량 중 고른 비율 (정답 있는 주장만)
- exact          고른 집합 = 정답 집합 비율 (정답 없음 주장은 빈 선택이면 정답)
- none_ok        정답 없음 주장에서 아무것도 고르지 않은 비율 (uncovered 표시는 따로 출력)
- ceiling        검색 상위 10 안에 정답이 있는 비율 (선택이 넘을 수 없는 상한)

사용: uv run python -m eval.retrieve.run_select --split dev --models opencode:qwen3.8-flash openai:gpt-4.1-mini --runs 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

from eval.retrieve.run_eval import load_gold
from task_spec.config import Settings
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.pseudonymize import mint_pseudonymized
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.suggest import load_lists
from task_spec.prompting import PromptRegistry, PromptTemplate
from task_spec.retrieve import build_retriever
from task_spec.retrieve.select import SelectInputClaim, select

HERE = Path(__file__).parent
PROMPTS = PromptRegistry(HERE.parents[1] / "prompts")
GPT_PARAMS = {"temperature": 0, "max_tokens": 4096}


class ModelOverride:
    """PromptRegistry 처럼 보이되 모델·설정만 바꾼 템플릿을 준다 (평가 전용)."""

    def __init__(self, base: PromptRegistry, model: str, version: int | None) -> None:
        self.base, self.model, self.version = base, model, version

    def get(self, task: str, version: int | None = None) -> PromptTemplate:
        t = self.base.get(task, self.version or version)
        if t.meta.model == self.model:
            return t
        params = GPT_PARAMS if self.model.startswith("openai:") else t.meta.params
        meta = t.meta.model_copy(update={"model": self.model, "params": params})
        return t.model_copy(update={"meta": meta})


Row = tuple[list[str], list[str], list[str]]  # (고른 것, gold, accept)


def _ok(p: list[str], g: list[str], a: list[str]) -> bool:
    """lenient 완전 일치: gold 를 모두 고르고, 나머지는 accept 안에서만."""
    return set(g) <= set(p) <= set(g) | set(a)


def score(rows: list[Row]) -> dict[str, float]:
    sel = sum(len(p) for p, _, _ in rows)
    tp = sum(len(set(p) & set(g)) for p, g, _ in rows)
    tp_len = sum(len(set(p) & (set(g) | set(a))) for p, g, a in rows)
    gold_n = sum(len(g) for _, g, _ in rows)
    none = [p for p, g, _ in rows if not g]
    return {
        "precision": round(tp / sel, 3) if sel else 0.0,
        "lenient_P": round(tp_len / sel, 3) if sel else 0.0,
        "recall": round(tp / gold_n, 3) if gold_n else 0.0,
        "exact": round(sum(1 for p, g, _ in rows if set(p) == set(g)) / len(rows), 3),
        "lenient_exact": round(sum(1 for r in rows if _ok(*r)) / len(rows), 3),
        "none_ok": round(sum(1 for p in none if not p) / len(none), 3) if none else 1.0,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--models", nargs="+", default=["opencode:qwen3.8-flash"])
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    s = Settings()
    repo = await OntologyRepository.open(s.database_url.get_secret_value(), "dev")
    lists = await load_lists(repo)
    ret = await build_retriever(repo, lists)
    await repo.close()
    assert ret is not None
    llm = build_llm_client(s)
    gold = load_gold(a.split)

    by_sub: dict[str, list[dict[str, Any]]] = {}
    for x in gold:
        by_sub.setdefault(x["sub"], []).append(x)
    hits = {
        x["id"]: ret.search(f"{x['action']}. {x['quote']}", x["sub"], scope="soft", k=10)
        for x in gold
    }
    ceiling = sum(
        1 for x in gold if set(x["gold"]) <= {h.competency_id for h in hits[x["id"]]}
    ) / len(gold)

    report: dict[str, Any] = {
        "split": a.split,
        "n": len(gold),
        "ceiling": round(ceiling, 3),
        "models": {},
    }
    detail: list[str] = []
    for model in a.models:
        reg = ModelOverride(PROMPTS, model, a.version)
        runs: list[dict[str, list[str]]] = []
        unc: dict[str, bool] = {}
        cost = 0.0
        n_err = 0
        for _ in range(a.runs):
            picks: dict[str, list[str]] = {}
            for sub, items in by_sub.items():
                inputs = [
                    SelectInputClaim(
                        claim_id=x["id"],
                        text=mint_pseudonymized(f"행동: {x['action']} / 문장: {x['quote']}"),
                        hits=hits[x["id"]],
                    )
                    for x in items
                ]
                label = f"{sub}: {ret.cards.sub_labels.get(sub, sub)}"
                res = await select(inputs, sub=label, cards=ret.cards, llm=llm, prompts=reg)  # type: ignore[arg-type]
                n_err += len(res.errors)
                cost += sum(p.cost_usd or 0.0 for p in res.provenance)
                for it in res.items:
                    picks[it.claim_id] = it.competency_ids
                    unc[it.claim_id] = it.uncovered
            runs.append(picks)
        rows = [(runs[0][x["id"]], x["gold"], x.get("accept", [])) for x in gold]
        m = score(rows)
        if a.runs > 1:
            m["stability"] = round(
                sum(1 for x in gold if all(r[x["id"]] == runs[0][x["id"]] for r in runs))
                / len(gold),
                3,
            )
        for kind in ("uncovered", "scope_probe", "multi"):
            kr = [r for r, x in zip(rows, gold, strict=True) if x.get("kind") == kind]
            if kr:
                m[f"{kind}_ok"] = round(sum(1 for r in kr if _ok(*r)) / len(kr), 3)
        m["errors"] = n_err
        m["cost_usd"] = round(cost, 4)
        report["models"][model] = m
        for x in gold:
            p = runs[0][x["id"]]
            if not _ok(p, x["gold"], x.get("accept", [])):
                detail.append(
                    f"- {model} {x['id']} gold={x['gold']} pick={p} uncovered={unc.get(x['id'])} | {x['quote'][:40]}"
                )

    lines = [
        f"# T1 선택 평가 — {date.today()} split={a.split} {a.tag}",
        "",
        f"n={len(gold)} · 검색 상한(정답이 상위 10 안) {report['ceiling']} · runs={a.runs}",
        "",
        "| 모델 | " + " | ".join(next(iter(report["models"].values()))) + " |",
        "|---" * (1 + len(next(iter(report["models"].values())))) + "|",
    ]
    for model, m in report["models"].items():
        lines.append(f"| {model} | " + " | ".join(str(v) for v in m.values()) + " |")
    lines += ["", "## 틀린 것", ""] + detail
    text = "\n".join(lines)
    print(text)
    out = HERE / "reports" / f"{date.today()}_{a.split}_select{('_' + a.tag) if a.tag else ''}"
    out.with_suffix(".md").write_text(text)
    if a.split == "test":
        out.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
