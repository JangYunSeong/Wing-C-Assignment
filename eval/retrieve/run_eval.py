"""retrieve T1 평가 — 비교 팔(#59 §7): 패턴 / BM25 / 벡터 / 하이브리드 / 하이브리드+벡터, 범위 hard·soft.

표본
- gold   eval/retrieve/gold/claims_gold_v1.jsonl (가상 지원자 주장 40건, 정답 역량). 정답 없음(uncovered)은 재현율에서 뺀다.
         dev = 홀수 id, test = 짝수 id. 조정은 dev·silver 로만 하고 test 는 마지막에 한 번.
- silver 시범 직군 공고 문장(검수된 line_competency). 예문 채널에서 같은 공고(포지션) 문장은 뺀다.
         정답이 매처 결과라 pattern 팔은 자명하게 100% — 문체도 달라 최종 지표가 아니다(예비 시험).

지표: recall@k (정답 역량 중 상위 k 안 비율, 미시 평균), hit@k (정답 하나라도), 평균 순위.
벡터: --vector 일 때만. OpenAI 임베딩(가상 주장·공고 문장만 보낸다 — 지원자 데이터 아님), data/cache 에 캐시.

사용: uv run python -m eval.retrieve.run_eval --split dev [--vector] [--show-misses]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from task_spec.config import Settings
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.suggest import load_lists
from task_spec.retrieve.cards import CardSet, load_cards
from task_spec.retrieve.t1 import Channel, Scope, T1Retriever

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
GOLD = HERE / "gold/claims_gold_v1.jsonl"
CACHE = ROOT / "data/cache/embeddings"
EMB_MODEL = "text-embedding-3-small"
KS = (1, 3, 5, 10)

ARMS: dict[str, tuple[tuple[Channel, ...], Scope]] = {
    "pattern": (("pattern",), "hard"),
    "bm25_card": (("card",), "hard"),
    "bm25_example": (("example",), "hard"),
    "bm25": (("card", "example"), "hard"),
    "hybrid": (("pattern", "card", "example"), "hard"),
    "hybrid_soft": (("pattern", "card", "example"), "soft"),
}
VECTOR_ARMS: dict[str, tuple[tuple[Channel, ...], Scope]] = {
    "vector": (("vector",), "hard"),
    "bm25+vector": (("card", "example", "vector"), "hard"),
    "hybrid+vector": (("pattern", "card", "example", "vector"), "hard"),
    "hybrid+vector_soft": (("pattern", "card", "example", "vector"), "soft"),
}


def load_gold(split: str) -> list[dict[str, Any]]:
    items = [json.loads(x) for x in GOLD.read_text().splitlines() if x.strip()]
    if split == "all":
        return items
    want = 1 if split == "dev" else 0
    return [x for x in items if int(x["id"][1:]) % 2 == want]


async def load_silver(repo: OntologyRepository) -> list[dict[str, Any]]:
    rows = await repo.fetch(
        "SELECT l.id, l.position_id, p.sub, l.text, array_agg(lc.competency_id) AS comps "
        "FROM posting_line l JOIN position p ON p.id = l.position_id "
        "JOIN line_competency lc ON lc.line_id = l.id WHERE p.in_pilot GROUP BY 1, 2, 3, 4 ORDER BY 1"
    )
    return [
        {
            "id": r["id"],
            "sub": r["sub"],
            "text": r["text"],
            "gold": sorted(r["comps"]),
            "exclude": frozenset({r["position_id"]}),
        }
        for r in rows
    ]


# ---------- 벡터 (평가 팔 전용) ----------
class Embedder:
    def __init__(self, api_key: str) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)
        CACHE.mkdir(parents=True, exist_ok=True)
        self.path = CACHE / f"{EMB_MODEL}.jsonl"
        self.cache: dict[str, list[float]] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                d = json.loads(line)
                self.cache[d["h"]] = d["v"]
        self.tokens = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        hs = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
        todo = list(dict.fromkeys(h for h in hs if h not in self.cache))
        by_h = dict(zip(hs, texts, strict=True))
        with self.path.open("a") as f:
            for i in range(0, len(todo), 256):
                chunk = todo[i : i + 256]
                r = await self.client.embeddings.create(
                    model=EMB_MODEL, input=[by_h[h] for h in chunk]
                )
                self.tokens += r.usage.total_tokens
                for h, d in zip(chunk, r.data, strict=True):
                    self.cache[h] = d.embedding
                    f.write(json.dumps({"h": h, "v": d.embedding}) + "\n")
        return [self.cache[h] for h in hs]


class VectorIndex:
    """역량별 최대 유사도 (카드 본문 1개 + 예문들). 같은 공고 예문은 뺀다."""

    def __init__(self, cards: CardSet, card_vecs: Any, ex_vecs: Any) -> None:
        import numpy as np

        self.np = np
        self.ids = list(cards.cards)
        self.card = self._norm(np.asarray(card_vecs))
        self.ex = self._norm(np.asarray(ex_vecs))
        self.examples = cards.examples

    def _norm(self, m: Any) -> Any:
        return m / self.np.linalg.norm(m, axis=1, keepdims=True)

    def rank(self, qv: list[float], exclude: frozenset[str] = frozenset()) -> list[str]:
        np = self.np
        q = np.asarray(qv)
        q = q / np.linalg.norm(q)
        best = dict(zip(self.ids, (self.card @ q).tolist(), strict=True))
        sims = self.ex @ q
        for i in np.argsort(-sims)[:300]:
            x = self.examples[int(i)]
            if x.position_id in exclude:
                continue
            for c in x.comps:
                if c in best and sims[i] > best[c]:
                    best[c] = float(sims[i])
        return [c for c, _ in sorted(best.items(), key=lambda x: -x[1])]


# ---------- 채점 ----------
def _ndcg(top: list[str], gold: list[str], accept: list[str], k: int = 10) -> float:
    """등급형 적합성: gold=2, accept=1, 나머지 0."""
    gain = {**{c: 1 for c in accept}, **{c: 2 for c in gold}}
    dcg = sum(gain.get(c, 0) / math.log2(i + 2) for i, c in enumerate(top[:k]))
    ideal = sorted(gain.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


Row = tuple[list[str], list[str], list[str]]  # (상위 목록, gold, accept)


def score(results: list[Row]) -> dict[str, float]:
    """strict = gold 만, lenient = gold ∪ accept. 정답 없음(uncovered) 항목은 넣지 않는다."""
    out: dict[str, float] = {}
    n_gold = sum(len(g) for _, g, _ in results)
    for k in KS:
        got = sum(1 for top, g, _ in results for c in g if c in top[:k])
        out[f"R@{k}"] = round(got / n_gold, 3) if n_gold else 0.0
    n_len = sum(len(set(g) | set(a)) for _, g, a in results)
    got = sum(1 for top, g, a in results for c in set(g) | set(a) if c in top[:10])
    out["lenient_R@10"] = round(got / n_len, 3) if n_len else 0.0
    out["nDCG@10"] = (
        round(sum(_ndcg(t, g, a) for t, g, a in results) / len(results), 3) if results else 0.0
    )
    ranks = [top.index(c) + 1 if c in top else 21 for top, g, _ in results for c in g]
    out["mean_rank(miss=21)"] = round(sum(ranks) / len(ranks), 2) if ranks else 0.0
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--vector", action="store_true")
    ap.add_argument("--no-silver", action="store_true")
    ap.add_argument("--show-misses", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    s = Settings()
    repo = await OntologyRepository.open(s.database_url.get_secret_value(), "dev")
    lists = await load_lists(repo)
    assert lists.matcher is not None
    cards = await load_cards(repo, lists.matcher)
    ret = T1Retriever(cards, lists.matcher)
    gold = [
        {
            "id": x["id"],
            "sub": x["sub"],
            "text": f"{x['action']}. {x['quote']}",
            "gold": x["gold"],
            "accept": x.get("accept", []),
            "kind": x.get("kind", "normal"),
            "exclude": frozenset(),
        }
        for x in load_gold(a.split)
    ]
    sets: dict[str, list[dict[str, Any]]] = {"gold": [x for x in gold if x["gold"]]}
    uncovered = [x for x in gold if not x["gold"]]
    if not a.no_silver:
        sets["silver"] = await load_silver(repo)
    await repo.close()

    arms = dict(ARMS)
    vec: VectorIndex | None = None
    emb: Embedder | None = None
    if a.vector:
        arms.update(VECTOR_ARMS)
        emb = Embedder(s.openai_api_key.get_secret_value())
        cv = await emb.embed([cards.cards[i].doc() for i in cards.cards])
        ev = await emb.embed([x.text for x in cards.examples])
        vec = VectorIndex(cards, cv, ev)

    table: dict[str, dict[str, dict[str, float]]] = {}
    misses: dict[str, list[str]] = {}
    by_kind: dict[str, list[Row]] = {}
    for name, items in sets.items():
        pre = [ret.channel_ranks(x["text"], exclude_positions=x["exclude"]) for x in items]
        vranks: list[list[str] | None] = [None] * len(items)
        if vec is not None and emb is not None:
            qv = await emb.embed([x["text"] for x in items])
            vranks = [vec.rank(v, x["exclude"]) for v, x in zip(qv, items, strict=True)]
        for arm, (chs, scope) in arms.items():
            res = []
            for x, p, vr in zip(items, pre, vranks, strict=True):
                hits = ret.search(
                    x["text"], x["sub"], channels=chs, scope=scope, k=20, precomputed=p, vector=vr
                )
                top = [h.competency_id for h in hits]
                res.append((top, x["gold"], x.get("accept", [])))
                if name == "gold" and arm == "hybrid_soft":
                    by_kind.setdefault(x["kind"], []).append(res[-1])
                if name == "gold" and arm == "hybrid_soft" and not set(x["gold"]) <= set(top[:10]):
                    misses.setdefault(arm, []).append(
                        f"{x['id']} {x['sub']} gold={x['gold']} top5={top[:5]} | {x['text'][:50]}"
                    )
            table.setdefault(name, {})[arm] = score(res)

    lines = [
        f"# retrieve T1 평가 — {date.today()} split={a.split} {a.tag}",
        "",
        f"온톨로지 {cards.version} · 카드 {len(cards.cards)} · 예문 {len(cards.examples)}"
        + (f" · 임베딩 {EMB_MODEL} 토큰 {emb.tokens}" if emb else ""),
        "",
    ]
    for name, rows in table.items():
        n = len(sets[name])
        lines += [
            f"## {name} (n={n})",
            "",
            "| 팔 | " + " | ".join(next(iter(rows.values()))) + " |",
        ]
        lines.append("|---" * (1 + len(next(iter(rows.values())))) + "|")
        for arm, m in rows.items():
            lines.append(f"| {arm} | " + " | ".join(str(v) for v in m.values()) + " |")
        lines.append("")
    if by_kind:
        lines += [
            "## gold 종류별 (hybrid_soft)",
            "",
            "| 종류 | n | R@10 | lenient_R@10 | nDCG@10 |",
            "|---|---|---|---|---|",
        ]
        for kind, rows in sorted(by_kind.items()):
            m = score(rows)
            lines.append(
                f"| {kind} | {len(rows)} | {m['R@10']} | {m['lenient_R@10']} | {m['nDCG@10']} |"
            )
        lines.append("")
    if uncovered:
        lines += ["## 정답 없음(uncovered) — hybrid 상위 3", ""]
        for x in uncovered:
            hits = ret.search(x["text"], x["sub"], k=3)
            lines.append(
                f"- {x['id']}: " + ", ".join(f"{h.competency_id}({h.score:.4f})" for h in hits)
            )
        lines.append("")
    if a.show_misses:
        for arm, ms in misses.items():
            lines += [f"## gold 놓침 ({arm}, @10)", ""] + [f"- {m}" for m in ms] + [""]
    report = "\n".join(lines)
    print(report)
    out = HERE / "reports" / f"{date.today()}_{a.split}{('_' + a.tag) if a.tag else ''}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(report)


if __name__ == "__main__":
    asyncio.run(main())
