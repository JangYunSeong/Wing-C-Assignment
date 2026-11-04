"""대조 겹침 정답 합의 — 같은 가명화본·같은 역량 목록을 다른 모델로 한 번 더 판정해 두 판정이 같은 것만 정답으로.

왜: 독립 검산에서 gpt-4.1 판정의 action 중 약 22% 가 판정 규칙 위반으로 보였다 (2026-11-04). 판정 잡음이 개선 효과를
가린다. 다른 계열 모델(기본 gpt-5.5)로 다시 판정하고, 둘이 같으면 '합의 정답', 다르면 사람 검토용 불일치 목록에 둔다.
입력: run_overlap 의 report.json (행마다 pseudo_doc·judge_asked·judge_items 가 저장된 판 — overlap3 이후).
     판정 입력은 가명화본만 (불변식 2). 프롬프트는 CURRENT(judge_overlap) 그대로, 모델·설정만 바꾼다 (ModelOverride).
두 번째 판정을 파일로: --second-dir <폴더> 면 API 를 부르지 않고 <폴더>/<이력서>.json (JudgeOverlapOutput) 을 읽는다
     (eval/retrieve/judge_packets 가 같은 입력으로 만든 패킷을 다른 판정자 — 예: 새로 띄운 Claude 에이전트 — 가 답한 것).
지표: --metric-splits(기본 dev)에 든 split 의 행만. held-out(test) 행은 정답(gold)·불일치 목록만 만들고 지표는 내지 않는다.
출력: <report 폴더>-<tag>/ (consensus.json, disagreements.md) — git 밖 data/.
사용: uv run python -m eval.retrieve.run_judge_consensus --report data/eval/overlap/<날짜>-overlap3/report.json \\
        --openai-key-env OPENAI_PREMIUM_API_KEY
     uv run python -m eval.retrieve.run_judge_consensus --report <run>/report.json \\
        --second-dir <run>/claude_judge/out --second-name claude-fresh-agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from eval.retrieve.judge_packets import read_judge_dir
from eval.retrieve.run_overlap import in_report
from eval.spec.run_draft import ModelOverride
from eval.spec.run_solo import _client
from task_spec.nodes.pseudonymize import load_pseudonymized
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import JudgeOverlapInput, JudgeOverlapOutput, PseudoDocRef

ROOT = Path(__file__).resolve().parents[2]
JUDGE = "judge_overlap"
STANDARDS = {
    "action": {"action"},
    "action+related": {"action", "related"},
    "any": {"action", "related", "mention"},
}
LAYERS = {"L0": ("L0",), "L0+L1": ("L0", "L1"), "L0+L1+L2": ("L0", "L1", "L2")}


def consensus(a: dict[str, str], b: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """두 판정(역량 → 라벨)이 같은 것만 정답, 다른 역량 id 목록."""
    agreed = {k: v for k, v in a.items() if b.get(k) == v}
    differ = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    return agreed, differ


def metrics(rows: list[dict[str, Any]], gold: dict[str, dict[str, str]]) -> dict[str, Any]:
    """합의 정답만으로 같은 기준 표 (판정이 갈린 역량은 분모·분자에서 뺀다)."""
    t = []
    for r in rows:
        g = gold.get(r["resume"], {})
        for x in r["table"]:
            if x["competency_id"] in g:
                t.append({**x, "gt": g[x["competency_id"]]})

    def pr(layers: tuple[str, ...], pos: set[str]) -> dict[str, Any]:
        p = [x for x in t if any(x[k] for k in layers)]
        gg = [x for x in t if x["gt"] in pos]
        hit = [x for x in p if x["gt"] in pos]
        return {
            "predicted": len(p),
            "gold": len(gg),
            "precision": round(len(hit) / len(p), 3) if p else None,
            "recall": round(len(hit) / len(gg), 3) if gg else None,
        }

    causes: dict[str, int] = {}
    for x in t:
        if x["gt"] == "action" and not x["L0"] and x.get("miss_cause"):
            causes[x["miss_cause"]] = causes.get(x["miss_cause"], 0) + 1
    return {
        "judged_rows": len(t),
        "gold": {
            lab: sum(x["gt"] == lab for x in t) for lab in ("action", "related", "mention", "none")
        },
        "same_standard": {
            s: {n: pr(ls, pos) for n, ls in LAYERS.items()} for s, pos in STANDARDS.items()
        },
        "missed_action_causes": causes,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--model", default="openai:gpt-5.5")
    ap.add_argument("--tag", default="consensus")
    ap.add_argument(
        "--budget", type=float, default=1.2, help="이 판정의 비용 상한(USD) — 넘으면 멈춘다"
    )
    ap.add_argument("--openai-key-env", default=None)
    ap.add_argument(
        "--second-dir", type=Path, default=None, help="두 번째 판정을 API 대신 이 폴더의 답 파일로"
    )
    ap.add_argument("--second-name", default="file-judge", help="--second-dir 판정자 이름 (기록용)")
    ap.add_argument(
        "--metric-splits", default="dev", help="지표에 넣을 split (쉼표). 'all' 이면 전부"
    )
    a = ap.parse_args()
    metric_splits = None if a.metric_splits == "all" else set(a.metric_splits.split(","))

    rep = json.loads(a.report.read_text("utf-8"))
    rows = [r for r in rep["rows"] if "table" in r and "pseudo_doc" in r]
    by_resume: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_resume.setdefault(r["resume"], r)

    spent = 0.0
    first: dict[str, dict[str, str]] = {}
    second: dict[str, dict[str, str]] = {}
    evidence: dict[str, dict[str, tuple[str, str]]] = {}
    usage: list[dict[str, Any]] = []
    second_checks: dict[str, dict[str, Any]] = {}
    if a.second_dir is not None:
        labels, ev2, second_checks = read_judge_dir(
            a.second_dir, {k: r["judge_asked"] for k, r in by_resume.items()}
        )
        for resume, lab in labels.items():
            r = by_resume[resume]
            asked_r = r["judge_asked"]
            first[resume] = {
                x["competency_id"]: x["label"]
                for x in r["judge_items"]
                if x["competency_id"] in asked_r
            }
            second[resume] = lab
            evidence[resume] = {
                k: (
                    next((x["evidence"] for x in r["judge_items"] if x["competency_id"] == k), ""),
                    ev2[resume].get(k, ""),
                )
                for k in asked_r
            }
        second_model = a.second_name
    else:
        second_model = a.model
    prompts = ModelOverride(PromptRegistry(ROOT / "prompts"), a.model)
    llm = _client(a.openai_key_env)
    # 두 번째 판정을 파일에서 읽었으면 API 판정은 돌지 않는다
    todo = sorted(by_resume.items()) if a.second_dir is None else []
    for resume, r in todo:
        if spent >= a.budget:
            print(f"예산 {a.budget} 도달 — {resume} 부터 멈춤")
            break
        ref = PseudoDocRef.model_validate(r["pseudo_doc"])
        text = await asyncio.to_thread(load_pseudonymized, ref)
        asked: dict[str, str] = r["judge_asked"]
        prompt = assemble(
            prompts.get(JUDGE),
            JudgeOverlapInput(
                resume=text,
                competencies="\n".join(f"{k} — {v}" for k, v in sorted(asked.items())),
            ),
            ontology_version="eval",
        )
        out = await llm.call(prompt, JudgeOverlapOutput)
        cost = out.provenance.cost_usd or 0.0
        spent += cost
        tok = out.provenance.tokens
        usage.append(
            {
                "resume": resume,
                "cost_usd": round(cost, 4),
                "tokens_in": tok.in_,
                "tokens_out": tok.out,
                "tokens_reasoning": tok.reasoning,
            }
        )
        first[resume] = {
            x["competency_id"]: x["label"] for x in r["judge_items"] if x["competency_id"] in asked
        }
        second[resume] = {
            x.competency_id: x.label for x in out.parsed.items if x.competency_id in asked
        }
        evidence[resume] = {
            k: (
                next((x["evidence"] for x in r["judge_items"] if x["competency_id"] == k), ""),
                next((x.evidence for x in out.parsed.items if x.competency_id == k), ""),
            )
            for k in asked
        }

    gold: dict[str, dict[str, str]] = {}
    disagreements = []
    for resume in first:
        agreed, differ = consensus(first[resume], second[resume])
        gold[resume] = agreed
        names = by_resume[resume]["judge_asked"]
        for k in differ:
            disagreements.append(
                {
                    "resume": resume,
                    "split": by_resume[resume].get("split"),
                    "competency_id": k,
                    "label": names.get(k, k),
                    "first": first[resume].get(k),
                    "second": second[resume].get(k),
                    "first_evidence": evidence[resume][k][0],
                    "second_evidence": evidence[resume][k][1],
                }
            )
    judged = sum(len(v) for v in first.values())
    agreed_n = sum(len(v) for v in gold.values())
    scored = [r for r in rows if in_report(r, metric_splits)]
    summary = {
        "report": str(a.report),
        "first_model": by_resume[next(iter(first))]["judge_model"] if first else None,
        "second_model": second_model,
        "resumes": sorted(first),
        "second_checks": second_checks,
        "agreement": f"{agreed_n}/{judged}",
        "disagreements": len(disagreements),
        "cost_usd": round(spent, 4),
        "usage": usage,
        # 지표는 metric_splits 행만 (held-out 은 gold·불일치만 만들고 채점하지 않는다)
        "metric_splits": sorted(metric_splits) if metric_splits else "all",
        "metric_resumes": sorted({r["resume"] for r in scored if r["resume"] in first}),
        "first_only": metrics(
            [r for r in scored if r["resume"] in first],
            {k: v for k, v in first.items()},
        ),
        "consensus": metrics([r for r in scored if r["resume"] in gold], gold),
    }
    out_dir = a.report.parent.with_name(f"{a.report.parent.name}-{a.tag}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "consensus.json").write_text(
        json.dumps(
            {**summary, "gold": gold, "disagreement_rows": disagreements},
            ensure_ascii=False,
            indent=1,
        ),
        "utf-8",
    )
    md = ["| 이력서 | 역량 | 1차 | 2차 | 1차 근거 | 2차 근거 |", "|---|---|---|---|---|---|"]
    md += [
        f"| {d['resume']} | {d['label']} | {d['first']} | {d['second']} | {d['first_evidence'][:60]} | {d['second_evidence'][:60]} |"
        for d in disagreements
    ]
    (out_dir / "disagreements.md").write_text("\n".join(md) + "\n", "utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("usage",)}, ensure_ascii=False))
    print(json.dumps(usage, ensure_ascii=False))
    print(f"→ {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
