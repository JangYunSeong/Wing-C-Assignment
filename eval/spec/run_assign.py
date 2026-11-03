"""슬롯 배정·면접 질문 평가 — 가상 주장 (지원자 데이터 아님).

슬롯: 2026-11-03-slots-<split> (run_slots 결과)를 다시 쓴다. 공고마다 가상 주장 3종:
  same    = 슬롯 역량과 같은 역량 (기대: 그 슬롯 배정)
  related = 슬롯 역량과 근접·기반 관계인 다른 역량 (기대: 관계로 배정)
  none    = 어떤 슬롯과도 관계없는 역량 (기대: 슬롯 없음 → 면접 질문만)
지표: 종류별 기대 배정 비율 · 면접 질문 검사 통과(첫 답·최종) · 비용
출력: data/eval/spec/<날짜>-assign-<split>/ (report.json, samples.md)
사용: uv run python -m eval.spec.run_assign --split dev
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from typing import Any

from eval.spec.run_draft import ROOT
from task_spec.config import Settings
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.pseudonymize import derive_pseudonymized, mint_pseudonymized
from task_spec.ontology.relatedness import load_relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry
from task_spec.schemas import TaskItem, VerifyTarget
from task_spec.spec.assign import assign_slots
from task_spec.spec.questions import make_questions

EVAL = ROOT / "data" / "eval" / "spec"
TPL = (
    "[ORG_1] 에서 {label} 업무를 맡아 기존 구조의 병목을 찾고 설계를 바꿔 처리 시간을 {n}% 줄였다."
)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], default="dev")
    a = ap.parse_args()
    st = Settings()
    repo = await OntologyRepository.open(st.database_url.get_secret_value(), "dev")
    rel = await load_relatedness(repo)
    labels = {r["id"]: r["label"] for r in await repo.fetch("SELECT id, label FROM competency")}
    await repo.close()
    src = json.loads((EVAL / f"2026-11-03-slots-{a.split}" / "report.json").read_text("utf-8"))
    llm = build_llm_client(st)
    prompts = PromptRegistry(ROOT / "prompts")

    rows: list[dict[str, Any]] = []
    jobs = []
    for row in src["rows"]:
        slots = [TaskItem.model_validate(s) for s in row["slots"]]
        if not slots:
            continue
        slot_comps = [s.target_competencies[0].competency_id for s in slots]
        related = next(
            (
                (c, n)
                for c in slot_comps
                for n in sorted(rel.pairs.get(c, set()))
                if n not in slot_comps
            ),
            None,
        )
        far = next(
            c
            for c in sorted(labels)
            if c not in slot_comps and not (rel.pairs.get(c, set()) & set(slot_comps))
        )
        cases = [("same", slot_comps[0], slots[0].task_id)]
        if related:
            exp = next(
                s.task_id for s in slots if s.target_competencies[0].competency_id == related[0]
            )
            cases.append(("related", related[1], exp))
        cases.append(("none", far, None))
        for i, (kind, comp, expect) in enumerate(cases, 1):
            vt = VerifyTarget(claim_id=f"c{i}", competency_ids=[comp], score=5.0)
            got = assign_slots([vt], slots, rel)[0]
            quote = TPL.format(label=labels.get(comp, comp), n=30 + 7 * i)
            text = derive_pseudonymized(
                mint_pseudonymized(quote), quote=quote, summary=f"{labels.get(comp, comp)} 개선"
            )
            slot = next((s for s in slots if s.task_id == got.slot_id), None)
            secret = (
                [o.value for o in slot.observables if o.kind == "first_mention_of"] if slot else []
            )
            rec = {
                "sub": row["sub"],
                "posting": row["posting"],
                "kind": kind,
                "competency": comp,
                "expected_slot": expect,
                "slot": got.slot_id,
                "basis": got.basis,
                "quote": quote,
                "slot_title": slot.title if slot else None,
            }
            rows.append(rec)
            jobs.append(
                make_questions(
                    f"c{i}",
                    text,
                    quote,
                    labels.get(comp, comp),
                    slot,
                    secret,
                    llm=llm,
                    prompts=prompts,
                    ontology_version="eval",
                )
            )
    results = await asyncio.gather(*jobs)
    for rec, res in zip(rows, results, strict=True):
        rec["ok_assign"] = rec["slot"] == rec["expected_slot"]
        rec["questions"] = res.output.model_dump() if res.output else None
        rec["flags"] = res.flags
        rec["calls"] = len(res.provenance)
        rec["cost_usd"] = sum(p.cost_usd or 0.0 for p in res.provenance)

    def rate(kind: str, key: str) -> float | None:
        xs = [r[key] for r in rows if r["kind"] == kind]
        return round(sum(xs) / len(xs), 3) if xs else None

    report = {
        "split": a.split,
        "n": len(rows),
        "assign_ok": {k: rate(k, "ok_assign") for k in ("same", "related", "none")},
        "questions_ok_final": round(
            sum(1 for r in rows if r["questions"] and not r["flags"]) / len(rows), 3
        ),
        "questions_first_try": round(
            sum(1 for r in rows if r["calls"] == 1 and not r["flags"]) / len(rows), 3
        ),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
    }
    out = EVAL / f"{date.today()}-assign-{a.split}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({**report, "rows": rows}, ensure_ascii=False, indent=1), "utf-8"
    )
    md = []
    for r in rows:
        q = r["questions"] or {}
        md.append(
            f"### {r['sub']} · {r['kind']} · {labels.get(r['competency'], r['competency'])}\n\n"
            f"가상 주장: {r['quote']}  \n배정: {r['slot']} ({r['slot_title']}) — {r['basis']} · 기대 {r['expected_slot']}\n\n"
            f"연결 문구: {q.get('link_text')}\n\n면접 질문\n"
            + "\n".join(f"- {x}" for x in q.get("follow_up_questions", []))
            + f"\n\nflags: {r['flags'] or '없음'}\n"
        )
    (out / "samples.md").write_text("\n---\n\n".join(md), "utf-8")
    print(json.dumps(report, ensure_ascii=False))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
