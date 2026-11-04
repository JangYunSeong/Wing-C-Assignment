"""검색어 변형 실험 (결정적·무료, LLM 없음) — 주장을 고정하고 검색어만 바꿔 정답 역량의 순위를 비교한다.

왜: 대조 겹침에서 '정답(행동으로 보임)인데 근거 주장의 검색 후보에 없음'(검색 누락)이 남는다. 특히 성과를 수치·결과로
쓴 주장(응답 시간 단축 등)에서 성능 역량이 빠진다. retrieve 노드의 검색어는 `행동. 인용` 이다.
입력: run_overlap report(주장·인용·top30 이 저장된 판, overlap3 이후) + run_judge_consensus 의 consensus.json(합의 정답).
     정답 action 역량마다 근거 주장(evidence_claims)들의 검색 결과에서 가장 좋은 순위를 변형별로 잰다.
변형: base(행동. 인용) / +결과 / 인용만 / 행동만 / 행동+결과 / 결과 표현 정규화(일반 어휘 덧붙임) / 프로젝트 맥락(제목·문제 정의 줄).
출력: <consensus 폴더>-search/ (report.json, samples.md) — git 밖 data/.
사용: uv run python -m eval.retrieve.run_search_variants --report <overlap3>/report.json --consensus <..>/consensus.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from statistics import mean
from typing import Any

from task_spec.config import Settings
from task_spec.llm.quotes import compact_for_quote
from task_spec.nodes.pseudonymize import load_pseudonymized
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.suggest import load_lists
from task_spec.retrieve import build_retriever
from task_spec.schemas import PseudoDocRef

K_MAX = 200  # 순위를 재기 위한 넉넉한 검색 폭 (노드는 10)
CUTS = (10, 20, 30)
_PERIOD = re.compile(r"\d{4}\.\d{1,2}\s*[~\-–]")  # 프로젝트 머리줄의 기간
_MARKER = re.compile(r"^\s*⟦(?P<label>[^⟧]{1,40})⟧\s*")
_PROBLEM_FIELDS = ("문제 정의", "배경")

# 결과 표현 정규화 — 성과를 수치로만 쓴 문장에 그 성과가 가리키는 일반 어휘를 덧붙인다 (가상·일반 어휘만)
_NORM_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(속도|응답|지연|처리\s*시간|latency|tps|throughput|성능)[^.\n]{0,30}"
            r"(단축|향상|개선|감소|줄|빨라|\d+\s*(?:초|ms|%))",
            re.I,
        ),
        "성능 개선 응답 시간 단축 성능 튜닝",
    ),
    (re.compile(r"\d+\s*(?:초|ms)\s*(?:에서|→|->)\s*\d+", re.I), "성능 개선 응답 시간 단축"),
    (re.compile(r"(비용|토큰|리소스|자원)[^.\n]{0,20}(절감|감소|줄)"), "비용 절감 자원 효율화"),
    (re.compile(r"(\d[\d,.]*\s*(?:만|억|천)\s*건|대용량|대량)"), "대용량 데이터 처리"),
]


def norm_terms(*texts: str | None) -> str:
    joined = " ".join(t for t in texts if t)
    return " ".join(dict.fromkeys(term for rx, term in _NORM_RULES if rx.search(joined)))


def project_context(doc: str, quote: str) -> tuple[str, str]:
    """인용이 속한 프로젝트의 머리줄(기간이 있는 줄)과 문제 정의 첫 줄. 못 찾으면 빈 문자열."""
    lines = doc.split("\n")
    head = compact_for_quote(quote)[:20]
    start = -1
    for i, line in enumerate(lines):
        own = compact_for_quote(line)
        # 이어 붙인 몇 줄에서 인용 머리가 **이 줄 안에서** 시작하면 이 줄이 시작 줄
        pos = compact_for_quote("".join(lines[i : i + 4])).find(head) if head and own else -1
        if 0 <= pos < len(own):
            start = i
            break
    if start < 0:
        return "", ""
    header_i = next((j for j in range(start, -1, -1) if _PERIOD.search(lines[j])), -1)
    if header_i < 0:
        return "", ""
    header = _MARKER.sub("", lines[header_i]).strip()
    problem = ""
    for line in lines[header_i + 1 : start + 1]:
        m = _MARKER.match(line)
        if m and m.group("label") in _PROBLEM_FIELDS:
            problem = line[m.end() :].strip()
            break
    return header, problem


Variant = Callable[[dict[str, Any], tuple[str, str]], str]
VARIANTS: dict[str, Variant] = {
    "base(행동.인용)": lambda c, ctx: f"{c['action']}. {c['quote']}",
    "+결과": lambda c, ctx: f"{c['action']}. {c['quote']} {c.get('result') or ''}",
    "인용만": lambda c, ctx: c["quote"],
    "행동만": lambda c, ctx: c["action"],
    "행동+결과": lambda c, ctx: f"{c['action']}. {c.get('result') or ''}",
    "정규화": lambda c, ctx: (
        f"{c['action']}. {c['quote']} {norm_terms(c['quote'], c.get('result'))}"
    ),
    "맥락(제목)": lambda c, ctx: f"{ctx[0]}. {c['action']}. {c['quote']}",
    "맥락(제목+문제)": lambda c, ctx: f"{ctx[0]}. {ctx[1]}. {c['action']}. {c['quote']}",
    "정규화+맥락": lambda c, ctx: (
        f"{ctx[0]}. {c['action']}. {c['quote']} {norm_terms(c['quote'], c.get('result'))}"
    ),
}


def best_rank(ranked: list[list[str]], comp: str) -> int | None:
    """근거 주장 여러 개 중 가장 좋은 순위 (1부터). 어디에도 없으면 None."""
    found = [lst.index(comp) + 1 for lst in ranked if comp in lst]
    return min(found) if found else None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--consensus", type=Path, required=True)
    a = ap.parse_args()
    rep = json.loads(a.report.read_text("utf-8"))
    cons = json.loads(a.consensus.read_text("utf-8"))
    gold: dict[str, dict[str, str]] = cons["gold"]

    st = Settings()
    repo = await OntologyRepository.open(st.database_url.get_secret_value(), st.ontology_version)
    retriever = await build_retriever(repo, await load_lists(repo))
    await repo.close()
    assert retriever is not None, "매칭 규칙 미적재 — 검색기를 만들 수 없음"

    docs: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    no_evidence = 0
    for row in rep["rows"]:
        if "table" not in row:
            continue
        resume = row["resume"]
        if resume not in docs:
            ref = PseudoDocRef.model_validate(row["pseudo_doc"])
            docs[resume] = str(await asyncio.to_thread(load_pseudonymized, ref))
        claims = {c["claim_id"]: c for c in row["claims"]}
        bonus = {x["competency_id"]: bool(x["primary"]) for x in row["table"]}
        for x in row["table"]:
            if gold.get(resume, {}).get(x["competency_id"]) != "action":
                continue
            ev = [claims[c] for c in x["evidence_claims"] if c in claims]
            if not ev:
                no_evidence += 1
                continue
            item: dict[str, Any] = {
                "resume": resume,
                "posting": row["posting"],
                "competency_id": x["competency_id"],
                "label": x["label"],
                "L0": x["L0"],
                "miss_cause": x["miss_cause"],
                "ranks": {},
            }
            ctxs = [project_context(docs[resume], c["quote"]) for c in ev]
            for name, fn in VARIANTS.items():
                ranked = [
                    [
                        h.competency_id
                        for h in retriever.search(
                            fn(c, ctx), row["sub"], scope="soft", posting=bonus, k=K_MAX
                        )
                    ]
                    for c, ctx in zip(ev, ctxs, strict=True)
                ]
                item["ranks"][name] = best_rank(ranked, x["competency_id"])
            # 기준 검색어에서 채널별 순위 (어느 채널이 놓치는지)
            chans: dict[str, int | None] = {}
            for c in ev:
                per = retriever.channel_ranks(VARIANTS["base(행동.인용)"](c, ("", "")))
                for ch, rk in per.items():
                    r = rk.get(x["competency_id"])
                    if r is not None and (chans.get(ch) is None or r < chans[ch]):  # type: ignore[operator]
                        chans[ch] = r
                    chans.setdefault(ch, None)
            item["channels"] = chans
            items.append(item)

    def summary(subset: list[dict[str, Any]]) -> dict[str, Any]:
        out = {}
        for name in VARIANTS:
            rs = [it["ranks"][name] for it in subset]
            out[name] = {
                **{f"r@{k}": round(sum(1 for r in rs if r and r <= k) / len(rs), 3) for k in CUTS},
                "mean_rank(없음=201)": round(mean(r or K_MAX + 1 for r in rs), 1),
            }
        return out

    perf = [it for it in items if "성능" in it["label"]]
    result = {
        "items": len(items),
        "no_evidence_claim": no_evidence,
        "all": summary(items) if items else {},
        "perf": summary(perf) if perf else {},
        "perf_n": len(perf),
        "base_misses_top10": [
            {k: it[k] for k in ("resume", "posting", "label", "miss_cause", "channels")}
            | {"base_rank": it["ranks"]["base(행동.인용)"]}
            for it in items
            if not (it["ranks"]["base(행동.인용)"] or 999) <= 10
        ],
    }
    out = a.consensus.parent.with_name(f"{a.consensus.parent.name}-search")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({**result, "rows": items}, ensure_ascii=False, indent=1), "utf-8"
    )
    md = ["| 변형 | r@10 | r@20 | r@30 | 평균 순위 |", "|---|---|---|---|---|"]
    for name, v in result["all"].items():
        md.append(
            f"| {name} | {v['r@10']} | {v['r@20']} | {v['r@30']} | {v['mean_rank(없음=201)']} |"
        )
    (out / "samples.md").write_text("\n".join(md) + "\n", "utf-8")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "base_misses_top10"}, ensure_ascii=False
        )
    )
    print(json.dumps(result["base_misses_top10"], ensure_ascii=False))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
