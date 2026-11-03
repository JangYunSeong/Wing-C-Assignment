"""확장 슬롯(draft_slot) 평가 — 이미 만든 v3 공통 과제 위에 슬롯을 생성한다 (지원자 데이터 없음).

공통 과제: 2026-11-03-solo-v3d-55 (dev) · 2026-11-03-solo-v3t-55 (test) 의 v3 초안을 다시 쓴다 (LLM 재생성 없음).
지표: 계획 슬롯 수 · 생성 성공 · 첫 답 검증 통과 · 재요청 후 통과 · 공통 과제 정답 노출(첫 답) · 슬롯 역량이 공통 과제와 겹치는 비율 · 비용·지연
출력: data/eval/spec/<날짜>-slots-<split>/ (report.json, samples.md) — git 밖.
사용: uv run python -m eval.spec.run_slots --split dev [--prompts-root DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

from eval.spec.run_draft import ROOT, pick_postings
from task_spec.config import Settings
from task_spec.llm.factory import build_llm_client
from task_spec.ontology.queries import evidence as query_evidence
from task_spec.ontology.queries import posting_profile
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry
from task_spec.schemas import DraftTaskOutputV3
from task_spec.spec.draft import DraftResult, apply
from task_spec.spec.incidents import load_incidents
from task_spec.spec.slots import build_slots

EVAL = ROOT / "data" / "eval" / "spec"
SOURCE = {"dev": "2026-11-03-solo-v3d-55", "test": "2026-11-03-solo-v3t-55"}


def sample_md(sub: str, pid: str, common_title: str, slot: dict[str, Any]) -> str:
    arts = "\n".join(
        f"  - {a['artifact_id']} [{a['role']}] {a['title']}" for a in slot["artifacts"]
    )
    a = slot.get("behavioral_anchors") or {}
    anchors = "\n".join(
        f"- {k[-1]}: {a[k]['behavior']}" for k in ("level_1", "level_3", "level_5") if k in a
    )
    return (
        f"### {sub} · {pid} · {slot['task_id']} ({', '.join(t['label'] for t in slot['target_competencies'])})\n\n"
        f"공통 과제: {common_title}\n\n**{slot['title']}** (최대 {slot['max_requests']}개 요청)\n\n{slot['scenario']}\n\n"
        f"자료\n{arts}\n\n함정: " + " / ".join(t["description"] for t in slot["traps"]) + "\n\n"
        f"행동 기준\n{anchors}\n\nreview_flags: {slot['review_flags'] or '없음'}\n"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], default="dev")
    ap.add_argument("--prompts-root", type=Path, default=ROOT / "prompts")
    a = ap.parse_args()

    st = Settings()
    repo = await OntologyRepository.open(st.database_url.get_secret_value(), "dev")
    picked = {
        pid: (sub, b)
        for sub, pid, b in await pick_postings(
            repo, repo.version.version, load_incidents(st.incidents_dir)
        )
    }
    commons = json.loads((EVAL / SOURCE[a.split] / "report.json").read_text("utf-8"))["items"]
    llm = build_llm_client(st)
    prompts = PromptRegistry(a.prompts_root)

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        sub, built = picked[item["posting"]]
        assert built.task is not None and built.ctx is not None
        out = DraftTaskOutputV3.model_validate(item["draft"])
        common = apply(built.task, DraftResult(output=out))
        p = await posting_profile(repo, item["posting"])
        assert p is not None and p.sub and p.industry_id
        ev = await query_evidence(
            repo,
            p.competency_ids,
            sub=p.sub,
            industry_id=p.industry_id,
            secondary_sub=p.secondary_sub,
            in_pilot=p.in_pilot,
        )
        res = await build_slots(p, ev, common, built.ctx, ontology=repo, llm=llm, prompts=prompts)
        common_ids = {t.competency_id for t in common.target_competencies}
        first = [e for e in res.errors if e.code == "draft_invalid"]
        return {
            "sub": sub,
            "posting": item["posting"],
            "common_title": common.title,
            "made": len(res.slots),
            "failed": len(res.flags),
            "first_invalid": len(first),
            "invalid_final": sum(
                1 for s in res.slots if any("자동 검증 실패" in f for f in s.review_flags)
            ),
            "overlap_common": sum(
                1 for s in res.slots if s.target_competencies[0].competency_id in common_ids
            ),
            "cost_usd": sum(x.cost_usd or 0.0 for x in res.provenance),
            "latency_s": max((x.latency_ms for x in res.provenance), default=0) / 1000,
            "slots": [s.model_dump(mode="json") for s in res.slots],
            "flags": res.flags,
        }

    sem = asyncio.Semaphore(
        3
    )  # 공고 3개씩 (슬롯은 공고 안에서 동시에) — 동시 호출이 많으면 응답이 느려진다

    async def limited(it: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            return await one(it)

    rows = await asyncio.gather(*(limited(it) for it in commons if it["posting"] in picked))
    await repo.close()
    made = sum(r["made"] for r in rows)
    report = {
        "split": a.split,
        "postings": len(rows),
        "slots_made": made,
        "slots_failed": sum(r["failed"] for r in rows),
        "first_invalid": sum(r["first_invalid"] for r in rows),
        "invalid_final": sum(r["invalid_final"] for r in rows),
        "overlap_common": sum(r["overlap_common"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "latency_s_max_per_posting": round(max((r["latency_s"] for r in rows), default=0), 1),
        "slots_per_posting": round(mean(r["made"] for r in rows), 2) if rows else 0,
    }
    out_dir = EVAL / f"{date.today()}-slots-{a.split}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps({**report, "rows": rows}, ensure_ascii=False, indent=1), "utf-8"
    )
    (out_dir / "samples.md").write_text(
        "\n---\n\n".join(
            sample_md(r["sub"], r["posting"], r["common_title"], s)
            for r in rows
            for s in r["slots"]
        ),
        "utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))
    print(f"→ {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
