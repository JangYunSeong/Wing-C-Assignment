"""가명화 평가: 심은 값이 결과에 남았는지(재현율), 남겨야 할 값이 사라졌는지(과잉 치환).

- split=dev  : 탐지기를 고칠 때 보는 세트. 놓친 사례를 전부 출력한다.
- split=test : 숨겨 둔 세트. 최종 수치만 낸다 (--show-misses 없이). 이 세트를 보고 탐지기를 고치지 않는다.
- hard       : 설계상 못 잡는다고 예상한 값(사전·접미어 없는 회사 등)은 따로 집계한다.

사용: uv run python -m eval.pseudonymize.run_eval --split dev --show-misses
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors, RunMapping, pseudonymize_text

HERE = Path(__file__).parent


def load(split: str) -> list[dict]:
    rows = []
    for f in sorted((HERE / "samples").glob(f"*_{split}*.jsonl")):
        rows += [
            json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
    return rows


def evaluate(rows: list[dict], *, use_kiwi: bool = True) -> dict:
    det = Detectors(load_dictionaries(), use_kiwi=use_kiwi)
    hit: Counter[str] = Counter()
    total: Counter[str] = Counter()
    hard_hit = hard_total = 0
    keep_lost: list[tuple[str, str]] = []
    keep_total = 0
    misses: list[dict] = []
    stopped = 0
    by_source: dict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        r = pseudonymize_text(row["text"], RunMapping(), det)
        stopped += r.residual > 0
        src = row["source"].split(":")[0]
        for p in row["planted"]:
            cat, val = p["category"], p["value"]
            ok = val not in r.text
            if val in row.get("hard", []):
                hard_total += 1
                hard_hit += ok
                continue
            total[cat] += 1
            hit[cat] += ok
            by_source[src]["total"] += 1
            by_source[src]["hit"] += ok
            if not ok:
                misses.append(
                    {"id": row["id"], "layout": row["layout"], "category": cat, "value": val}
                )
        for k in (k for k in row.get("keep", []) if k in row["text"]):  # 입력에 실제로 있는 값만
            keep_total += 1
            if k not in r.text:
                keep_lost.append((row["id"], k))

    all_hit, all_total = sum(hit.values()), sum(total.values())
    return {
        "samples": len(rows),
        "recall": {c: [hit[c], total[c]] for c in sorted(total)},
        "recall_all": [all_hit, all_total],
        "recall_by_source": {s: [c["hit"], c["total"]] for s, c in sorted(by_source.items())},
        "hard_recall": [hard_hit, hard_total],
        "over_replacement": [len(keep_lost), keep_total],
        "stopped_by_residual": stopped,
        "misses": misses,
        "keep_lost": keep_lost,
    }


def _pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.1f}%)" if b else "-"


def render(split: str, res: dict, show_misses: bool) -> str:
    lines = [
        f"# 가명화 평가 — {split} ({date.today().isoformat()})",
        "",
        f"- 표본 {res['samples']}건 · 출처별 재현율 "
        + ", ".join(f"{s} {_pct(*v)}" for s, v in res["recall_by_source"].items()),
        f"- **전체 재현율 {_pct(*res['recall_all'])}** · 설계상 어려운 값(hard) {_pct(*res['hard_recall'])}",
        f"- 과잉 치환(남겨야 할 값 소실) {_pct(*res['over_replacement'])}",
        f"- 누락 검사로 멈춘 문서 {res['stopped_by_residual']}건",
        "",
        "| 범주 | 재현율 |",
        "|---|---|",
        *[f"| {c} | {_pct(*v)} |" for c, v in res["recall"].items()],
    ]
    if show_misses:
        lines += [
            "",
            "## 놓친 값 (가상 데이터)",
            "",
            "| id | 형식 | 범주 | 값 |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {m['id']} | {m['layout'][:20]} | {m['category']} | `{m['value']}` |"
            for m in res["misses"]
        ]
        lines += ["", "## 과잉 치환", ""] + [f"- {i}: `{k}`" for i, k in res["keep_lost"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    ap.add_argument("--show-misses", action="store_true")
    ap.add_argument("--no-kiwi", action="store_true")
    ap.add_argument("--save", action="store_true", help="reports/ 에 md·json 저장")
    a = ap.parse_args()
    if a.split == "test" and a.show_misses:
        raise SystemExit("test 세트의 놓친 사례는 보지 않는다 (숨겨 둔 세트 유지)")
    res = evaluate(load(a.split), use_kiwi=not a.no_kiwi)
    md = render(a.split, res, a.show_misses)
    print(md)
    if a.save:
        stem = HERE / "reports" / f"{date.today().isoformat()}_{a.split}"
        stem.with_suffix(".md").write_text(md, encoding="utf-8")
        summary = {
            k: v for k, v in res.items() if a.split == "dev" or k not in ("misses", "keep_lost")
        }
        stem.with_suffix(".json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
