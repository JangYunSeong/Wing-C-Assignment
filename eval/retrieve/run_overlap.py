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
입력: --resumes 디렉터리의 *.txt (지원자 데이터 — git 밖 data/ 에만), --postings 공고 id 들 (모든 이력서 공통).
     또는 --pairs pairs.json — 이력서마다 다른 공고 묶음 ({"pairs": [{resume, split, posting_id}, ...]},
     이력서 파일은 pairs.json 옆 <split>/<resume>.txt). 쌍의 다른 필드(작성 의도 등)는 읽지 않는다.
     외부 LLM 에는 파이프라인의 로컬 가명화를 통과한 글만 간다 (불변식 2).
판정 비용: 주장 추출을 모두 끝낸 뒤 판정 프롬프트 길이로 비용을 어림하고 --judge-budget 을 넘으면 판정 전에 멈춘다.
지표: --report-splits(기본 dev)에 든 split 의 행만 지표·samples.md 에 넣는다. 다른 split(held-out test)의 행은
     report.json 에 원자료로만 남긴다 — 결정 때 한 번만 채점한다. split 이 없는 행(--resumes)은 항상 지표에 든다.
출력: data/eval/overlap/<날짜>-<tag>/ 또는 --out (report.json, samples.md) — git 밖.
사용: uv run python -m eval.retrieve.run_overlap --resumes data/eval/overlap/2026-11-04/resumes \\
        --postings comtus_2026#007 naver_2026#002 kakaobank_2026#002 --openai-key-env OPENAI_PREMIUM_API_KEY
     uv run python -m eval.retrieve.run_overlap --pairs data/eval/overlap/synth-2026-11-04/pairs.json \\
        --out data/eval/overlap/synth-2026-11-04-run --openai-key-env OPENAI_PREMIUM_API_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
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
from task_spec.prompting import AssembledPrompt, PromptRegistry, assemble
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
OUT_SUB = ("eval", "overlap")  # 결과는 설정의 data_dir(git 밖) 아래
JUDGE = "judge_overlap"
EVIDENCE_SPAN = 8  # 정답 근거와 주장 인용이 공백 빼고 이만큼 겹치면 그 주장이 근거를 덮는다
STANDARDS = {
    "action": {"action"},
    "action+related": {"action", "related"},
    "any": {"action", "related", "mention"},
}
LAYERS = {"L0": ("L0",), "L0+L1": ("L0", "L1"), "L0+L1+L2": ("L0", "L1", "L2")}
# 판정 비용 어림 (gpt-4.1 USD/토큰). 보수적으로: 한국어 1.3자당 1토큰, 판정 항목 하나에 출력 60토큰
JUDGE_PRICE = {"in": 2.0e-6, "out": 8.0e-6}
CHARS_PER_TOKEN = 1.3
OUT_TOKENS_PER_ITEM = 60


@dataclass(frozen=True)
class Job:
    """이력서 하나 — 이 이력서와 대조할 공고들, 평가 split (pairs 입력일 때만)."""

    resume: Path
    postings: tuple[str, ...]
    split: str | None = None


def jobs_from_dir(resumes: Path, postings: list[str]) -> list[Job]:
    return [Job(r, tuple(postings)) for r in sorted(resumes.glob("*.txt"))]


def load_pairs(path: Path) -> list[Job]:
    """pairs.json → 이력서별 Job. resume·split·posting_id 만 읽는다 (작성 의도 필드는 판정·지표에 쓰지 않음)."""
    data = json.loads(path.read_text("utf-8"))
    by: dict[str, tuple[str | None, list[str]]] = {}
    for p in data["pairs"]:
        split = p.get("split")
        prev = by.setdefault(p["resume"], (split, []))
        if prev[0] != split:
            raise ValueError(f"{p['resume']}: split 이 쌍마다 다름 ({prev[0]} / {split})")
        if p["posting_id"] not in prev[1]:
            prev[1].append(p["posting_id"])
    return [
        Job(path.parent / (split or "") / f"{name}.txt", tuple(pids), split)
        for name, (split, pids) in sorted(by.items())
    ]


def judge_prompt(
    prompts: PromptRegistry, resume: Any, union: dict[str, str], version: str
) -> AssembledPrompt:
    """정답 판정 프롬프트 — 가명화본 이력서 + 공고 역량 합집합("id — 이름" 줄). Claude 판정 패킷도 이것을 쓴다."""
    return assemble(
        prompts.get(JUDGE),
        JudgeOverlapInput(
            resume=resume,
            competencies="\n".join(f"{k} — {v}" for k, v in sorted(union.items())),
        ),
        ontology_version=version,
    )


def estimate_judge_cost(prompt_chars: int, n_items: int) -> float:
    return (prompt_chars / CHARS_PER_TOKEN) * JUDGE_PRICE["in"] + (
        n_items * OUT_TOKENS_PER_ITEM
    ) * JUDGE_PRICE["out"]


def in_report(row: dict[str, Any], splits: set[str] | None) -> bool:
    """지표에 넣을 행인가 — split 이 없는 행은 늘, 있으면 splits 에 들 때만 (held-out 은 원자료로만)."""
    split = row.get("split")
    return split is None or splits is None or split in splits


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
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--resumes", type=Path)
    src.add_argument("--pairs", type=Path, help="이력서마다 다른 공고 묶음 (pairs.json)")
    ap.add_argument("--postings", nargs="+", help="--resumes 와 함께: 모든 이력서 공통 공고 id")
    ap.add_argument("--tag", default="overlap")
    ap.add_argument(
        "--out", type=Path, default=None, help="결과 폴더 (기본 data/eval/overlap/<날짜>-<tag>)"
    )
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--legacy-quotes", action="store_true")
    ap.add_argument("--openai-key-env", default=None, help="판정용 OpenAI 키를 읽을 .env 변수 이름")
    ap.add_argument(
        "--judge-budget",
        type=float,
        default=0.5,
        help="판정 비용 어림 상한(USD) — 넘으면 판정 전에 멈춘다",
    )
    ap.add_argument(
        "--report-splits", default="dev", help="지표에 넣을 split (쉼표). 'all' 이면 전부"
    )
    ap.add_argument(
        "--only", default=None, help="이 이력서만 (쉼표, 파일 이름 stem) — 실패한 이력서 재실행용"
    )
    a = ap.parse_args()
    if a.resumes and not a.postings:
        ap.error("--resumes 에는 --postings 가 필요합니다")
    if a.legacy_quotes:
        use_legacy_quotes()
    jobs = load_pairs(a.pairs) if a.pairs else jobs_from_dir(a.resumes, a.postings)
    if a.only:
        jobs = [j for j in jobs if j.resume.stem in set(a.only.split(","))]
    missing_files = [str(j.resume) for j in jobs if not j.resume.is_file()]
    if missing_files:
        raise SystemExit(f"없는 이력서 파일: {missing_files}")
    report_splits = None if a.report_splits == "all" else set(a.report_splits.split(","))

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
    all_pids = sorted({pid for j in jobs for pid in j.postings})
    profiles = {pid: await posting_profile(repo, pid) for pid in all_pids}
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

        async def extract(job: Job) -> dict[str, Any]:
            """주장 추출·역할 판정까지 이력서마다 한 번 — 공고마다 바뀌는 검색·선택·대조는 judge_and_compare 에서."""
            async with sem:
                run_id = f"ovl{uuid.uuid4().hex[:12]}"
                raw = job.resume.read_bytes()
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
                    posting_id=job.postings[0],
                    documents=[doc],
                )
                cfg = {"configurable": {"thread_id": run_id}}
                await graph.ainvoke(
                    inp.model_dump(mode="json"), cfg, interrupt_before=["retrieve_t1"]
                )
                values = (await graph.aget_state(cfg)).values
                out: dict[str, Any] = {"job": job, "run_id": run_id, "values": values}
                if values.get("claims") and values.get("pseudo_docs"):
                    ptext = await asyncio.to_thread(load_pseudonymized, values["pseudo_docs"][0])
                    union = {
                        c.competency_id: c.label
                        for pid in job.postings
                        for c in profiles[pid].competencies  # type: ignore[union-attr]
                    }
                    out["union"] = union
                    out["prompt"] = judge_prompt(prompts, ptext, union, repo.version.version)
                return out

        extracted = await asyncio.gather(*(extract(j) for j in jobs))

    estimate = sum(
        estimate_judge_cost(sum(len(m.content) for m in x["prompt"].messages), len(x["union"]))
        for x in extracted
        if "prompt" in x
    )
    print(f"판정 비용 어림 ${estimate:.3f} (상한 ${a.judge_budget}) — 이력서 {len(extracted)}개")
    if estimate > a.judge_budget:
        await repo.close()
        raise SystemExit("판정 비용 어림이 상한을 넘어 판정 전에 멈춤")

    async def judge_and_compare(x: dict[str, Any]) -> list[dict[str, Any]]:
        job: Job = x["job"]
        values = x["values"]
        base_errors = [e.code for e in values.get("errors") or []]
        if "prompt" not in x:
            return [
                {
                    "resume": job.resume.stem,
                    "split": job.split,
                    "error": "주장 없음",
                    "node_errors": base_errors,
                }
            ]
        async with sem:
            prompt, union = x["prompt"], x["union"]
            j = await judge_llm.call(prompt, JudgeOverlapOutput)
            answered = [v.competency_id for v in j.parsed.items]
            gt = {v.competency_id: v for v in j.parsed.items if v.competency_id in union}
            judge_check = {
                "asked": len(union),
                "answered": len(set(answered) & set(union)),
                "duplicates": len(answered) - len(set(answered)),
                "unknown_ids": sorted(set(answered) - set(union)),
            }
            rows = []
            for pid in job.postings:
                prof = profiles[pid]
                s2 = RunState.model_validate(
                    {**values, "posting_id": pid, "posting_profile": prof, "candidates": []}
                )
                r = await retrieve(s2)
                s3 = s2.model_copy(update={"candidates": r["candidates"]})
                cmp = (await compare(s3))["comparison"]
                row = {
                    "resume": job.resume.stem,
                    "split": job.split,
                    "posting": pid,
                    "run_id": x["run_id"],
                    "extract_errors": base_errors,
                    # 판정만 다시 돌릴 수 있게 가명화본 참조·판정 원본 (data/ 아래에만 저장된다)
                    "pseudo_doc": values["pseudo_docs"][0].model_dump(mode="json"),
                    "judge_model": prompt.model,
                    "judge_asked": union,
                    "judge_items": [v.model_dump() for v in j.parsed.items],
                    "judge_check": judge_check,
                    "judge_cost_usd": (j.provenance.cost_usd or 0.0) / len(job.postings),
                    # 주장 추출 비용은 공고 수로 나눠 싣고, 선택 비용은 그 공고 몫 그대로
                    "pipeline_cost_usd": sum(
                        p.cost_usd or 0.0 for p in values.get("provenance") or []
                    )
                    / len(job.postings)
                    + sum(p.cost_usd or 0.0 for p in r.get("provenance", [])),
                }
                row.update(
                    analyse(s3, cmp, gt, job.resume.read_text("utf-8"), lists, rel, retriever)
                )
                rows.append(row)
            return rows

    nested = await asyncio.gather(*(judge_and_compare(x) for x in extracted))
    await repo.close()
    write_report(
        a.tag,
        [r for rs in nested for r in rs],
        legacy=a.legacy_quotes,
        out_root=Path(st.data_dir).joinpath(*OUT_SUB),
        out_dir=a.out,
        report_splits=report_splits,
    )


TOP_STORE = 30  # 검색 실험용으로 주장마다 저장하는 후보 수 (retrieve 노드는 10개만 쓴다)


def search_top(
    retriever: Any, query: str, sub: str | None, posting: dict[str, bool], k: int = TOP_STORE
) -> list[dict[str, Any]]:
    """retrieve 노드와 같은 조건(scope=soft, 공고 가산점)으로 상위 k 를 채널 순위와 함께."""
    return [
        {"id": h.competency_id, "score": round(h.score, 5), "ranks": dict(h.ranks)}
        for h in retriever.search(query, sub, scope="soft", posting=posting, k=k)
    ]


def analyse(
    state: RunState,
    cmp: Any,
    gt: dict[str, Any],
    raw_text: str,
    lists: Any,
    rel: Any,
    retriever: Any = None,
) -> dict[str, Any]:
    prof = state.posting_profile
    assert prof is not None
    posting = {c.competency_id: c for c in prof.competencies}
    claims = {c.claim_id: c for c in state.claims}
    bonus = {k: c.primary for k, c in posting.items()}
    per_claim = {
        x.claim_id: {
            "picked": list(x.competency_ids),
            "top10": [r.competency_id for r in x.candidates],
            "top30": (
                search_top(
                    retriever,
                    f"{claims[x.claim_id].action}. {claims[x.claim_id].quote}",
                    prof.sub,
                    bonus,
                )
                if retriever is not None and x.claim_id in claims
                else []
            ),
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
        # 인용(quote)은 가명화본에서 온 것 — 결과는 git 밖 data/ 에만 쓴다
        "claims": [
            {
                "claim_id": c.claim_id,
                "action": c.action,
                "result": c.result,
                "quote": c.quote,
                "role": c.role,
                "field": c.field,
            }
            for c in claims.values()
        ],
        "per_claim": per_claim,
        "applicant_only": [(m.competency_id, m.relatedness) for m in cmp.applicant_only],
        "table": table,
    }


def write_report(
    tag: str,
    rows: list[dict[str, Any]],
    *,
    legacy: bool,
    out_root: Path,
    out_dir: Path | None = None,
    report_splits: set[str] | None = None,
) -> None:
    held_out = [r for r in rows if "table" in r and not in_report(r, report_splits)]
    ok = [r for r in rows if "table" in r and in_report(r, report_splits)]
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
        "report_splits": sorted(report_splits) if report_splits else "all",
        # held-out 행은 지표 없이 원자료(rows)로만 — 쌍 수만 적는다
        "held_out_pairs": len(held_out),
        "held_out_resumes": sorted({r["resume"] for r in held_out}),
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
        # 비용은 held-out 행까지 전부 (판정 키와 파이프라인 키가 달라 따로)
        "cost_usd": {
            "judge": round(sum(r.get("judge_cost_usd", 0) for r in ok + held_out), 4),
            "pipeline": round(sum(r.get("pipeline_cost_usd", 0) for r in ok + held_out), 4),
        },
        "errors": [
            {k: r.get(k) for k in ("resume", "error", "node_errors")} for r in rows if "error" in r
        ],
    }
    out = out_dir or out_root / f"{date.today()}-{tag}"
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
