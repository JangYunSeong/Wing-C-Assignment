"""처음부터 끝까지(e2e) 대조 평가 — 이력서 × 공고 쌍마다 POST /runs 와 같은 경로로 실제 그래프를 승인 interrupt 까지.

왜: run_overlap·run_topdown 은 주장을 이력서마다 한 번 뽑아 고정하고 검색·선택·확인만 다시 돌린다 (단계별 비교용).
운영에서는 쌍마다 업로드 → ingest → 가명화 → 주장 추출 → retrieve → evidence → verify_overlap → compare → draft_spec →
승인 대기가 한 번에 돈다. 이 도구는 그 경로를 그대로 돌려 운영 겹침(두 경로 합침)을 잰다.

- 쌍마다 run 하나 (가명화·주장 추출을 이력서끼리 공유하지 않는다 — 실행마다 흔들림까지 지표에 들어간다).
  업로드는 API 의 store_uploads 를 그대로 (doc_id·권한·형식 검사 같음). --docs 에 <이력서>.docx 가 있으면 그 파일을,
  없으면 --resumes 의 <이력서>.txt (또는 <split>/<이력서>.txt).
- 그래프 = API lifespan 과 같은 구성 (온톨로지·OpenCode LLM·검색 색인·직무 연계·사례). 다른 점 두 가지:
  체크포인터는 InMemorySaver, 공통 과제 저장소는 InMemoryCommonStore (평가 초안을 task_db 에 쓰지 않게 — 같은 공고는
  이 프로세스 안에서 재사용, 운영과 같은 규칙).
- 수집 (행 = 쌍 × 실행): 가명화본 참조, 주장(인용·칸·역할), 주장별 선택·상위 10, 확인 경로 상태·재질문·보정 수,
  대조(출처별 겹침·관련 경험·공고에만), 승인 요약, 노드별 provenance 비용·지연, 노드 완료 시각, errors(id·코드만).
- 표(table): 공고 역량마다 L0(선택 경로가 고름)·TD(확인 경로 action)·OP(운영 겹침 = compare 의 overlap)·출처·역할.
  run_overlap 의 report 와 같은 모양이라 judge_packets · run_judge_consensus · overlap_gold score 가 그대로 읽는다
  (행마다 pseudo_doc · judge_asked · table · claims · per_claim, 판정 뒤 judge_items).
- 판정(선택): judge_overlap(gpt-4.1, --openai-key-env) — 이력서마다 한 번, 그 이력서 공고 역량 합집합, 첫 실행의
  가명화본만 (불변식 2). 판정 프롬프트 길이로 비용을 어림해 --judge-budget 을 넘으면 판정 전에 멈춘다.
- 채점은 정답이 있을 때만: --gold(합의·사람 검토 최종 정답) 또는 판정 결과(판정자 1명 → '잠정'). score 하위 명령으로
  나중에 최종 정답으로 다시 채점한다.
- 결과는 git 밖 data/eval/overlap/<tag>/ (report.json · samples.md · score.json · score.md). 판정 패킷·요약에는
  가명화본에서 나온 것만 쓴다.

사용 (레포 루트에서 — .env 로딩. 작업 폴더 코드면 PYTHONPATH=$W:$W/src):
  uv run python -m eval.retrieve.run_e2e run --pairs data/eval/overlap/synth-v2-2026-11-04/pairs.json \\
      --resumes data/eval/overlap/synth-v2-2026-11-04/resumes --docs data/eval/overlap/synth-v2-2026-11-04/docx \\
      --runs 2 --tag synth-v2-e2e --openai-key-env OPENAI_PREMIUM_API_KEY --judge-budget 0.5
  uv run python -m eval.retrieve.run_e2e score --report data/eval/overlap/synth-v2-e2e/report.json \\
      --gold data/eval/overlap/<합의 폴더>/gold.json
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from starlette.datastructures import UploadFile

from eval.retrieve.run_overlap import covers, estimate_judge_cost, judge_prompt
from task_spec.api.uploads import store_uploads
from task_spec.schemas import JudgeOverlapOutput, RunInput, RunState

ROOT = Path(__file__).resolve().parents[2]
OUT_SUB = ("eval", "overlap")
# provenance 의 프롬프트 → 그 프롬프트를 부르는 노드
NODE_OF_TASK = {
    "extract_claims": "extract_claims",
    "select_competency": "retrieve_t1",
    "verify_overlap": "verify_overlap",
    "draft_task": "draft_spec",
    "draft_slot": "draft_spec",
    "slot_questions": "draft_spec",
}
STANDARDS = {"action": {"action"}, "action+related": {"action", "related"}}
LAYERS = ("L0", "TD", "TD+rel", "OP", "OP+rel")
SOURCES = ("both", "verify", "promoted", "select", None)
SOURCE_KO = {
    "both": "두 경로 일치",
    "verify": "공고 역량 확인",
    "promoted": "승인자가 올림",  # e2e 는 승인하지 않으므로 보통 없음
    "select": "선택만 일치(예전 규칙 — 자동 겹침)",  # 2026-11-06 이전 report 만 (새 규칙은 만들지 않음)
    None: "확인 안 됨",
}
REASK = "빠진 역량을 다시 물음"


@dataclass(frozen=True)
class Pair:
    resume: str
    split: str | None
    posting: str


def load_pairs(path: Path) -> list[Pair]:
    """pairs.json → 쌍 목록. resume·split·posting_id 만 읽는다 (작성 의도 필드는 쓰지 않음)."""
    data = json.loads(path.read_text("utf-8"))
    out = []
    for p in data["pairs"]:
        pair = Pair(p["resume"], p.get("split"), p["posting_id"])
        if pair not in out:
            out.append(pair)
    return out


def pick_source(pair: Pair, resumes: Path | None, docs: Path | None) -> Path:
    """--docs/<이력서>.docx 가 있으면 그것, 없으면 --resumes/<이력서>.txt 또는 --resumes/<split>/<이력서>.txt."""
    if docs is not None and (docs / f"{pair.resume}.docx").is_file():
        return docs / f"{pair.resume}.docx"
    if resumes is not None:
        for cand in (
            resumes / f"{pair.resume}.txt",
            resumes / (pair.split or "") / f"{pair.resume}.txt",
        ):
            if cand.is_file():
                return cand
    raise FileNotFoundError(pair.resume)


# ── 실행 1건 ─────────────────────────────────────────────────────────────


async def run_pair(
    graph: Any,
    *,
    data_dir: Path,
    max_bytes: int,
    ontology_version: str,
    pair: Pair,
    src: Path,
    run: int,
    union: dict[str, str],
    stop_before_draft: bool = False,
) -> dict[str, Any]:
    """업로드 바이트 → 그래프 → 승인 interrupt. 행 하나 (실패해도 행을 남긴다).

    stop_before_draft: draft_spec 앞에서 멈춘다 (대조까지만 — 공통 과제·면접 질문 LLM 비용·시간을 뺀 빠른 판).
    """
    run_id = f"e2e{uuid.uuid4().hex[:12]}"
    base: dict[str, Any] = {
        "resume": pair.resume,
        "split": pair.split,
        "posting": pair.posting,
        "run": run,
        "run_id": run_id,
        "input": src.suffix.lower().lstrip("."),
        "judge_asked": union,
    }
    upload = UploadFile(file=io.BytesIO(src.read_bytes()), filename=f"upload{src.suffix.lower()}")
    docs = await store_uploads(
        run_id=run_id, data_dir=data_dir, max_bytes=max_bytes, uploads=[("resume", upload)]
    )
    inp = RunInput(
        run_id=run_id, ontology_version=ontology_version, posting_id=pair.posting, documents=docs
    )
    cfg = {"configurable": {"thread_id": run_id}}
    marks: list[tuple[str, float]] = []
    payload: dict[str, Any] | None = None
    t0 = time.monotonic()
    try:
        async for chunk in graph.astream(
            inp.model_dump(mode="json"),
            cfg,
            stream_mode="updates",
            interrupt_before=["draft_spec"] if stop_before_draft else None,
        ):
            now = round(time.monotonic() - t0, 2)
            for node, upd in chunk.items():
                if node == "__interrupt__":
                    payload = upd[0].value if upd else None
                else:
                    marks.append((node, now))
    except Exception as exc:  # 실행 하나가 죽어도 나머지는 돈다 — 예외 이름만
        return {**base, "error": type(exc).__name__, "wall_s": round(time.monotonic() - t0, 2)}
    values = (await graph.aget_state(cfg)).values
    row = collect(RunState.model_validate(values), payload, marks)
    if stop_before_draft and row["status"] == "ended":
        row["status"] = "stopped_before_draft"
    return {**base, **row, "wall_s": round(time.monotonic() - t0, 2)}


def _node(prompt_ref: str) -> str:
    task = prompt_ref.split("@")[0]
    return NODE_OF_TASK.get(task, task)


def node_walls(marks: Sequence[tuple[str, float]]) -> dict[str, float]:
    """노드 완료 시각 → 직전 완료 이후 걸린 시간 (병렬 가지는 근사)."""
    out: dict[str, float] = {}
    prev = 0.0
    for node, t in marks:
        out[node] = round(out.get(node, 0.0) + (t - prev), 2)
        prev = t
    return out


def collect(
    state: RunState, payload: dict[str, Any] | None, marks: Sequence[tuple[str, float]]
) -> dict[str, Any]:
    """State·승인 payload → 행. 원문·매핑·판정 이유는 없다 (가명화본 주장·id·코드만)."""
    cost: Counter[str] = Counter()
    llm_ms: Counter[str] = Counter()
    calls: Counter[str] = Counter()
    for p in state.provenance:
        n = _node(p.prompt)
        cost[n] += p.cost_usd or 0.0
        llm_ms[n] += p.latency_ms
        calls[n] += 1
    errors = [{"node": e.node, "code": e.code, "ref_id": e.ref_id} for e in state.errors]
    verify_msgs = [e.message for e in state.errors if e.node == "verify_overlap"]
    row: dict[str, Any] = {
        "status": "awaiting_approval" if payload is not None else "ended",
        "pseudo_doc": state.pseudo_docs[0].model_dump(mode="json") if state.pseudo_docs else None,
        "sub": state.posting_profile.sub if state.posting_profile else None,
        "cost_by_node": {k: round(v, 5) for k, v in cost.items()},
        "llm_calls_by_node": dict(calls),
        "llm_ms_by_node": dict(llm_ms),
        "node_wall_s": node_walls(marks),
        "pipeline_cost_usd": round(sum(cost.values()), 5),
        "errors": errors,
        "approval_summary": payload.get("summary") if payload else None,
    }
    p = state.posting_profile
    if p is None or state.comparison is None:
        return {**row, "error": "대조 없음"}
    claims = {c.claim_id: c for c in state.claims}
    per_claim = {
        x.claim_id: {
            "picked": list(x.competency_ids),
            "top10": [r.competency_id for r in x.candidates],
        }
        for x in state.candidates
    }
    picked = {k for v in per_claim.values() for k in v["picked"]}
    chk = state.overlap_check
    checks = {c.competency_id: c for c in chk.items} if chk else {}
    cmp = state.comparison
    place = {
        **{m.competency_id: ("posting_only", m) for m in cmp.posting_only},
        **{m.competency_id: ("related", m) for m in cmp.related},
        **{m.competency_id: ("overlap", m) for m in cmp.overlap},
    }
    table = []
    for pc in p.competencies:
        where, m = place.get(pc.competency_id, ("posting_only", None))
        ck = checks.get(pc.competency_id)
        table.append(
            {
                "competency_id": pc.competency_id,
                "label": pc.label,
                "primary": pc.primary,
                "gt": None,  # 판정 뒤 채움 (None = 판정 없음 — 지표에서 제외)
                "evidence": "",
                "evidence_claims": [],
                "L0": pc.competency_id in picked,
                "TD": ck is not None and ck.label == "action",
                "td_label": ck.label if ck else None,
                "td_claims": list(ck.claim_ids) if ck else [],
                "OP": where == "overlap",
                "op_status": where,
                "source": m.overlap_source if m else None,
                "select_only": bool(m.select_only) if m else False,
                "role": m.role if m else None,
                "miss_cause": None,
                "op_miss_cause": None,
            }
        )
    by_source: dict[str, list[str]] = defaultdict(list)
    for m in cmp.overlap:
        by_source[m.overlap_source or "none"].append(m.competency_id)
    return {
        **row,
        # 인용(quote)은 가명화본에서 온 것 — 결과는 git 밖 data/ 에만
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
        "overlap_check": {
            "status": chk.status if chk else None,
            "labels": dict(Counter(c.label for c in chk.items)) if chk else {},
            "reasks": sum(1 for msg in verify_msgs if msg == REASK),
            "notes": sum(1 for e in state.errors if e.code == "verify_answer_invalid"),
            "failures": sum(
                1
                for e in state.errors
                if e.node == "verify_overlap" and e.code != "verify_answer_invalid"
            ),
        },
        "comparison": {
            "check_status": cmp.check_status,
            "overlap_by_source": dict(by_source),
            "related": [m.competency_id for m in cmp.related],
            "posting_only": [m.competency_id for m in cmp.posting_only],
            "claims_to_verify": [v.claim_id for v in cmp.claims_to_verify],
        },
        "table": table,
    }


# ── 판정 (선택) ──────────────────────────────────────────────────────────


def fill_judgement(row: dict[str, Any], items: Sequence[Any], asked: dict[str, str]) -> None:
    """판정 결과 → 표의 gt·evidence·근거 주장·놓친 원인 (L0 기준 miss_cause · 운영 기준 op_miss_cause)."""
    gt = {v.competency_id: v for v in items if v.competency_id in asked}
    claims = row.get("claims") or []
    per_claim = row.get("per_claim") or {}
    for x in row.get("table") or []:
        g = gt.get(x["competency_id"])
        x["gt"] = g.label if g else None
        x["evidence"] = g.evidence if g else ""
        cover = [
            c["claim_id"] for c in claims if x["evidence"] and covers(c["quote"], x["evidence"])
        ]
        x["evidence_claims"] = cover
        x["miss_cause"] = x["op_miss_cause"] = None
        if x["gt"] != "action":
            continue
        k = x["competency_id"]
        if not x["L0"]:
            if not cover:
                x["miss_cause"] = "주장 누락"
            elif any(k in per_claim.get(c, {}).get("picked", []) for c in cover):
                x["miss_cause"] = "기타"
            elif any(k in per_claim.get(c, {}).get("top10", []) for c in cover):
                x["miss_cause"] = "선택 누락"
            else:
                x["miss_cause"] = "검색 누락"
        if not x["OP"]:
            if not cover:
                x["op_miss_cause"] = "주장 누락"
            elif x["td_label"] is None:
                x["op_miss_cause"] = "확인 안 됨"
            else:
                x["op_miss_cause"] = f"확인 {x['td_label']}"


def judge_plan(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """이력서마다 판정에 쓸 행 하나 — 가장 이른 실행의 가명화본 (같은 이력서는 한 번만 판정)."""
    plan: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (r["resume"], r["run"])):
        if r.get("pseudo_doc") and "table" in r and r["resume"] not in plan:
            plan[r["resume"]] = r
    return plan


# ── 채점 (순수 함수) ─────────────────────────────────────────────────────


def provisional_gold(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """판정자 1명의 판정을 잠정 정답으로 (합의·사람 검토 전)."""
    g: dict[str, dict[str, Any]] = {}
    for r in rows:
        for x in r.get("table") or []:
            if x.get("gt") is not None:
                g.setdefault(r["resume"], {})[x["competency_id"]] = {
                    "label": x["gt"],
                    "evidence": [x["evidence"]] if x.get("evidence") else [],
                }
    return {"resumes": g, "provisional": True}


def _hit(x: dict[str, Any], layer: str) -> bool:
    return {
        "L0": bool(x["L0"]),
        "TD": x["td_label"] == "action",
        "TD+rel": x["td_label"] in ("action", "related"),
        "OP": bool(x["OP"]),
        "OP+rel": x["op_status"] in ("overlap", "related"),
    }[layer]


def _pr(items: list[dict[str, Any]], layer: str, pos: set[str]) -> dict[str, Any]:
    p = [x for x in items if _hit(x, layer)]
    g = [x for x in items if x["gt"] in pos]
    hit = [x for x in p if x["gt"] in pos]
    return {
        "predicted": len(p),
        "gold": len(g),
        "precision": round(len(hit) / len(p), 3) if p else None,
        "recall": round(len(hit) / len(g), 3) if g else None,
    }


def _resume_level(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """이력서×역량 — 공고 여러 개에 나온 역량은 하나라도 맞으면 예측 (층마다 같은 규칙)."""
    rank = {"action": 3, "related": 2, "mention": 1, "none": 0, None: -1}
    srank = {"overlap": 2, "related": 1, "posting_only": 0}
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for x in items:
        a = agg.setdefault(
            (x["resume"], x["competency_id"]),
            {
                "L0": False,
                "OP": False,
                "td_label": None,
                "op_status": "posting_only",
                "gt": x["gt"],
            },
        )
        a["L0"] = a["L0"] or bool(x["L0"])
        a["OP"] = a["OP"] or bool(x["OP"])
        if rank[x["td_label"]] > rank[a["td_label"]]:
            a["td_label"] = x["td_label"]
        if srank[x["op_status"]] > srank[a["op_status"]]:
            a["op_status"] = x["op_status"]
    return list(agg.values())


def scored_items(
    rows: Iterable[dict[str, Any]],
    gold: dict[str, Any],
    *,
    exclude_low: bool = False,
    splits: set[str] | None = None,
) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if "table" not in r or (splits is not None and r.get("split") not in splits):
            continue
        g = gold["resumes"].get(r["resume"], {})
        for x in r["table"]:
            e = g.get(x["competency_id"])
            if e is None or (exclude_low and e.get("confidence") == "하"):
                continue
            out.append(
                {
                    **x,
                    "gt": e["label"],
                    "resume": r["resume"],
                    "posting": r["posting"],
                    "run": r["run"],
                }
            )
    return out


def score(
    rows: Sequence[dict[str, Any]],
    gold: dict[str, Any],
    *,
    exclude_low: bool = False,
    splits: set[str] | None = None,
) -> dict[str, Any]:
    items = scored_items(rows, gold, exclude_low=exclude_low, splits=splits)
    runs = sorted({x["run"] for x in items})
    per_run = []
    for i in runs:
        it = [x for x in items if x["run"] == i]
        res = _resume_level(it)
        overlap = [x for x in it if x["OP"]]
        per_run.append(
            {
                "run": i,
                "rows": len(it),
                "row_level": {
                    s: {ly: _pr(it, ly, pos) for ly in LAYERS} for s, pos in STANDARDS.items()
                },
                "resume_level": {
                    s: {ly: _pr(res, ly, pos) for ly in LAYERS} for s, pos in STANDARDS.items()
                },
                # 운영 겹침의 출처별 정밀도 (두 경로 일치 / 공고 역량 확인 / 선택만 / 확인 안 됨)
                "by_source": {
                    str(src): {
                        "n": len(xs),
                        "action_precision": round(
                            sum(x["gt"] == "action" for x in xs) / len(xs), 3
                        ),
                        "action_related_precision": round(
                            sum(x["gt"] in ("action", "related") for x in xs) / len(xs), 3
                        ),
                    }
                    for src in SOURCES
                    if (xs := [x for x in overlap if x["source"] == src])
                },
                # 선택만 일치 (2026-11-06 부터 겹침 아님 — 관련 경험·공고에만에 확인 필요 표시)
                "select_only": {
                    "n": len(so := [x for x in it if x.get("select_only")]),
                    "action_precision": round(sum(x["gt"] == "action" for x in so) / len(so), 3)
                    if so
                    else None,
                    "action_related_precision": round(
                        sum(x["gt"] in ("action", "related") for x in so) / len(so), 3
                    )
                    if so
                    else None,
                },
                "role_rate": {
                    "overlap": round(sum(x["role"] is not None for x in overlap) / len(overlap), 3)
                    if overlap
                    else None,
                    "overlap_n": len(overlap),
                },
                "op_miss_causes": dict(
                    Counter(
                        x["op_miss_cause"]
                        for x in it
                        if x["gt"] == "action" and not x["OP"] and x.get("op_miss_cause")
                    )
                ),
            }
        )
    return {
        "provisional": bool(gold.get("provisional")),
        "exclude_low": exclude_low,
        "gold": dict(Counter(x["gt"] for x in items if x["run"] == (runs[0] if runs else None))),
        "per_run": per_run,
        "stability": stability(rows),
    }


def stability(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """같은 쌍·같은 역량이 실행마다 같은 결과인가 (정답 없이). 주장 수 흔들림도."""
    by: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    n_claims: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        if "table" not in r:
            continue
        n_claims[(r["resume"], r["posting"])].append(len(r.get("claims") or []))
        for x in r["table"]:
            by[(r["resume"], r["posting"], x["competency_id"])].append(x)
    multi = [xs for xs in by.values() if len(xs) > 1]
    if not multi:
        return {"pairs_with_runs": 0}

    def agree(key: str) -> float:
        return round(sum(len({x[key] for x in xs}) == 1 for xs in multi) / len(multi), 3)

    spread = [max(v) - min(v) for v in n_claims.values() if len(v) > 1]
    return {
        "items": len(multi),
        "op_agree": agree("OP"),
        "source_agree": agree("source"),
        "td_label_agree": agree("td_label"),
        "l0_agree": agree("L0"),
        "claims_spread_max": max(spread) if spread else 0,
        "claims_spread_mean": round(sum(spread) / len(spread), 2) if spread else 0,
    }


def score_md(s: dict[str, Any], title: str) -> str:
    lines = [f"# {title}" + (" — 정답 잠정(판정자 1명)" if s["provisional"] else ""), ""]
    lines.append(f"- 정답 분포(첫 실행): {s['gold']} · 확신 하 제외: {s['exclude_low']}")
    lines.append(f"- 실행 간 안정성: {s['stability']}")
    lines.append("")
    for unit in ("row_level", "resume_level"):
        lines += [
            f"## {unit}",
            "",
            "| run | 기준 | 층 | 예측 | 정답 | 정밀도 | 재현율 |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in s["per_run"]:
            for st in STANDARDS:
                for ly in LAYERS:
                    m = r[unit][st][ly]
                    lines.append(
                        f"| {r['run']} | {st} | {ly} | {m['predicted']} | {m['gold']} | {m['precision']} | {m['recall']} |"
                    )
        lines.append("")
    lines += ["## 출처별 정밀도·역할·놓친 원인", ""]
    for r in s["per_run"]:
        src = {SOURCE_KO.get(None if k == "None" else k, k): v for k, v in r["by_source"].items()}
        lines.append(
            f"- run {r['run']}: 출처 {src} · 선택만 일치(겹침 아님) {r.get('select_only')} · 역할 {r['role_rate']}"
            f" · 운영 놓친 원인 {r['op_miss_causes']}"
        )
    return "\n".join(lines)


def summary_md(rows: Sequence[dict[str, Any]]) -> str:
    """samples.md — 쌍마다 표 (가명화본 주장 id·온톨로지 라벨만)."""
    mark = {True: "●", False: "·"}
    out = []
    for r in rows:
        if "table" not in r:
            out.append(f"### {r['resume']} × {r['posting']} (run {r['run']}) — {r.get('error')}\n")
            continue
        head = (
            f"### {r['resume']} × {r['posting']} (run {r['run']}, {r['input']}, {r['sub']})\n\n"
            f"주장 {len(r['claims'])}개 · 확인 {r['overlap_check']} · 비용 ${r['pipeline_cost_usd']} · {r['wall_s']}초\n\n"
            "| 공고 역량 | 정답 | L0 | 확인 | 운영 | 출처 | 역할 | 근거 주장 |\n|---|---|---|---|---|---|---|---|\n"
        )
        body = "\n".join(
            f"| {x['label']} | {x['gt'] or '-'} | {mark[x['L0']]} | {x['td_label'] or '-'} | {x['op_status']} | "
            f"{SOURCE_KO.get(x['source'], '-') if x['OP'] else '-'} | {x['role'] or '-'} | "
            f"{','.join(c.split('-')[-1] for c in x['td_claims'])} |"
            for x in r["table"]
        )
        out.append(head + body + "\n")
    return "\n---\n\n".join(out)


def run_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if "table" in r]
    cost: Counter[str] = Counter()
    wall: Counter[str] = Counter()
    for r in ok:
        cost.update(r["cost_by_node"])
        wall.update(r["node_wall_s"])
    n = len(ok) or 1
    return {
        "rows": len(rows),
        "ok": len(ok),
        "failed": dict(Counter(r.get("error") for r in rows if "table" not in r)),
        "inputs": dict(Counter(r["input"] for r in rows)),
        "check_status": dict(Counter(r["overlap_check"]["status"] for r in ok)),
        "reasks": sum(r["overlap_check"]["reasks"] for r in ok),
        "verify_failures": sum(r["overlap_check"]["failures"] for r in ok),
        "overlap_by_source": dict(
            Counter(
                s
                for r in ok
                for s, ids in r["comparison"]["overlap_by_source"].items()
                for _ in ids
            )
        ),
        "related": sum(len(r["comparison"]["related"]) for r in ok),
        "posting_only": sum(len(r["comparison"]["posting_only"]) for r in ok),
        "cost_usd": {
            "pipeline": round(sum(r["pipeline_cost_usd"] for r in ok), 4),
            "judge": round(sum(r.get("judge_cost_usd", 0.0) for r in rows), 4),
            "pipeline_by_node": {k: round(v, 4) for k, v in cost.items()},
        },
        "mean_wall_s": round(sum(r["wall_s"] for r in ok) / n, 1),
        "mean_node_wall_s": {k: round(v / n, 1) for k, v in wall.items()},
    }


def write_out(out: Path, report: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({**report, "rows": rows}, ensure_ascii=False, indent=1, default=str), "utf-8"
    )
    (out / "samples.md").write_text(summary_md(rows), "utf-8")


# ── 명령 ─────────────────────────────────────────────────────────────────


async def cmd_run(a: argparse.Namespace) -> None:
    from eval.spec.run_solo import _client
    from task_spec.config import Settings
    from task_spec.graph import build_graph, make_checkpointer
    from task_spec.llm.factory import build_llm_client
    from task_spec.nodes.ingest import FileIngestor
    from task_spec.nodes.pseudonymize import RulePseudonymizer, load_pseudonymized
    from task_spec.ontology.queries import posting_profile
    from task_spec.ontology.relatedness import load_relatedness
    from task_spec.ontology.repository import OntologyRepository
    from task_spec.postings.store import PostingStore
    from task_spec.postings.suggest import load_lists
    from task_spec.prompting import PromptRegistry
    from task_spec.retrieve import build_retriever
    from task_spec.schemas import PseudoDocRef
    from task_spec.spec.incidents import load_incidents
    from task_spec.spec.store import InMemoryCommonStore

    pairs = load_pairs(a.pairs)
    if a.only:
        keep = set(a.only.split(","))
        pairs = [p for p in pairs if p.resume in keep]
    if a.limit:
        pairs = pairs[: a.limit]
    sources = {p: pick_source(p, a.resumes, a.docs) for p in pairs}

    st = Settings()
    repo = await OntologyRepository.open(st.database_url.get_secret_value(), st.ontology_version)
    version = repo.version.version
    profiles = {pid: await posting_profile(repo, pid) for pid in sorted({p.posting for p in pairs})}
    if missing := [pid for pid, pr in profiles.items() if pr is None]:
        await repo.close()
        raise SystemExit(f"없는 공고: {missing}")
    union: dict[str, dict[str, str]] = defaultdict(dict)
    for p in pairs:
        union[p.resume].update({c.competency_id: c.label for c in profiles[p.posting].competencies})  # type: ignore[union-attr]

    prompts = PromptRegistry(ROOT / "prompts")
    prompts.validate_all()
    llm = build_llm_client(st)
    lists = await load_lists(repo)
    store = await PostingStore.open(st.database_url.get_secret_value())
    pseudo = RulePseudonymizer(st.data_dir)
    await pseudo.warm_up()
    retriever = await build_retriever(repo, lists)
    incidents_dir = st.incidents_dir if st.incidents_dir.is_absolute() else ROOT / st.incidents_dir
    sem = asyncio.Semaphore(a.concurrency)
    rows: list[dict[str, Any]] = []
    try:
        async with make_checkpointer(kind="memory") as cp:
            graph = build_graph(
                cp,
                ingestor=FileIngestor(st.data_dir),
                pseudonymizer=pseudo,
                llm=llm,
                prompts=prompts,
                ontology=repo,
                postings=store,
                ontology_lists=lists,
                retriever=retriever,
                relatedness=await load_relatedness(repo),
                common_store=InMemoryCommonStore(),  # 평가 초안을 task_db 에 쓰지 않는다
                incidents=load_incidents(incidents_dir),
                use_cover_letters=st.use_cover_letters,
            )

            async def one(p: Pair, run: int) -> dict[str, Any]:
                async with sem:
                    r = await run_pair(
                        graph,
                        data_dir=st.data_dir,
                        max_bytes=st.upload_max_bytes,
                        ontology_version=version,
                        pair=p,
                        src=sources[p],
                        run=run,
                        union=union[p.resume],
                        stop_before_draft=a.stop_before_draft,
                    )
                    print(
                        f"{p.resume}×{p.posting} run{run}: {r.get('error') or r['status']} "
                        f"${r.get('pipeline_cost_usd', 0)} {r.get('wall_s')}s",
                        flush=True,
                    )
                    return r

            for run in range(a.runs):
                rows += await asyncio.gather(*(one(p, run) for p in pairs))
    finally:
        await store.close()

    report: dict[str, Any] = {
        "meta": {
            "tool": "run_e2e",
            "date": str(date.today()),
            "ontology_version": version,
            "pairs": len(pairs),
            "runs": a.runs,
            "prompts": {t: prompts.get(t).ref for t in NODE_OF_TASK},
            "common_store": "in-memory (같은 공고는 이 실행 안에서 재사용)",
            "shared_extraction": False,
            "stop_before_draft": a.stop_before_draft,
        }
    }
    out = a.out or Path(st.data_dir).joinpath(*OUT_SUB, a.tag)

    judged = False
    if a.judge:
        plan = judge_plan(rows)
        texts = {
            res: await asyncio.to_thread(
                load_pseudonymized, PseudoDocRef.model_validate(r["pseudo_doc"])
            )
            for res, r in plan.items()
        }
        jprompts = {res: judge_prompt(prompts, texts[res], union[res], version) for res in plan}
        estimate = sum(
            estimate_judge_cost(sum(len(m.content) for m in jp.messages), len(union[res]))
            for res, jp in jprompts.items()
        )
        report["judge_estimate_usd"] = round(estimate, 4)
        print(
            f"판정 비용 어림 ${estimate:.3f} (상한 ${a.judge_budget}) — 이력서 {len(plan)}개",
            flush=True,
        )
        if estimate > a.judge_budget:
            report["judge_skipped"] = "비용 어림이 상한을 넘어 판정하지 않음"
        else:
            judge_llm = _client(a.openai_key_env)
            jsem = asyncio.Semaphore(a.concurrency)

            async def judge(res: str) -> None:
                async with jsem:
                    j = await judge_llm.call(jprompts[res], JudgeOverlapOutput)
                mine = [r for r in rows if r["resume"] == res and "table" in r]
                answered = [v.competency_id for v in j.parsed.items]
                check = {
                    "asked": len(union[res]),
                    "answered": len(set(answered) & set(union[res])),
                    "duplicates": len(answered) - len(set(answered)),
                    "unknown_ids": sorted(set(answered) - set(union[res])),
                }
                for r in mine:
                    fill_judgement(r, j.parsed.items, union[res])
                    r["judge_model"] = jprompts[res].model
                    r["judge_items"] = [v.model_dump() for v in j.parsed.items]
                    r["judge_check"] = check
                    r["judge_run_id"] = plan[res]["run_id"]
                    r["judge_cost_usd"] = (j.provenance.cost_usd or 0.0) / len(mine)

            await asyncio.gather(*(judge(res) for res in plan))
            judged = True
    await repo.close()

    report["summary"] = run_summary(rows)
    write_out(out, report, rows)
    splits = None if a.score_splits == "all" else set(a.score_splits.split(","))
    gold = (
        json.loads(a.gold.read_text("utf-8"))
        if a.gold
        else (provisional_gold(rows) if judged else None)
    )
    if gold is not None:
        write_score(out, rows, gold, splits)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
    print(f"→ {out}")


def write_score(
    out: Path, rows: list[dict[str, Any]], gold: dict[str, Any], splits: set[str] | None
) -> None:
    res = {
        "all": score(rows, gold, splits=splits),
        "no_low": score(rows, gold, exclude_low=True, splits=splits),
    }
    (out / "score.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    md = (
        score_md(res["all"], f"e2e 채점 — {out.name}")
        + "\n\n"
        + score_md(res["no_low"], "확신 하 제외")
    )
    (out / "score.md").write_text(md, "utf-8")
    print(md)


def cmd_score(a: argparse.Namespace) -> None:
    rep = json.loads(a.report.read_text("utf-8"))
    rows = rep["rows"]
    gold = json.loads(a.gold.read_text("utf-8")) if a.gold else provisional_gold(rows)
    splits = None if a.score_splits == "all" else set(a.score_splits.split(","))
    write_score(a.out or a.report.parent, rows, gold, splits)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--pairs", type=Path, required=True)
    r.add_argument("--resumes", type=Path, help="<이력서>.txt 폴더 (또는 <split>/ 아래)")
    r.add_argument("--docs", type=Path, help="<이력서>.docx 가 있으면 txt 대신")
    r.add_argument("--runs", type=int, default=1)
    r.add_argument("--tag", required=True, help="결과 폴더 이름 data/eval/overlap/<tag>/")
    r.add_argument("--out", type=Path, default=None)
    r.add_argument("--concurrency", type=int, default=3)
    r.add_argument("--only", default=None, help="이 이력서만 (쉼표)")
    r.add_argument("--limit", type=int, default=0, help="쌍 앞에서 N개만 (스모크)")
    r.add_argument(
        "--stop-before-draft",
        action="store_true",
        help="draft_spec 앞에서 멈춤 (대조까지만, 승인 요약 없음 — 빠르고 싸다)",
    )
    r.add_argument("--judge", action=argparse.BooleanOptionalAction, default=True)
    r.add_argument("--openai-key-env", default=None, help="판정용 OpenAI 키의 .env 변수 이름")
    r.add_argument("--judge-budget", type=float, default=0.5)
    r.add_argument("--gold", type=Path, default=None, help="최종 정답 (있으면 바로 채점)")
    r.add_argument("--score-splits", default="all")
    s = sub.add_parser("score")
    s.add_argument("--report", type=Path, required=True)
    s.add_argument(
        "--gold", type=Path, default=None, help="없으면 report 안의 판정을 잠정 정답으로"
    )
    s.add_argument("--out", type=Path, default=None)
    s.add_argument("--score-splits", default="all")
    a = ap.parse_args()
    if a.cmd == "run":
        if a.resumes is None and a.docs is None:
            ap.error("--resumes 또는 --docs 가 필요합니다")
        asyncio.run(cmd_run(a))
    else:
        cmd_score(a)


if __name__ == "__main__":
    main()
