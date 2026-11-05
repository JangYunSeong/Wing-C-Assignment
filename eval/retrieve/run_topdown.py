"""공고 역량 → 주장 확인(top-down) 평가 — 운영 노드(nodes/verify_overlap.py)를 그대로 부른다. [확정: 2026-11-04]

왜: 지금 대조 겹침은 주장 → 검색 상위 10 → 주장별 선택(0~2개)이라, 정답 역량이 어느 주장의 상위 10 에도 없으면
선택이 완벽해도 못 찾는다 (사용자 dev 이력서×역량 26건: 검색 상한 0.77, 완벽 선택 0.73, 실제 0.58).
여기서는 반대 방향으로 공고 역량마다 그 이력서의 주장 전체를 보고 판정한다 (prompts/verify_overlap).

- 주장은 저장된 run_overlap report 에서 고정 (실행마다 흔들리는 주장 추출을 빼고 판정만 비교).
- LLM 입력 = 가명화본에서 다시 만든 주장 텍스트(derive_pseudonymized)와 온톨로지 역량 설명만. 이력서 원문은 넣지 않는다
  (불변식 2·3). 목록이 공고 역량이라는 사실도 알리지 않는다.
- 대조군: 공고마다 같은 이력서의 다른 공고에만 있는 역량(정답 있음) N_CONTROLS 개를 고정 시드로 섞는다.
  정답 none 인 항목의 '예' 비율을 공고 안·대조군으로 나눠 본다.
- 층: L0(지금 겹침, report 에 저장된 값) / TD(top-down action) / TD+rel(action·related) / L0∪TD / L0∩TD.
  행(이력서×공고×역량)과 이력서×역량 두 단위로 채점. 기준은 overlap_gold 와 같다.
- 결과는 git 밖 data/ 에만 (지원자 데이터 — 판정 이유에 이력서 표현이 섞일 수 있다).

사용 (레포 루트에서 — .env 로딩):
  W=.claude/worktrees/overlap2
  PYTHONPATH=$W:$W/src uv run python $W/eval/retrieve/run_topdown.py \\
      --report data/eval/overlap/2026-11-04-overlap3/report.json \\
      --gold data/eval/overlap/2026-11-04-gold/gold.json --runs 2 --name user-dev
  --gold 를 빼면 report 표의 판정(gt, 판정자 1명)을 '잠정' 정답으로 쓴다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from eval.retrieve.run_overlap import covers
from task_spec.config import Settings
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.roles import decide_role
from task_spec.nodes.verify_overlap import TASK, make_verify_overlap
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.suggest import load_lists
from task_spec.prompting import PromptRegistry
from task_spec.retrieve.cards import CardSet, load_cards
from task_spec.schemas import (
    Claim,
    OverlapCheckResult,
    PostingCompetency,
    PostingProfile,
    PseudoDocRef,
    RunState,
)

ROOT = Path(__file__).resolve().parents[2]
N_CONTROLS = 3
STANDARDS = {"action": {"action"}, "action+related": {"action", "related"}}
LAYERS = ("L0", "TD", "TD+rel", "L0∪TD", "L0∩TD")


# ── 입력 만들기 (순수 함수) ─────────────────────────────────────────────


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def pick_controls(
    resume: str,
    posting: str,
    own: set[str],
    others: set[str],
    judged: set[str],
    k: int = N_CONTROLS,
) -> list[str]:
    """같은 이력서의 다른 공고에만 있고 정답이 있는 역량 k 개 (고정 시드)."""
    pool = sorted((others - own) & judged)
    return random.Random(_seed(resume, posting)).sample(pool, min(k, len(pool)))


def ordered(ids: Iterable[str], resume: str, posting: str) -> list[str]:
    """대조군이 끝에 몰리지 않게 섞는다 (고정 시드)."""
    out = sorted(ids)
    random.Random(_seed("order", resume, posting)).shuffle(out)
    return out


def state_for(
    row: dict[str, Any], asked: Sequence[str], labels: dict[str, str], ontology_version: str
) -> RunState:
    """report 행 → 운영 노드(verify_overlap)가 받는 State. 공고 역량 자리에 asked(공고 역량 + 대조군)를 넣는다."""
    ref = PseudoDocRef.model_validate(row["pseudo_doc"])
    return RunState(
        run_id=row.get("run_id") or "eval",
        ontology_version=ontology_version,
        posting_id=row["posting"],
        posting_profile=PostingProfile(
            posting_id=row["posting"],
            ontology_version=ontology_version,
            competencies=[
                PostingCompetency(competency_id=c, label=labels.get(c, c), primary=True)
                for c in asked
            ],
        ),
        pseudo_docs=[ref],
        claims=[
            Claim(
                claim_id=c["claim_id"],
                doc_id=ref.doc_id,
                action=c["action"],
                result=c.get("result"),
                quote=c["quote"],
                field=c.get("field"),
            )
            for c in row["claims"]
        ],
    )


def verdicts_from(
    result: OverlapCheckResult | None, asked: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """노드 결과 → {역량: {label, claims}}. 답이 없는 역량(partial)은 none + missing."""
    got = {c.competency_id: c for c in (result.items if result else [])}
    return {
        cid: (
            {"label": got[cid].label, "claims": list(got[cid].claim_ids)}
            if cid in got
            else {"label": "none", "claims": [], "missing": True}
        )
        for cid in asked
    }


# ── 채점 (순수 함수) ────────────────────────────────────────────────────


def layer_hit(x: dict[str, Any], layer: str) -> bool:
    td = x["td"] == "action"
    return {
        "L0": bool(x["L0"]),
        "TD": td,
        "TD+rel": x["td"] in ("action", "related"),
        "L0∪TD": bool(x["L0"]) or td,
        "L0∩TD": bool(x["L0"]) and td,
    }[layer]


def _pr(items: list[dict[str, Any]], layer: str, pos: set[str]) -> dict[str, Any]:
    p = [x for x in items if layer_hit(x, layer)]
    g = [x for x in items if x["gt"] in pos]
    hit = [x for x in p if x["gt"] in pos]
    return {
        "predicted": len(p),
        "gold": len(g),
        "precision": round(len(hit) / len(p), 3) if p else None,
        "recall": round(len(hit) / len(g), 3) if g else None,
    }


def scored_items(
    rows: Iterable[dict[str, Any]], gold: dict[str, Any], *, exclude_low: bool = False
) -> list[dict[str, Any]]:
    """공고 안 역량 행만 (대조군 제외). gt·원인 붙임."""
    out = []
    for r in rows:
        g = gold["resumes"].get(r["resume"], {})
        claims = {c["claim_id"]: c for c in r.get("claims") or []}
        for x in r["table"]:
            e = g.get(x["competency_id"])
            if e is None or (exclude_low and e.get("confidence") == "하"):
                continue
            cause = None
            if e["label"] == "action" and x["td"] != "action":
                cover = [
                    k
                    for k, c in claims.items()
                    if any(covers(c["quote"], ev) for ev in e["evidence"])
                ]
                cause = "주장 없음" if not cover else f"TD {x['td']}"
            out.append({**x, "resume": r["resume"], "gt": e["label"], "cause": cause})
    return out


def by_resume(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """이력서×역량 단위 — 공고 여러 개에 나온 역량은 하나라도 맞으면 예측 (L0·TD 같은 규칙)."""
    rank = {"action": 3, "related": 2, "mention": 1, "none": 0}
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for x in items:
        k = (x["resume"], x["competency_id"])
        a = agg.setdefault(k, {"L0": False, "td": "none", "gt": x["gt"]})
        a["L0"] = a["L0"] or bool(x["L0"])
        if rank[x["td"]] > rank[a["td"]]:
            a["td"] = x["td"]
    return list(agg.values())


def score(
    rows: list[dict[str, Any]], gold: dict[str, Any], *, exclude_low: bool = False
) -> dict[str, Any]:
    items = scored_items(rows, gold, exclude_low=exclude_low)
    res_items = by_resume(items)
    return {
        "rows": len(items),
        "resume_comps": len(res_items),
        "row_level": {
            s: {ly: _pr(items, ly, pos) for ly in LAYERS} for s, pos in STANDARDS.items()
        },
        "resume_level": {
            s: {ly: _pr(res_items, ly, pos) for ly in LAYERS} for s, pos in STANDARDS.items()
        },
        "td_miss_causes": dict(Counter(x["cause"] for x in items if x["cause"])),
        "l0_only_hits": sum(
            1 for x in items if x["L0"] and x["td"] != "action" and x["gt"] == "action"
        ),
    }


def yes_bias(rows: list[dict[str, Any]], gold: dict[str, Any]) -> dict[str, Any]:
    """정답 none 인 항목이 action(·related) 로 나온 비율 — 공고 안 vs 대조군."""
    out: dict[str, Any] = {}
    for kind in ("posting", "control"):
        n = a = ar = 0
        for r in rows:
            g = gold["resumes"].get(r["resume"], {})
            ids = r["controls"] if kind == "control" else [x["competency_id"] for x in r["table"]]
            for cid in ids:
                e = g.get(cid)
                if e is None or e["label"] != "none":
                    continue
                lab = r["verdicts"][cid]["label"]
                n += 1
                a += lab == "action"
                ar += lab in ("action", "related")
        out[kind] = {
            "gold_none": n,
            "action_rate": round(a / n, 3) if n else None,
            "action_or_related_rate": round(ar / n, 3) if n else None,
        }
    return out


def stability(run_a: list[dict[str, Any]], run_b: list[dict[str, Any]]) -> dict[str, Any]:
    va = {
        (r["resume"], r["posting"], c): v["label"] for r in run_a for c, v in r["verdicts"].items()
    }
    vb = {
        (r["resume"], r["posting"], c): v["label"] for r in run_b for c, v in r["verdicts"].items()
    }
    keys = sorted(set(va) & set(vb))
    if not keys:
        return {"n": 0}
    same = sum(va[k] == vb[k] for k in keys)
    same_a = sum((va[k] == "action") == (vb[k] == "action") for k in keys)
    return {
        "n": len(keys),
        "label_agree": round(same / len(keys), 3),
        "action_agree": round(same_a / len(keys), 3),
    }


def cross_posting(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """같은 이력서·같은 역량이 공고마다 다르게 판정되는 비율 (대조군 포함, 한 run 안)."""
    seen: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        for c, v in r["verdicts"].items():
            seen.setdefault((r["resume"], c), []).append(v["label"])
    multi = [labs for labs in seen.values() if len(labs) > 1]
    return {
        "multi": len(multi),
        "consistent": round(sum(len(set(x)) == 1 for x in multi) / len(multi), 3)
        if multi
        else None,
    }


def roles(rows: list[dict[str, Any]]) -> dict[str, int]:
    """TD action 근거 주장의 칸 → 역할 규칙 (불변식 6) 이 붙는지만 본다. 근거 여러 개면 가장 높은 역할."""
    order = {"design": 3, "lead": 2, "participate": 1, None: 0}
    cnt: Counter[str] = Counter()
    for r in rows:
        claims = {c["claim_id"]: c for c in r.get("claims") or []}
        for x in r["table"]:
            if x["td"] != "action":
                continue
            best = None
            for cid in x["td_claims"]:
                c = claims.get(cid)
                if c is None:
                    continue
                role, _ = decide_role(
                    Claim(
                        claim_id=c["claim_id"],
                        doc_id="d",
                        action=c["action"],
                        quote=c["quote"],
                        field=c.get("field"),
                    )
                )
                if order[role] > order[best]:
                    best = role
            cnt[best or "칸 밖·판정 안 함"] += 1
    return dict(cnt)


def gold_from_table(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """report 표의 판정(판정자 1명)을 잠정 정답으로 — 합의·사람 검토 전."""
    g: dict[str, dict[str, Any]] = {}
    for r in rows:
        for x in r["table"]:
            if x.get("gt") is None:
                continue
            g.setdefault(r["resume"], {})[x["competency_id"]] = {
                "label": x["gt"],
                "evidence": [x["evidence"]] if x.get("evidence") else [],
            }
    return {"resumes": g, "provisional": True}


# ── 실행 ─────────────────────────────────────────────────────────────────


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--gold", type=Path, help="없으면 report 표의 판정을 잠정 정답으로")
    ap.add_argument("--splits", nargs="+", default=["dev"], help="split 필드가 없는 행은 dev")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--budget", type=float, default=0.15, help="이 실행의 OpenCode 비용 상한(USD)")
    ap.add_argument("--name", required=True, help="결과 하위 폴더 이름 (user-dev, synth-dev …)")
    ap.add_argument("--tag", default="topdown")
    a = ap.parse_args()

    all_rows = [r for r in json.loads(a.report.read_text("utf-8"))["rows"] if "table" in r]
    base_rows = [r for r in all_rows if r.get("split", "dev") in a.splits]
    if not base_rows:
        raise SystemExit("고른 split 에 행이 없음")
    gold = json.loads(a.gold.read_text("utf-8")) if a.gold else gold_from_table(base_rows)

    st = Settings()
    repo = await OntologyRepository.open(st.database_url.get_secret_value(), st.ontology_version)
    lists = await load_lists(repo)
    cards: CardSet = await load_cards(repo, lists.matcher)
    ontology_version = repo.version.version
    await repo.close()
    registry = PromptRegistry(ROOT / "prompts")
    template = registry.get(TASK)
    llm = build_llm_client(st)
    node = make_verify_overlap(llm, registry, cards)  # 운영 노드 그대로 (빠진 역량 재질문 포함)
    sem = asyncio.Semaphore(a.concurrency)
    spent = 0.0

    comps_by_resume: dict[str, set[str]] = {}
    labels_by_resume: dict[str, dict[str, str]] = {}
    for r in base_rows:
        comps_by_resume.setdefault(r["resume"], set()).update(
            x["competency_id"] for x in r["table"]
        )
        labels_by_resume.setdefault(r["resume"], {}).update(
            {x["competency_id"]: x["label"] for x in r["table"]}
        )

    async def one(row: dict[str, Any], run: int) -> dict[str, Any]:
        nonlocal spent
        resume, posting = row["resume"], row["posting"]
        own = {x["competency_id"] for x in row["table"]}
        controls = pick_controls(
            resume, posting, own, comps_by_resume[resume], set(gold["resumes"].get(resume, {}))
        )
        asked = ordered(own | set(controls), resume, posting)
        base = {"resume": resume, "posting": posting, "split": row.get("split", "dev"), "run": run}
        async with sem:
            if spent >= a.budget:
                return {**base, "error": "budget"}
            out = await node(state_for(row, asked, labels_by_resume[resume], ontology_version))
            prov = out.get("provenance", [])
            cost = sum(p.cost_usd or 0.0 for p in prov)
            spent += cost
        result: OverlapCheckResult | None = out.get("overlap_check")
        if result is None or result.status == "failed":
            return {**base, "error": "failed" if result else "skipped", "cost_usd": cost}
        verdicts = verdicts_from(result, asked)
        table = [
            {
                "competency_id": x["competency_id"],
                "label": x["label"],
                "L0": x["L0"],
                "td": verdicts[x["competency_id"]]["label"],
                "td_claims": verdicts[x["competency_id"]]["claims"],
            }
            for x in row["table"]
        ]
        return {
            **base,
            "claims": row["claims"],
            "controls": controls,
            "asked": asked,
            "verdicts": verdicts,
            "table": table,
            "status": result.status,
            "errors": [f"{e.code}:{e.message}" for e in out.get("errors", [])],
            "cost_usd": cost,
            "calls": len(prov),
            "prompt_ref": prov[0].prompt if prov else None,
        }

    runs: list[list[dict[str, Any]]] = []
    for i in range(a.runs):
        runs.append(list(await asyncio.gather(*(one(r, i) for r in base_rows))))

    results = []
    for i, rows in enumerate(runs):
        ok = [r for r in rows if "table" in r]
        results.append(
            {
                "run": i,
                "ok": len(ok),
                "failed": Counter(r["error"] for r in rows if "error" in r),
                "all": score(ok, gold),
                "no_low": score(ok, gold, exclude_low=True),
                "yes_bias": yes_bias(ok, gold),
                "cross_posting": cross_posting(ok),
                "roles": roles(ok),
                "errors": dict(Counter(e.split(":")[0] for r in ok for e in r["errors"])),
                "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in rows), 4),
            }
        )
    stab = (
        stability([r for r in runs[0] if "table" in r], [r for r in runs[1] if "table" in r])
        if len(runs) > 1
        else None
    )

    out_dir = Path(st.data_dir) / "eval" / "overlap" / f"{date.today()}-{a.tag}" / a.name
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "report": str(a.report),
        "gold": str(a.gold) if a.gold else "provisional(report table)",
        "splits": a.splits,
        "ontology_version": ontology_version,
        "prompt": template.ref,
        "prompt_sha": template.sha256[:12],
    }
    (out_dir / "report.json").write_text(
        json.dumps(
            {"meta": meta, "results": results, "stability": stab, "runs": runs},
            ensure_ascii=False,
            indent=1,
            default=list,
        ),
        "utf-8",
    )
    text = summary(a.name, meta, results, stab, provisional=a.gold is None)
    (out_dir / "summary.md").write_text(text, "utf-8")
    print(text)
    print(f"→ {out_dir}")


def summary(
    name: str,
    meta: dict[str, Any],
    results: list[dict[str, Any]],
    stab: dict[str, Any] | None,
    *,
    provisional: bool,
) -> str:
    head = f"# top-down 겹침 실험 — {name} ({date.today()})" + (
        " — 정답 잠정(판정자 1명)" if provisional else ""
    )
    lines = [
        head,
        "",
        f"- 프롬프트 {meta['prompt']} ({meta['prompt_sha']}) · 온톨로지 {meta['ontology_version']}",
        "",
    ]
    for subset in ("all", "no_low"):
        for unit in ("row_level", "resume_level"):
            lines += [
                f"## {subset} · {unit}",
                "",
                "| run | 기준 | 층 | 예측 | 정답 | 정밀도 | 재현율 |",
                "|---|---|---|---|---|---|---|",
            ]
            for res in results:
                for s in STANDARDS:
                    for ly in LAYERS:
                        m = res[subset][unit][s][ly]
                        lines.append(
                            f"| {res['run']} | {s} | {ly} | {m['predicted']} | {m['gold']} "
                            f"| {m['precision']} | {m['recall']} |"
                        )
            lines.append("")
    lines += ["## 원인·쏠림·안정성·역할·비용", ""]
    for res in results:
        lines.append(
            f"- run {res['run']}: ok {res['ok']} 실패 {dict(res['failed'])} · TD 누락 원인 {res['all']['td_miss_causes']}"
            f" · L0 만 맞춘 것 {res['all']['l0_only_hits']} · '예' 쏠림 {res['yes_bias']}"
            f" · 공고 간 일관 {res['cross_posting']} · 역할 {res['roles']} · 오류 {res['errors']}"
            f" · ${res['cost_usd']}"
        )
    lines.append(f"- 실행 간 안정성: {stab}")
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
