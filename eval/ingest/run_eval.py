"""추출 평가: 본문 일치율 · 칸 라벨 정확도 · 머리말 기간 인식 · 2단 읽기 순서 (레이아웃별).

- dev 로 고치고 test 는 숨긴다 (eval-harness 원칙). test 에서는 사례별 출력(--show-misses)을 거부한다.
- 파일은 시드로 임시 폴더에 다시 만든다 (바이너리는 커밋하지 않는다).

사용: uv run python -m eval.ingest.run_eval --split dev --show-misses
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from eval.ingest.generate import LAYOUTS, render, samples
from task_spec.nodes.ingest.extractors import extract_raw
from task_spec.nodes.ingest.structure import structure

HERE = Path(__file__).parent
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub("", text.replace("—", "-"))


def evaluate(split: str, per_layout: int) -> dict:
    by_layout: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    misses: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for g in samples(split, per_layout):
            raw = extract_raw(render(g, Path(tmp)), Path(tmp) / "work")
            text, sections = structure(raw.lines)
            got_labels = [f.label for s in sections for f in s.fields]
            got_periods = [h.period for s in sections for h in s.headers]
            sim = SequenceMatcher(
                None, _norm("\n".join(g.expected_text)), _norm(text.split("오른쪽 단")[0])
            ).ratio()
            label_ok = got_labels == g.labels
            period_hit = sum(
                1
                for p in g.periods
                if any(p.replace(" ", "") in (q or "").replace(" ", "") for q in got_periods)
            )
            m = by_layout[g.layout]
            m["text_similarity"].append(sim)
            m["labels_exact"].append(float(label_ok))
            remaining = list(got_labels)
            hit = 0
            for lab in g.labels:  # 칸 단위 재현율 (순서 무관 다중집합)
                if lab in remaining:
                    remaining.remove(lab)
                    hit += 1
            m["label_recall"].append(hit / len(g.labels))
            m["label_extra"].append(len(remaining))
            m["period_recall"].append(period_hit / len(g.periods))
            if g.right_block:
                body_end = max(
                    text.find(e.split("⟧")[-1].strip()[:8]) for e in g.expected_text[-2:]
                )
                right_start = text.find("오른쪽 단")
                m["two_col_order"].append(float(right_start == -1 or right_start > body_end))
            if not label_ok or sim < 0.95 or period_hit < len(g.periods):
                misses.append(
                    {
                        "id": g.id,
                        "layout": g.layout,
                        "method": raw.method,
                        "sim": round(sim, 3),
                        "expected": g.labels,
                        "got": got_labels,
                    }
                )
    summary = {
        layout: {k: round(sum(v) / len(v), 3) for k, v in by_layout[layout].items()}
        for layout in LAYOUTS
        if layout in by_layout
    }
    return {"split": split, "per_layout": per_layout, "summary": summary, "misses": misses}


def render_md(res: dict, show_misses: bool) -> str:
    keys = [
        "text_similarity",
        "label_recall",
        "labels_exact",
        "label_extra",
        "period_recall",
        "two_col_order",
    ]
    lines = [
        f"# 추출 평가 — {res['split']} ({date.today().isoformat()}, 레이아웃당 {res['per_layout']}건)",
        "",
        "| 레이아웃 | 본문 일치 | 칸 재현율 | 칸 순서 정확 | 잘못 만든 칸(평균) | 기간 인식 | 2단 순서 |",
        "|---|---|---|---|---|---|---|",
    ]
    for layout, m in res["summary"].items():
        lines.append(
            f"| {layout} | " + " | ".join(f"{m[k]:.3f}" if k in m else "-" for k in keys) + " |"
        )
    if show_misses:
        lines += ["", "## 실패 사례", ""] + [f"- {x}" for x in res["misses"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    ap.add_argument("--per-layout", type=int, default=8)
    ap.add_argument("--show-misses", action="store_true")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    if a.split == "test" and a.show_misses:
        raise SystemExit("test 세트의 실패 사례는 보지 않는다 (숨겨 둔 세트 유지)")
    res = evaluate(a.split, a.per_layout)
    md = render_md(res, a.show_misses)
    print(md)
    if a.save:
        stem = HERE / "reports" / f"{date.today().isoformat()}_{a.split}"
        stem.parent.mkdir(exist_ok=True)
        stem.with_suffix(".md").write_text(md, encoding="utf-8")
        out = {k: v for k, v in res.items() if a.split == "dev" or k != "misses"}
        stem.with_suffix(".json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
