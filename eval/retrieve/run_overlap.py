"""대조(compare) 겹침 평가 — 이력서 × 공고 쌍을 실제 파이프라인으로 대조까지 돌려 역량별 정답과 비교한다.

왜: 짧은 가상 이력서 한 건에서 겹침이 2/12 로 낮았다 (2026-11-04). 첫 평가를 독립 검산한 결과를 반영한 두 번째 판:
  - 주장 추출은 이력서마다 **한 번** (공고마다 다시 뽑으면 추출 흔들림이 지표에 섞인다). 공고별로는 검색·선택·대조만.
  - 정답(judge_overlap, gpt-4.1·가명화본만)도 이력서마다 한 번 — 공고 여럿의 역량을 합쳐 판정 (공고마다 갈리지 않게).
    응답 개수·중복을 검사하고, 빠진 역량은 none 으로 채우지 않고 지표에서 뺀다.
  - 놓친 정답(action) 원인을 **근거 주장 기준**으로: 정답 근거 문장을 덮는 주장이 없으면 주장 누락,
    있는데 그 주장들의 검색 상위 10에 없으면 검색 누락, 있는데 고르지 않았으면 선택 누락.
    (첫 판은 모든 주장의 후보를 합쳐 선택 누락을 부풀렸다)
  - 같은 정답 기준으로 L0·L0+L1·L0+L1+L2 를 비교하는 표 (첫 판은 줄마다 기준이 달라 정밀도가 오르는 것처럼 보였다).
층: L0 지금 겹침 / L1 고른 공고 밖 역량과 온톨로지 근접·기반 관계인 공고 역량 / L2 공고 매처를 이력서 문장에 (로컬·결정적).
--legacy-quotes: 인용 대조를 예전 규칙(공백을 하나로만 줄임)으로 — 줄바꿈 수정 전후 비교용.
입력: --resumes 디렉터리의 *.txt (지원자 데이터 — git 밖 data/ 에만), --postings 공고 id 들.
     외부 LLM 에는 파이프라인의 로컬 가명화를 통과한 글만 간다 (불변식 2).
출력: data/eval/overlap/<날짜>-<tag>/ (report.json, samples.md) — git 밖.
사용: uv run python -m eval.retrieve.run_overlap --resumes data/eval/overlap/2026-11-04/resumes \\
        --postings comtus_2026#007 naver_2026#002 kakaobank_2026#002 --openai-key-env OPENAI_PREMIUM_API_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from eval.spec.run_solo import _client
from task_spec.config import Settings
from task_spec.graph import build_graph, make_checkpointer
from task_spec.llm.factory import build_llm_client
from task_spec.llm.quotes import SECTION_MARKER, compact_for_quote
from task_spec.nodes.compare import make_compare
from task_spec.nodes.ingest import FileIngestor
from task_spec.nodes.pseudonymize import RulePseudonymizer, load_pseudonymized
from task_spec.nodes.retrieve import make_retrieve_t1
from task_spec.ontology.queries import posting_profile
from task_spec.ontology.relatedness import load_relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.suggest import analyze, load_lists
from task_spec.prompting import PromptRegistry, assemble
from task_spec.retrieve import build_retriever
from task_spec.schemas import (
    DocumentRef,
    JudgeOverlapInput,
    JudgeOverlapOutput,
    PostingLineIn,
    RunInput,
    RunState,
)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "eval" / "overlap"
JUDGE = "judge_overlap"
EVIDENCE_SPAN = 8  # 정답 근거와 주장 인용이 공백 빼고 이만큼 겹치면 그 주장이 근거를 덮는다
STANDARDS = {
    "action": {"action"},
    "action+related": {"action", "related"},
    "any": {"action", "related", "mention"},
}
LAYERS = {"L0": ("L0",), "L0+L1": ("L0", "L1"), "L0+L1+L2": ("L0", "L1", "L2")}


def use_legacy_quotes() -> None:
    """수정 전 인용 규칙: 공백·줄바꿈을 공백 하나로만 줄임 (단어 중간 줄바꿈이면 실패)."""
    from task_spec.nodes import claims as claims_mod
    from task_spec.nodes import roles as roles_mod
    from task_spec.nodes.pseudonymize import node as pseudo_node

    ws = re.compile(r"\s+")

    def norm(t: str) -> str:
        return ws.sub(" ", SECTION_MARKER.sub(" ", t)).strip()

    def quote_in(q: str, s: str) -> bool:
        return bool(norm(q)) and norm(q) in norm(s)

    def locate_field(text: str, quote: str) -> Any:
        q = norm(quote)
        if not q:
            return None
        lines = text.split("\n")
        fields = roles_mod.field_of_lines(text)
        head = q[: min(len(q), 30)]
        for i, line in enumerate(lines):
            joined = norm(" ".join(lines[i : i + 4]))
            if joined.startswith(head) or head in norm(line):
                return fields[i]
        return None

    claims_mod.verify_quote = quote_in  # type: ignore[assignment]
    claims_mod.locate_field = locate_field  # type: ignore[assignment]
    pseudo_node.quote_in = quote_in  # type: ignore[attr-defined]


def ratio(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def covers(quote: str, evidence: str) -> bool:
    q, e = compact_for_quote(quote), compact_for_quote(evidence)
    if not q or not e:
        return False
    if len(e) <= EVIDENCE_SPAN:
        return e in q
    return any(e[i : i + EVIDENCE_SPAN] in q for i in range(len(e) - EVIDENCE_SPAN + 1))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resumes", type=Path, required=True)
    ap.add_argument("--postings", nargs="+", required=True)
    ap.add_argument("--tag", default="overlap")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--legacy-quotes", action="store_true")
    ap.add_argument("--openai-key-env", default=None, help="판정용 OpenAI 키를 읽을 .env 변수 이름")
    a = ap.parse_args()
    if a.legacy_quotes:
        use_legacy_quotes()

    st = Settings()
    repo = await OntologyRepository.open(st.database_url.get_secret_value(), st.ontology_version)
    lists = await load_lists(repo)
    retriever = await build_retriever(repo, lists)
    rel = await load_relatedness(repo)
    prompts = PromptRegistry(ROOT / "prompts")
    llm = build_llm_client(st)
    pseudo = RulePseudonymizer(st.data_dir)
    await pseudo.warm_up()
    judge_llm = _client(a.openai_key_env)
    profiles = {pid: await posting_profile(repo, pid) for pid in a.postings}
    missing = [pid for pid, p in profiles.items() if p is None]
    if missing:
        raise SystemExit(f"없는 공고: {missing}")
    retrieve = make_retrieve_t1(retriever, llm, prompts)
    compare = make_compare(rel)
    sem = asyncio.Semaphore(a.concurrency)

    async with make_checkpointer(kind="memory") as cp:
        graph = build_graph(
            cp,
            ingestor=FileIngestor(st.data_dir),
            pseudonymizer=pseudo,
            llm=llm,
            prompts=prompts,
            ontology=repo,
            ontology_lists=lists,
            retriever=retriever,
            relatedness=rel,
        )

        async def one_resume(resume: Path) -> list[dict[str, Any]]:
            async with sem:
                run_id = f"ovl{uuid.uuid4().hex[:12]}"
                raw = resume.read_bytes()
                dest = st.data_dir / "raw" / run_id / "resume-1.txt"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                doc = DocumentRef(
                    doc_id="resume-1",
                    kind="resume",
                    path=str(dest),
                    sha256=hashlib.sha256(raw).hexdigest(),
                )
                inp = RunInput(
                    run_id=run_id,
                    ontology_version=repo.version.version,
                    posting_id=a.postings[0],
                    documents=[doc],
                )
                cfg = {"configurable": {"thread_id": run_id}}
                # 주장 추출·역할 판정까지 한 번 — 공고마다 바뀌는 검색·선택·대조는 아래에서 공고별로
                await graph.ainvoke(
                    inp.model_dump(mode="json"), cfg, interrupt_before=["retrieve_t1"]
                )
                values = (await graph.aget_state(cfg)).values
                base_errors = [e.code for e in values.get("errors") or []]
                if not values.get("claims") or not values.get("pseudo_docs"):
                    return [
                        {"resume": resume.stem, "error": "주장 없음", "node_errors": base_errors}
                    ]
                ptext = await asyncio.to_thread(load_pseudonymized, values["pseudo_docs"][0])
                union = {
                    c.competency_id: c.label for p in profiles.values() for c in p.competencies
                }  # type: ignore[union-attr]
                prompt = assemble(
                    prompts.get(JUDGE),
                    JudgeOverlapInput(
                        resume=ptext,
                        competencies="\n".join(f"{k} — {v}" for k, v in sorted(union.items())),
                    ),
                    ontology_version=repo.version.version,
                )
                j = await judge_llm.call(prompt, JudgeOverlapOutput)
                answered = [x.competency_id for x in j.parsed.items]
                gt = {x.competency_id: x for x in j.parsed.items if x.competency_id in union}
                judge_check = {
                    "asked": len(union),
                    "answered": len(set(answered) & set(union)),
                    "duplicates": len(answered) - len(set(answered)),
                    "unknown_ids": sorted(set(answered) - set(union)),
                }
                rows = []
                for pid, prof in profiles.items():
                    s2 = RunState.model_validate(
                        {**values, "posting_id": pid, "posting_profile": prof, "candidates": []}
                    )
                    r = await retrieve(s2)
                    s3 = s2.model_copy(update={"candidates": r["candidates"]})
                    cmp = (await compare(s3))["comparison"]
                    row = {
                        "resume": resume.stem,
                        "posting": pid,
                        "run_id": run_id,
                        "extract_errors": base_errors,
                        "judge_check": judge_check,
                        "judge_cost_usd": (j.provenance.cost_usd or 0.0) / len(profiles),
                        # 주장 추출 비용은 공고 수로 나눠 싣고, 선택 비용은 그 공고 몫 그대로
                        "pipeline_cost_usd": sum(
                            p.cost_usd or 0.0 for p in values.get("provenance") or []
                        )
                        / len(profiles)
                        + sum(p.cost_usd or 0.0 for p in r.get("provenance", [])),
                    }
                    row.update(analyse(s3, cmp, gt, resume.read_text("utf-8"), lists, rel))
                    rows.append(row)
                return rows

        nested = await asyncio.gather(*(one_resume(r) for r in sorted(a.resumes.glob("*.txt"))))
    await repo.close()
    write_report(a.tag, [r for rs in nested for r in rs], legacy=a.legacy_quotes)


def analyse(
    state: RunState, cmp: Any, gt: dict[str, Any], raw_text: str, lists: Any, rel: Any
) -> dict[str, Any]:
    prof = state.posting_profile
    assert prof is not None
    posting = {c.competency_id: c for c in prof.competencies}
    claims = {c.claim_id: c for c in state.claims}
    per_claim = {
        x.claim_id: {
            "picked": list(x.competency_ids),
            "top10": [r.competency_id for r in x.candidates],
        }
        for x in state.candidates
    }
    picked = {k for v in per_claim.values() for k in v["picked"]}
    l0 = {m.competency_id for m in cmp.overlap}
    outside = picked - set(posting)
    l1 = {k for k in posting if k not in l0 and any(k in rel.pairs.get(o, set()) for o in outside)}
    # PDF 줄바꿈이 단어 중간을 끊으므로 줄을 이어 붙인 뒤 글머리표로 나눈다
    joined = raw_text.replace("\n", "")
    lines = [
        PostingLineIn(text=t.strip(" -"), section="duties") for t in joined.split("•") if t.strip()
    ]
    matched = {c for x in analyze(lists, lines, prof.sub or "", id_prefix="ovl") for c in x.comps}
    l2 = (matched & set(posting)) - l0 - l1
    table = []
    for k, c in posting.items():
        g = gt.get(k)
        ev = g.evidence if g else ""
        cover = [cid for cid, cl in claims.items() if ev and covers(cl.quote, ev)]
        cause = None
        if g is not None and g.label == "action" and k not in l0:
            if not cover:
                cause = "주장 누락"
            elif any(k in per_claim.get(cid, {}).get("picked", []) for cid in cover):
                cause = "기타"  # 근거 주장이 골랐는데 겹침이 아님 — 있으면 안 됨
            elif any(k in per_claim.get(cid, {}).get("top10", []) for cid in cover):
                cause = "선택 누락"
            else:
                cause = "검색 누락"
        table.append(
            {
                "competency_id": k,
                "label": c.label,
                "primary": c.primary,
                "gt": g.label if g else None,  # None = 판정이 빠짐 (지표에서 제외)
                "evidence": ev,
                "evidence_claims": cover,
                "L0": k in l0,
                "L1": k in l1,
                "L2": k in l2,
                "miss_cause": cause,
            }
        )
    return {
        "sub": prof.sub,
        "claims": [
            {"claim_id": c.claim_id, "action": c.action, "role": c.role, "field": c.field}
            for c in claims.values()
        ],
        "per_claim": per_claim,
        "applicant_only": [(m.competency_id, m.relatedness) for m in cmp.applicant_only],
        "table": table,
    }


def write_report(tag: str, rows: list[dict[str, Any]], *, legacy: bool) -> None:
    ok = [r for r in rows if "table" in r]
    t = [x for r in ok for x in r["table"] if x["gt"] is not None]

    def pr(layers: tuple[str, ...], pos: set[str]) -> dict[str, Any]:
        p = [x for x in t if any(x[k] for k in layers)]
        g = [x for x in t if x["gt"] in pos]
        hit = [x for x in p if x["gt"] in pos]
        return {
            "predicted": len(p),
            "gold": len(g),
            "precision": ratio(len(hit), len(p)),
            "recall": ratio(len(hit), len(g)),
        }

    causes: dict[str, int] = {}
    for x in t:
        if x["miss_cause"]:
            causes[x["miss_cause"]] = causes.get(x["miss_cause"], 0) + 1
    by_resume: dict[str, dict[str, Any]] = {}
    for r in ok:
        by_resume.setdefault(
            r["resume"],
            {
                "claims": len(r["claims"]),
                "quote_mismatch": r["extract_errors"].count("quote_mismatch"),
                "judge_check": r["judge_check"],
            },
        )
    report = {
        "legacy_quotes": legacy,
        "pairs": len(rows),
        "ok": len(ok),
        "posting_competencies_judged": len(t),
        "judge_missing": sum(1 for r in ok for x in r["table"] if x["gt"] is None),
        "gold": {
            lab: sum(x["gt"] == lab for x in t) for lab in ("action", "related", "mention", "none")
        },
        "same_standard": {
            s: {name: pr(layers, pos) for name, layers in LAYERS.items()}
            for s, pos in STANDARDS.items()
        },
        "missed_action_causes": causes,
        "by_resume": by_resume,
        "cost_usd": round(
            sum(r.get("judge_cost_usd", 0) + r.get("pipeline_cost_usd", 0) for r in ok), 4
        ),
        "errors": [
            {k: r.get(k) for k in ("resume", "error", "node_errors")} for r in rows if "error" in r
        ],
    }
    out = OUT / f"{date.today()}-{tag}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({**report, "rows": rows}, ensure_ascii=False, indent=1), "utf-8"
    )
    md = []
    mark = {True: "●", False: "·"}
    for r in ok:
        head = f"### {r['resume']} × {r['posting']} ({r['sub']})\n\n"
        head += "주장: " + " / ".join(
            f"{c['claim_id']}[{c['field'] or '-'}·{c['role'] or '-'}] {c['action']}"
            for c in r["claims"]
        )
        head += "\n\n고른 역량: " + "; ".join(
            f"{cid}→{','.join(v['picked']) or '-'}" for cid, v in r["per_claim"].items()
        )
        rows_md = "\n".join(
            f"| {x['label']} | {x['gt'] or '(판정 없음)'} | {mark[x['L0']]} | {mark[x['L1']]} | {mark[x['L2']]} | "
            f"{x['miss_cause'] or ''} | {','.join(c.split('-')[-1] for c in x['evidence_claims'])} | {x['evidence'][:50]} |"
            for x in r["table"]
        )
        md.append(
            head
            + "\n\n| 공고 역량 | 정답 | 지금 겹침 | 관련 경험 | 기술 언급 | 놓친 원인 | 근거 주장 | 근거 |\n|---|---|---|---|---|---|---|---|\n"
            + rows_md
            + "\n"
        )
    (out / "samples.md").write_text("\n---\n\n".join(md), "utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "by_resume"}, ensure_ascii=False))
    print(json.dumps(report["by_resume"], ensure_ascii=False))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
