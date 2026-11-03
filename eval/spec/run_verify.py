"""경험 검증 확장 문항(verify_task) 평가 — 가상 주장 (지원자 데이터 아님).

표본: run_draft 와 같은 시범 공고(직군별 1건). 공고마다 공통 과제를 draft_task(CURRENT)로 만든 뒤,
가상 주장 3건(공통 과제 역량 1 + 다른 공고 역량 2)을 이미 가명화된 형태로 만들어 build_verifications 와 같은 경로로 확장 문항을 만든다.
가상 주장에는 일부러 가명화 토큰([ORG_1])과 수치를 넣어 '원문 베끼기' 검사가 실제로 걸리는지 본다.

자동 지표: ok_first · ok_final · failed · copy_first(첫 답에서 베끼기 걸림) · cite_cov · n_follow_up · 공통 과제 역량 감점 동작
출력: data/eval/spec/<날짜>-verify-<split>/ (report.json, samples.md) — git 밖.

사용: uv run python -m eval.spec.run_verify --split dev [--prompts-root DIR]
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
from task_spec.llm import LLMClient
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.pseudonymize import derive_pseudonymized, mint_pseudonymized
from task_spec.ontology.queries import criteria_detail
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry
from task_spec.schemas import TargetCompetency, VerifyTarget
from task_spec.spec.build import CommonDraft
from task_spec.spec.common import LEVEL_RANK
from task_spec.spec.draft import apply, draft_task
from task_spec.spec.incidents import load_incidents
from task_spec.spec.verify import (
    MAX_CRITERIA,
    VerifyContext,
    draft_verify,
    item_difficulty,
    pick_targets,
    verify_tech_env,
)

CLAIM_TPL = (
    "[ORG_1] 에서 {label} 업무를 맡아 기존 방식의 병목을 분석하고 구조를 바꿔 "
    "처리 시간을 {n}% 줄였다."
)


async def run_posting(
    repo: OntologyRepository,
    llm: LLMClient,
    prompts: PromptRegistry,
    sub: str,
    pid: str,
    built: CommonDraft,
) -> list[dict[str, Any]]:
    assert built.ctx is not None and built.task is not None
    res = await draft_task(built.ctx, llm=llm, prompts=prompts)
    common = apply(built.task, res)
    common_ids = {t.competency_id for t in common.target_competencies}
    ev_units = {t.competency_id: t.ncs_unit_ids for t in common.target_competencies}

    # 가상 주장: 공통 과제 역량 1 + 그 밖 공고 역량 2 (근거 단위가 있는 것만)
    from task_spec.ontology.queries import evidence as query_evidence
    from task_spec.ontology.queries import posting_profile

    p = await posting_profile(repo, pid)
    assert p is not None and p.sub and p.industry_id
    ev = await query_evidence(
        repo,
        p.competency_ids,
        sub=p.sub,
        industry_id=p.industry_id,
        secondary_sub=p.secondary_sub,
        in_pilot=p.in_pilot,
    )
    units = {
        e.competency_id: [u.unit_cl_cd for u in e.ncs_units if u.n_criteria > 0] for e in ev
    } | ev_units
    others = [
        c
        for c in p.competencies
        if c.competency_id not in common_ids and units.get(c.competency_id)
    ]
    pool = [next(c for c in p.competencies if c.competency_id in common_ids), *others[:2]]
    vts = [
        VerifyTarget(claim_id=f"c{i}", competency_ids=[c.competency_id], score=5.0)
        for i, c in enumerate(pool, 1)
    ]
    picked = pick_targets(vts, common_ids)
    by_id = {c.competency_id: c for c in pool}
    posting_langs = [t.label for t in p.tools if t.kind == "language"]

    rows: list[dict[str, Any]] = []
    for i, vt in enumerate(picked, 1):
        comp = by_id[vt.competency_ids[0]]
        quote = CLAIM_TPL.format(label=comp.label, n=30 + 7 * i)
        src = mint_pseudonymized(quote)  # 가상 문서 — 이미 가명화된 형태
        text = derive_pseudonymized(src, quote=quote, summary=f"{comp.label} 구조 개선")
        crit, _ = await criteria_detail(repo, units[comp.competency_id], max_criteria=MAX_CRITERIA)
        lv = max(comp.levels, key=lambda x: LEVEL_RANK.get(x, 0)) if comp.levels else None
        target = TargetCompetency(
            competency_id=comp.competency_id,
            label=comp.label,
            level=lv,
            ncs_unit_ids=units[comp.competency_id],
            ncs_criterion_ids=[c.criterion_id for c in crit],
        )
        env = verify_tech_env(quote, posting_langs, built.ctx.language_labels)
        ctx = VerifyContext(
            claim_id=vt.claim_id,
            claim_text=text,
            quote=quote,
            targets=[target],
            criteria=crit,
            env=env,
            difficulty=item_difficulty(comp.levels),
            common_task=f"{common.title}\n{common.scenario}",
            sub_label=built.ctx.sub_label,
            industry_label=built.ctx.industry_label,
            ontology_version=built.ctx.ontology_version,
            language_labels=built.ctx.language_labels,
        )
        r = await draft_verify(ctx, llm=llm, prompts=prompts)
        out = r.output
        cited: set[str] = set()
        if out is not None and r.anchors_ok:
            a = out.behavioral_anchors
            cited = (
                set(a.level_1.criterion_ids)
                | set(a.level_3.criterion_ids)
                | set(a.level_5.criterion_ids)
            )
        rows.append(
            {
                "sub": sub,
                "posting": pid,
                "claim": vt.claim_id,
                "overlaps_common": comp.competency_id in common_ids,
                "ok_first": out is not None and not r.first_problems and not r.errors,
                "ok_final": out is not None and not r.flags,
                "failed": out is None,
                "copy_first": any("원문" in x for x in r.first_problems),
                "first_problems": r.first_problems,
                "flags": r.flags,
                "cite_cov": float(bool(cited & set(target.ncs_criterion_ids))),
                "n_follow_up": len(out.follow_up_questions) if out else 0,
                "cost_usd": sum(x.cost_usd or 0.0 for x in r.provenance),
                "latency_s": sum(x.latency_ms for x in r.provenance) / 1000,
                "common_title": common.title,
                "quote": quote,
                "draft": out.model_dump(mode="json") if out else None,
            }
        )
    return rows


def sample_md(r: dict[str, Any]) -> str:
    d = r["draft"]
    head = f"### {r['sub']} · {r['posting']} · {r['claim']}{' (공통 과제 역량과 겹침)' if r['overlaps_common'] else ''}\n\n"
    head += f"공통 과제: {r['common_title']}  \n가상 주장: {r['quote']}  \n"
    if d is None:
        return head + f"(문항 없음: {r['flags']})\n"
    a = d["behavioral_anchors"]
    return (
        head
        + f"자동 검증: {'통과' if r['ok_final'] else '; '.join(r['flags'])}  \n첫 답 문제: {r['first_problems'] or '없음'}\n\n"
        + f"**{d['title']}**\n\n{d['scenario']}\n\n제출물: "
        + " / ".join(d["deliverables"])
        + "\n\n"
        + f"행동 기준\n- 1: {a['level_1']['behavior']}\n- 3: {a['level_3']['behavior']}\n- 5: {a['level_5']['behavior']}\n\n"
        + "면접 확인 질문\n"
        + "\n".join(f"- {q}" for q in d["follow_up_questions"])
        + "\n"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--prompts-root", type=Path, default=ROOT / "prompts")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    s = Settings()
    repo = await OntologyRepository.open(s.database_url.get_secret_value(), "dev")
    picked = await pick_postings(repo, repo.version.version, load_incidents(s.incidents_dir))
    split = {"dev": 0, "test": 1}
    picked = [x for i, x in enumerate(picked) if a.split == "all" or i % 2 == split[a.split]]
    llm = build_llm_client(s)
    prompts = PromptRegistry(a.prompts_root)
    groups = await asyncio.gather(
        *(run_posting(repo, llm, prompts, sub, pid, b) for sub, pid, b in picked)
    )
    await repo.close()
    rows = [r for g in groups for r in g]

    def avg(k: str) -> float:
        return round(mean(float(r[k]) for r in rows), 3) if rows else 0.0

    report = {
        "split": a.split,
        "n_items": len(rows),
        "postings": [p for _, p, _ in picked],
        "ok_first": avg("ok_first"),
        "ok_final": avg("ok_final"),
        "failed": avg("failed"),
        "copy_first": avg("copy_first"),
        "cite_cov": avg("cite_cov"),
        "n_follow_up": avg("n_follow_up"),
        "overlap_picked": sum(1 for r in rows if r["overlaps_common"]),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "latency_s": avg("latency_s"),
    }
    out_dir = (
        ROOT
        / "data"
        / "eval"
        / "spec"
        / f"{date.today()}-verify-{a.split}{('-' + a.tag) if a.tag else ''}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps({**report, "rows": rows}, ensure_ascii=False, indent=1), "utf-8"
    )
    (out_dir / "samples.md").write_text("\n---\n\n".join(sample_md(r) for r in rows), "utf-8")
    print(json.dumps(report, ensure_ascii=False))
    print(f"→ {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
