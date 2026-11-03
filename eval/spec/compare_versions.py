"""같은 공고의 draft_task 두 버전 과제를 judge_draft_task(gpt-4.1)로 이름을 가리고 비교 채점한다 (평가 전용).

기본: v2 = 2026-11-03-dev-v1v2b 의 @2 초안, v3 = 2026-11-03-solo-v3dev-b 의 초안 (둘 다 dev 6 공고).
사용: uv run python -m eval.spec.compare_versions [--openai-key-env NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from statistics import mean
from typing import Any

from eval.spec.run_draft import ROOT, judge_posting, pick_postings
from eval.spec.run_solo import _client
from task_spec.config import Settings
from task_spec.ontology.repository import OntologyRepository
from task_spec.schemas import DraftTaskOutput, DraftTaskOutputV3
from task_spec.spec.incidents import load_incidents

EVAL = ROOT / "data" / "eval" / "spec"
DIMS = ("alignment", "trap_quality", "anchors", "feasibility", "ai_axes")


def load_v2() -> dict[str, DraftTaskOutput]:
    out = {}
    for line in (EVAL / "2026-11-03-dev-v1v2b" / "drafts.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        if r["model"].endswith("@2") and r.get("draft"):
            out[r["posting"]] = DraftTaskOutput.model_validate(r["draft"])
    return out


def load_v3() -> dict[str, DraftTaskOutputV3]:
    rep = json.loads((EVAL / "2026-11-03-solo-v3dev-b" / "report.json").read_text("utf-8"))
    return {it["posting"]: DraftTaskOutputV3.model_validate(it["draft"]) for it in rep["items"]}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--openai-key-env", default=None)
    a = ap.parse_args()
    st = Settings()
    repo = await OntologyRepository.open(st.database_url.get_secret_value(), "dev")
    picked = await pick_postings(repo, repo.version.version, load_incidents(st.incidents_dir))
    await repo.close()
    v2, v3 = load_v2(), load_v3()
    llm = _client(a.openai_key_env)
    rows: list[dict[str, Any]] = []
    for sub, pid, b in picked:
        if pid not in v2 or pid not in v3 or b.ctx is None:
            continue
        res = await judge_posting(llm, b.ctx, {"v2": v2[pid], "v3": v3[pid]}, seed=pid)
        rows.append({"sub": sub, "posting": pid, **{k: res.get(k, {}) for k in ("v2", "v3")}})
        print(
            sub,
            {
                k: (res.get(k, {}).get("rank"), [res.get(k, {}).get(d) for d in DIMS])
                for k in ("v2", "v3")
            },
        )
    summary = {}
    for v in ("v2", "v3"):
        ok = [r[v] for r in rows if "rank" in r[v]]
        summary[v] = {
            "rank": round(mean(x["rank"] for x in ok), 2) if ok else None,
            **{d: round(mean(x[d] for x in ok), 2) if ok else None for d in DIMS},
        }
    out = EVAL / f"{date.today()}-compare-v2v3-dev"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), "utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
