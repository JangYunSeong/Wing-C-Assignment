"""선택 프롬프트 버전 비교 — 저장된 주장으로 retrieve_t1(검색·선택) + compare 만 다시 돌려 대조 겹침을 채점한다.

왜: 주장 추출은 실행마다 흔들린다. 선택 프롬프트 효과만 보려면 주장을 고정해야 한다.
입력: run_overlap report(주장·가명화본 참조가 저장된 것) + overlap_gold 최종 정답(gold.json).
층 규칙(L0 겹침·L1 가까운 역량·L2 공고 매처)은 run_overlap.analyse 를 그대로 쓴다.
LLM 에는 가명화본에서 다시 만든 주장 텍스트만 간다 (retrieve 노드와 같은 경로 — 불변식 2).
L2 매처만 원문 이력서를 로컬에서 읽는다 (외부 호출 없음, run_overlap 과 같음).
결과는 git 밖 data/ 에만 쓴다 (지원자 데이터).
사용:
  uv run python -m eval.retrieve.run_select_rerun --report <overlap3 report.json> --gold <gold.json> \\
      --resumes <원문 이력서 폴더> --versions 1 2 --runs 2 --tag select-rerun
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eval.retrieve.overlap_gold import LAYERS, STANDARDS, score
from eval.retrieve.run_overlap import analyse
from task_spec.config import Settings
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.compare import make_compare
from task_spec.nodes.retrieve import make_retrieve_t1
from task_spec.ontology.queries import posting_profile
from task_spec.ontology.relatedness import load_relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.suggest import load_lists
from task_spec.prompting import PromptRegistry, PromptTemplate
from task_spec.retrieve import build_retriever
from task_spec.schemas import Claim, PseudoDocRef, RunState

ROOT = Path(__file__).resolve().parents[2]
TASK = "select_competency"


class Pinned:
    """PromptRegistry 처럼 보이되 한 과제만 지정 버전으로 고정한다 (CURRENT 를 바꾸지 않고 비교)."""

    def __init__(self, base: PromptRegistry, task: str, version: int) -> None:
        self.base, self.task, self.version = base, task, version

    def get(self, task: str, version: int | None = None) -> PromptTemplate:
        return self.base.get(task, self.version if task == self.task else version)


def claims_of(row: dict[str, Any]) -> list[Claim]:
    doc_id = row["pseudo_doc"]["doc_id"]
    return [
        Claim(
            claim_id=c["claim_id"],
            doc_id=doc_id,
            action=c["action"],
            result=c.get("result"),
            quote=c["quote"],
            field=c.get("field"),
            role=c.get("role"),
        )
        for c in row["claims"]
    ]


def gt_of(gold: dict[str, Any], resume: str) -> dict[str, Any]:
    """analyse 가 받는 판정 모양(label·evidence) — 원인 분석은 score 가 gold 근거 전부로 다시 한다."""
    return {
        cid: SimpleNamespace(label=e["label"], evidence=(e["evidence"] or [""])[0])
        for cid, e in gold["resumes"].get(resume, {}).items()
    }


def pick_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = [len(v["picked"]) for r in rows for v in r["per_claim"].values()]
    return {
        "claims": len(n),
        "picks": sum(n),
        "per_claim": round(sum(n) / len(n), 2) if n else None,
        "dist": {k: n.count(k) for k in range(4)},
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--gold", type=Path, required=True)
    ap.add_argument("--resumes", type=Path, required=True, help="L2 매처용 원문 이력서 폴더 (로컬)")
    ap.add_argument("--versions", nargs="+", type=int, default=[1, 2])
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--tag", default="select-rerun")
    a = ap.parse_args()

    base_rows = [r for r in json.loads(a.report.read_text("utf-8"))["rows"] if "table" in r]
    gold = json.loads(a.gold.read_text("utf-8"))

    st = Settings()
    repo = await OntologyRepository.open(st.database_url.get_secret_value(), st.ontology_version)
    lists = await load_lists(repo)
    retriever = await build_retriever(repo, lists)
    rel = await load_relatedness(repo)
    profiles = {pid: await posting_profile(repo, pid) for pid in {r["posting"] for r in base_rows}}
    ontology_version = repo.version.version
    await repo.close()
    registry = PromptRegistry(ROOT / "prompts")
    llm = build_llm_client(st)
    compare = make_compare(rel)
    sem = asyncio.Semaphore(a.concurrency)

    async def one(row: dict[str, Any], version: int) -> dict[str, Any]:
        async with sem:
            retrieve = make_retrieve_t1(retriever, llm, Pinned(registry, TASK, version))
            state = RunState(
                run_id=row["run_id"],
                ontology_version=ontology_version,
                posting_id=row["posting"],
                posting_profile=profiles[row["posting"]],
                pseudo_docs=[PseudoDocRef.model_validate(row["pseudo_doc"])],
                claims=claims_of(row),
            )
            r = await retrieve(state)
            s2 = state.model_copy(update={"candidates": r["candidates"]})
            cmp = (await compare(s2))["comparison"]
            raw = (a.resumes / f"{row['resume']}.txt").read_text("utf-8")
            out = analyse(s2, cmp, gt_of(gold, row["resume"]), raw, lists, rel, retriever)
            return {
                "resume": row["resume"],
                "posting": row["posting"],
                "version": version,
                "errors": [e.code for e in r.get("errors", [])],
                "cost_usd": sum(p.cost_usd or 0.0 for p in r.get("provenance", [])),
                "prompt_ref": next((p.prompt for p in r.get("provenance", [])), None),
                **out,
            }

    results: dict[str, Any] = {}
    all_rows: list[dict[str, Any]] = []
    for v in a.versions:
        per_run = []
        for i in range(a.runs):
            rows = await asyncio.gather(*(one(r, v) for r in base_rows))
            all_rows += [{**x, "run": i} for x in rows]
            per_run.append(
                {
                    "all": score(rows, gold),
                    "no_low": score(rows, gold, exclude_low=True),
                    "picks": pick_stats(rows),
                    "errors": sum(len(x["errors"]) for x in rows),
                    "cost_usd": round(sum(x["cost_usd"] for x in rows), 4),
                }
            )
        results[f"v{v}"] = per_run

    out_dir = Path(st.data_dir) / "eval" / "overlap" / f"{date.today()}-{a.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps({"results": results, "rows": all_rows}, ensure_ascii=False, indent=1), "utf-8"
    )
    lines = [f"# 선택 버전 비교 (주장 고정) — {date.today()}", ""]
    for subset in ("all", "no_low"):
        lines += [
            f"## {subset}",
            "",
            "| 버전·run | 기준 | 층 | 예측 | 정답 | 정밀도 | 재현율 |",
            "|---|---|---|---|---|---|---|",
        ]
        for vk, runs in results.items():
            for i, res in enumerate(runs):
                for s in STANDARDS:
                    for layer in LAYERS:
                        m = res[subset]["same_standard"][s][layer]
                        lines.append(
                            f"| {vk}·{i} | {s} | {layer} | {m['predicted']} | {m['gold']} "
                            f"| {m['precision']} | {m['recall']} |"
                        )
        lines.append("")
    lines += ["## 놓친 원인·선택 수·비용", ""]
    for vk, runs in results.items():
        for i, res in enumerate(runs):
            lines.append(
                f"- {vk}·{i}: 원인 {res['all']['missed_action_causes']} · 선택 {res['picks']} "
                f"· 오류 {res['errors']} · ${res['cost_usd']}"
            )
    text = "\n".join(lines)
    (out_dir / "summary.md").write_text(text, "utf-8")
    print(text)
    print(f"→ {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
