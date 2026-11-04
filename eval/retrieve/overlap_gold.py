"""대조 겹침 최종 정답 — 두 모델 합의 + 사람 검토(불일치만)를 한 파일로 고정하고, 그 정답으로 채점한다.

왜: 판정 한 모델의 잡음이 개선 효과를 가린다 (2026-11-04 독립 검산). 합의한 것은 그대로, 갈린 것은 사람이 정한 값.
gold.json = {"resumes": {이력서: {역량 id: {label, source(consensus|human), confidence?, memo?, evidence[]}}}}
  evidence 는 판정 모델이 옮긴 이력서 구절(가명화본) — 놓친 원인을 근거 주장 기준으로 다시 가르는 데 쓴다.
채점: run_overlap report 의 행(claims·per_claim·table)을 받아 같은 기준 표(L0·L0+L1·L0+L1+L2)와 놓친 원인.
  사람 확신 '하' 를 빼고도 본다 (--exclude-low).
gold·검토표·결과는 git 밖 data/ 에만 (지원자 데이터).
사용:
  uv run python -m eval.retrieve.overlap_gold build --consensus <consensus.json> --reviewed <disagreements_reviewed.md> \\
      --report <overlap3 report.json> --out data/eval/overlap/<날짜>-gold/gold.json
  uv run python -m eval.retrieve.overlap_gold score --report <report.json> --gold <gold.json> [--exclude-low]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from eval.retrieve.run_overlap import covers

LABELS = ("action", "related", "mention", "none")
STANDARDS = {"action": {"action"}, "action+related": {"action", "related"}}
LAYERS = {"L0": ("L0",), "L0+L1": ("L0", "L1"), "L0+L1+L2": ("L0", "L1", "L2")}
REVIEW_COLS = ("resume", "label", "first", "second", "final", "confidence", "memo")


def parse_reviewed(md: str) -> list[dict[str, str]]:
    """사람 검토표(마크다운 표: 이력서·역량·1차·2차·최종·확신·메모) → 행 목록. 머리·구분 줄은 건너뛴다."""
    rows = []
    for line in md.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < len(REVIEW_COLS) or cells[4] not in LABELS:
            continue
        rows.append(dict(zip(REVIEW_COLS, cells[: len(REVIEW_COLS)], strict=True)))
    return rows


def build_gold(
    consensus: dict[str, Any], reviewed: list[dict[str, str]], report_rows: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    first_ev: dict[tuple[str, str], str] = {}
    for r in report_rows:
        for x in r.get("judge_items") or []:
            first_ev[(r["resume"], x["competency_id"])] = x.get("evidence") or ""
    gold: dict[str, dict[str, dict[str, Any]]] = {}
    for resume, labels in consensus["gold"].items():
        for cid, lab in labels.items():
            gold.setdefault(resume, {})[cid] = {
                "label": lab,
                "source": "consensus",
                "evidence": [e for e in [first_ev.get((resume, cid), "")] if e],
            }
    by_label = {(d["resume"], d["label"]): d for d in consensus["disagreement_rows"]}
    unmatched = []
    for r in reviewed:
        d = by_label.get((r["resume"], r["label"]))
        if d is None:
            unmatched.append(f"{r['resume']}:{r['label']}")
            continue
        ev = [e for e in (d.get("first_evidence"), d.get("second_evidence")) if e]
        gold.setdefault(r["resume"], {})[d["competency_id"]] = {
            "label": r["final"],
            "source": "human",
            "confidence": r["confidence"],
            "memo": r["memo"],
            "evidence": ev,
        }
    if unmatched:
        raise ValueError(f"검토표 행을 불일치 목록에서 찾지 못함: {unmatched}")
    counts = {
        lab: sum(e["label"] == lab for g in gold.values() for e in g.values()) for lab in LABELS
    }
    return {
        "sources": {
            "consensus": sum(len(v) for v in consensus["gold"].values()),
            "human": len(reviewed),
        },
        "counts": counts,
        "resumes": gold,
    }


def miss_cause(
    cid: str, evidence: list[str], claims: dict[str, dict[str, Any]], per_claim: dict[str, Any]
) -> str:
    cover = [k for k, c in claims.items() if any(covers(c["quote"], e) for e in evidence)]
    if not cover:
        return "주장 누락"
    if any(cid in per_claim.get(k, {}).get("picked", []) for k in cover):
        return "기타"
    if any(cid in per_claim.get(k, {}).get("top10", []) for k in cover):
        return "선택 누락"
    return "검색 누락"


def score(
    rows: Iterable[dict[str, Any]], gold: dict[str, Any], *, exclude_low: bool = False
) -> dict[str, Any]:
    t = []
    for r in rows:
        g = gold["resumes"].get(r["resume"], {})
        claims = {c["claim_id"]: c for c in r.get("claims") or []}
        for x in r["table"]:
            e = g.get(x["competency_id"])
            if e is None or (exclude_low and e.get("confidence") == "하"):
                continue
            cause = None
            if e["label"] == "action" and not x["L0"]:
                cause = miss_cause(
                    x["competency_id"], e["evidence"], claims, r.get("per_claim") or {}
                )
            t.append({**x, "gt": e["label"], "cause": cause})

    def pr(layers: tuple[str, ...], pos: set[str]) -> dict[str, Any]:
        p = [x for x in t if any(x.get(k) for k in layers)]
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
        if x["cause"]:
            causes[x["cause"]] = causes.get(x["cause"], 0) + 1
    return {
        "rows": len(t),
        "gold": {lab: sum(x["gt"] == lab for x in t) for lab in LABELS},
        "same_standard": {
            s: {n: pr(ls, pos) for n, ls in LAYERS.items()} for s, pos in STANDARDS.items()
        },
        "missed_action_causes": causes,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--consensus", type=Path, required=True)
    b.add_argument("--reviewed", type=Path, required=True)
    b.add_argument("--report", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("score")
    s.add_argument("--report", type=Path, required=True)
    s.add_argument("--gold", type=Path, required=True)
    s.add_argument("--exclude-low", action="store_true")
    a = ap.parse_args()
    if a.cmd == "build":
        gold = build_gold(
            json.loads(a.consensus.read_text("utf-8")),
            parse_reviewed(a.reviewed.read_text("utf-8")),
            json.loads(a.report.read_text("utf-8"))["rows"],
        )
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(gold, ensure_ascii=False, indent=1), "utf-8")
        print(json.dumps({k: gold[k] for k in ("sources", "counts")}, ensure_ascii=False))
        print(f"→ {a.out}")
    else:
        rows = [r for r in json.loads(a.report.read_text("utf-8"))["rows"] if "table" in r]
        out = score(rows, json.loads(a.gold.read_text("utf-8")), exclude_low=a.exclude_low)
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
