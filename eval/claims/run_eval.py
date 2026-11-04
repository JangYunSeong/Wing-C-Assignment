"""주장 추출 평가 — 실제 LLM 호출 (비용 발생, 입력은 전부 가상).

모델 출력 주장을 정답 문장에 대응시킨다: 인용(정규화)이 정답 문장을 포함하거나 포함되거나 유사도 ≥ 0.8.
지표
- sentence_recall   행동 문장(actions≥1) 중 1건 이상 대응된 비율
- action_recall     Σ min(대응 주장 수, 행동 수) / Σ 행동 수
- split_rate        행동 2개 문장 중 주장 2건 이상으로 나눈 비율 (프롬프트 규칙 1)
- precision         대응된 주장 중 행동 문장에 대응된 비율 (행동이 아닌 문장·대응 없는 주장은 오답)
- non_action_rate   행동이 아닌 문장(문제 정의 맥락·지원 동기 등)을 뽑은 비율
- quote_ok          인용이 원문에 글자 그대로 있는 비율 (verify_quote)
- field_acc / role_acc  대응된 행동 주장 중 칸·역할이 정답과 같은 비율
- stability         같은 입력 2회 실행에서 대응된 정답 문장 집합의 Jaccard 평균

사용: uv run python -m eval.claims.run_eval --split dev --models opencode:qwen3.8-flash openai:gpt-4.1-mini
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from eval.claims.generate import Doc, docs
from task_spec.config import Settings
from task_spec.llm import LLMClient, normalize_for_quote, verify_quote
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.claims import extract_parts
from task_spec.nodes.pseudonymize import mint_pseudonymized
from task_spec.nodes.roles import decide_role, locate_field
from task_spec.prompting import PromptRegistry, PromptTemplate
from task_spec.schemas import Claim

HERE = Path(__file__).parent
PROMPTS = PromptRegistry(HERE.parents[1] / "prompts")
GPT_PARAMS = {"temperature": 0, "max_tokens": 2048}  # gpt-4.1-mini 는 reasoning_effort 미지원


def _with_model(template: PromptTemplate, model: str) -> PromptTemplate:
    """평가 비교용으로 템플릿의 모델·설정만 바꾼다 (본문·sha256 그대로)."""
    if template.meta.model == model:
        return template
    params = GPT_PARAMS if model.startswith("openai:") else template.meta.params
    meta = template.meta.model_copy(update={"model": model, "params": params})
    return template.model_copy(update={"meta": meta})


def _sim(quote: str, sentence: str) -> float:
    """1.0 = 포함 관계, 그 외 유사도. 0.8 미만은 대응 안 함."""
    q, s = normalize_for_quote(quote), normalize_for_quote(sentence)
    if not q:
        return 0.0
    if q == s or q in s or s in q:
        return 1.0 + min(len(q), len(s)) / max(len(q), len(s))  # 포함이면 길이가 비슷할수록 우선
    r = SequenceMatcher(None, q, s).ratio()
    return r if r >= 0.8 else 0.0


def _match(quote: str, sentence: str) -> bool:
    return _sim(quote, sentence) > 0


def _best(quote: str, sentences):  # type: ignore[no-untyped-def]
    scored = [(_sim(quote, s.text), s) for s in sentences]
    scored = [x for x in scored if x[0] > 0]
    return max(scored, key=lambda x: x[0])[1] if scored else None


async def _extract(
    client: LLMClient, doc: Doc, model: str, version: int | None = None
) -> tuple[list[Claim], dict]:
    """운영 노드와 같은 경로(nodes.claims.extract_parts — 출력 잘림이면 나눠 다시)로 뽑는다."""
    text = mint_pseudonymized(doc.text)
    template = _with_model(PROMPTS.get("extract_claims", version), model)
    got = await extract_parts(
        client, template, doc.kind, text, ontology_version="eval", doc_id=doc.id
    )
    provs = [p for _, _, p in got.parts] + got.spent
    if not got.parts:  # 스키마 위반·빈 응답 등 — 이 문서는 0건으로
        return [], {"error": got.failures[0] if got.failures else "EmptyResponse"}
    claims = []
    i = 0
    for _part, parsed, _prov in got.parts:
        for it in parsed.claims:
            i += 1
            c = Claim(
                claim_id=f"{doc.id}-c{i}",
                doc_id=doc.id,
                action=it.action,
                quote=it.quote,
                field=locate_field(doc.text, it.quote),
            )
            role, _ = decide_role(c)
            claims.append(c.model_copy(update={"role": role}))
    return claims, {
        "retries": sum(p.retries for p in provs),
        "tokens_out": sum(p.tokens.out for p in provs),
        "tokens_reasoning": sum(p.tokens.reasoning for p in provs),
        "latency_ms": max(p.latency_ms for p in provs),
        "parts": len(got.parts),
        "calls": got.calls,
        "partial": bool(got.failures),
    }


def score(doc: Doc, claims: list[Claim]) -> dict:
    per_sentence: dict[str, list[Claim]] = defaultdict(list)
    unmatched = 0
    for c in claims:
        best = _best(c.quote, doc.sentences)
        if best is None:
            unmatched += 1
            continue
        per_sentence[best.sid].append(c)
    acts = [s for s in doc.sentences if s.actions]
    non = [s for s in doc.sentences if not s.actions]
    matched_action_claims = [(s, c) for s in acts for c in per_sentence.get(s.sid, [])]
    return {
        "sent_hit": sum(1 for s in acts if per_sentence.get(s.sid)),
        "sent_total": len(acts),
        "act_hit": sum(min(len(per_sentence.get(s.sid, [])), s.actions) for s in acts),
        "act_total": sum(s.actions for s in acts),
        "split_hit": sum(
            1 for s in acts if s.actions == 2 and len(per_sentence.get(s.sid, [])) >= 2
        ),
        "split_total": sum(1 for s in acts if s.actions == 2),
        "claims": len(claims),
        "good_claims": len(matched_action_claims),
        "non_action_claims": sum(len(per_sentence.get(s.sid, [])) for s in non),
        "non_action_total": len(non),
        "unmatched": unmatched,
        "quote_ok": sum(1 for c in claims if verify_quote(c.quote, doc.text)),
        "field_ok": sum(1 for s, c in matched_action_claims if c.field == s.field),
        "role_ok": sum(1 for s, c in matched_action_claims if c.role == s.role),
        "hit_ids": sorted(s.sid for s in acts if per_sentence.get(s.sid)),
    }


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 3) if b else None


async def evaluate(
    split: str, n: int, models: list[str], runs: int, version: int | None = None
) -> dict:
    client = build_llm_client(Settings())  # type: ignore[call-arg]
    items = docs(split, n)
    sem = asyncio.Semaphore(4)

    async def one(doc: Doc, model: str, run: int) -> tuple[str, int, Doc, list[Claim], dict]:
        async with sem:
            claims, meta = await _extract(client, doc, model, version)
            return model, run, doc, claims, meta

    results = await asyncio.gather(
        *(one(d, m, k) for m in models for k in range(runs) for d in items)
    )
    ref = PROMPTS.get("extract_claims", version).ref
    out: dict = {"split": split, "docs": n, "runs": runs, "prompt": ref, "models": {}}
    for model in models:
        agg: dict[str, float] = defaultdict(float)
        misses: list[dict] = []
        hits_by_run: dict[int, dict[str, set[str]]] = defaultdict(dict)
        errors = 0
        lat, reason = [], []
        for m, run, doc, claims, meta in results:
            if m != model:
                continue
            errors += "error" in meta
            if "latency_ms" in meta:
                lat.append(meta["latency_ms"])
                reason.append(meta["tokens_reasoning"])
            s = score(doc, claims)
            hits_by_run[run][doc.id] = set(s.pop("hit_ids"))
            for k, v in s.items():
                agg[k] += v
            if run == 0:
                for gs in doc.sentences:
                    got = [c for c in claims if _best(c.quote, doc.sentences) is gs]
                    if gs.actions and len(got) < gs.actions:
                        misses.append(
                            {
                                "doc": doc.id,
                                "kind": "missed_action",
                                "expected": gs.actions,
                                "got": len(got),
                                "sentence": gs.text,
                            }
                        )
                    if not gs.actions and got:
                        misses.append(
                            {"doc": doc.id, "kind": "non_action_extracted", "sentence": gs.text}
                        )
        stab = []
        if runs > 1:
            for did in hits_by_run[0]:
                a, b = hits_by_run[0][did], hits_by_run[1].get(did, set())
                stab.append(len(a & b) / len(a | b) if a | b else 1.0)
        out["models"][model] = {
            "sentence_recall": _ratio(agg["sent_hit"], agg["sent_total"]),
            "action_recall": _ratio(agg["act_hit"], agg["act_total"]),
            "split_rate": _ratio(agg["split_hit"], agg["split_total"]),
            "precision": _ratio(agg["good_claims"], agg["claims"]),
            "non_action_rate": _ratio(agg["non_action_claims"], agg["non_action_total"]),
            "quote_ok": _ratio(agg["quote_ok"], agg["claims"]),
            "field_acc": _ratio(agg["field_ok"], agg["good_claims"]),
            "role_acc": _ratio(agg["role_ok"], agg["good_claims"]),
            "stability": round(sum(stab) / len(stab), 3) if stab else None,
            "claims_per_doc": round(agg["claims"] / (n * runs), 2),
            "errors": errors,
            "latency_ms_avg": int(sum(lat) / len(lat)) if lat else None,
            "reasoning_tokens_avg": int(sum(reason) / len(reason)) if reason else None,
            "misses": misses,
        }
    return out


KEYS = [
    "sentence_recall",
    "action_recall",
    "split_rate",
    "precision",
    "non_action_rate",
    "quote_ok",
    "field_acc",
    "role_acc",
    "stability",
    "claims_per_doc",
    "errors",
    "latency_ms_avg",
    "reasoning_tokens_avg",
]


def render(res: dict, show: bool) -> str:
    models = list(res["models"])
    lines = [
        f"# 주장 추출 평가 — {res['split']} ({date.today().isoformat()}, 문서 {res['docs']}건 × {res['runs']}회, prompt {res['prompt']})",
        "",
        "| 지표 | " + " | ".join(models) + " |",
        "|---|" + "---|" * len(models),
    ]
    for k in KEYS:
        lines.append(f"| {k} | " + " | ".join(str(res["models"][m][k]) for m in models) + " |")
    if show:
        for m in models:
            lines += ["", f"## 실패 사례 — {m} (1회차)", ""] + [
                f"- {x}" for x in res["models"][m]["misses"]
            ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument(
        "--models", nargs="+", default=["opencode:qwen3.8-flash", "openai:gpt-4.1-mini"]
    )
    ap.add_argument("--prompt-version", type=int, default=None, help="기본: CURRENT")
    ap.add_argument("--show-misses", action="store_true")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    if a.split == "test" and a.show_misses:
        raise SystemExit("test 세트의 실패 사례는 보지 않는다 (숨겨 둔 세트 유지)")
    res = asyncio.run(evaluate(a.split, a.n, a.models, a.runs, a.prompt_version))
    md = render(res, a.show_misses)
    print(md)
    if a.save:
        tag = res["prompt"].replace("@", "-v")
        stem = HERE / "reports" / f"{date.today().isoformat()}_{a.split}_{tag}"
        stem.with_suffix(".md").write_text(md, encoding="utf-8")
        if a.split == "test":
            for m in res["models"].values():
                m.pop("misses")
        stem.with_suffix(".json").write_text(
            json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
