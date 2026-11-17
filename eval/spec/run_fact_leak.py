"""문제 분석 판 흘림 판정 — 사실(f#)의 구체 답이 묻지 않고 보이는가 (평가 전용, judge_fact_leak, analysis-stage.md §5.1).

낱말 겹침 검사(spec/analysis_request.leak_flags)는 오탐(일반 낱말)과 놓침(낱말을 바꾼 흘림)이 번갈아 나와 표시로 내렸다 (2026-11-16 사용자).
여기서는 뜻으로 잰다: qwen 이 구체 답을 핵심(값·순서·조건·대상)으로 나누고, 핵심마다 그것을 알려 주는 원문을 **인용만** 한다.
흘림 여부는 프로그램이 정한다 — 인용이 실제로 그 글에 있어야 센다(지어낸 인용은 "인용 불일치"로 따로 센다).
- 사실 판정: 핵심을 모두 찾음 = leaked, 일부 = partial, 없음 = safe.
- 판정을 범위마다 따로 부른다 (2026-11-16 보정 — 한 번에 다 주면 자료에서 근거를 찾고 모호한 답을 보지 않았다):
  text = 보이는 글(제목·배경 글·요청 글) + 모호한 답(f#·c#) → 요청 글 단계 몫,
  artifact = 자료(a#, 보이는 글은 제목만) → 기능형 생성기 몫. all = 한 번에 (v1·v2 보정 기록 재현용).

입력: 문제 분석 칸이 붙은 명세 — 평가 기록(`{"task": {...}}`, analysis-request-eval outputs), TaskItem JSON,
     run_feature 의 drafts.jsonl. 공고로 만든 명세만.
--protect spec: 문제 분석 전 명세(생성기 평가) — 공식 요구·공개 수용 기준 문장을 지킬 답으로 놓고 자료 범위만 잰다.
출력: data/eval/analysis-leak/<날짜>-<tag>/ (report.json, samples.md) — git 밖.
사용: uv run python -m eval.spec.run_fact_leak --inputs data/eval/analysis-request-eval/2026-11-16/outputs/*.json \\
        --tag v1 [--version 2] [--scopes text artifact]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from eval.spec.run_draft import ROOT
from task_spec.config import Settings
from task_spec.llm import LLMClient
from task_spec.llm.factory import build_llm_client
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    Fact,
    JudgeLeakInput,
    JudgeLeakOutput,
    JudgeLeakOutputV2,
    LeakEvidence,
    TaskItem,
)

EVAL = ROOT / "data" / "eval" / "analysis-leak"
TASK = "judge_fact_leak"
NONE = "(없음)"
VISIBLE = ("title", "background", "request")
OUTPUTS: dict[str, type[JudgeLeakOutput] | type[JudgeLeakOutputV2]] = {
    "JudgeLeakOutput": JudgeLeakOutput,  # v1 — 핵심마다 한 곳
    "JudgeLeakOutputV2": JudgeLeakOutputV2,  # v2 — 보이는 곳 전부
}


def load_task(path: Path) -> TaskItem:
    d = json.loads(path.read_text("utf-8"))
    return TaskItem.model_validate(d.get("task", d))


def load_tasks(path: Path) -> list[tuple[str, TaskItem]]:
    """(이름, 명세) — drafts.jsonl 은 줄마다 공고 하나 (생성 실패로 명세가 없는 줄은 뺀다)."""
    if path.suffix != ".jsonl":
        return [(path.stem, load_task(path))]
    out = []
    for line in path.read_text("utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("task"):
            out.append(
                (str(rec.get("posting") or rec.get("id")), TaskItem.model_validate(rec["task"]))
            )
    return out


def spec_items(task: TaskItem) -> tuple[TaskItem, dict[str, str]]:
    """공식 요구·공개 수용 기준 문장을 지킬 답(사실 자리)으로 — 문제 분석 전 명세의 자료 흘림을 잰다. 돌려주는 표: f# → r#·p#."""
    items = [(r.req_id, r.text) for r in task.requirements]
    items += [(a.ac_id, a.text) for a in task.acceptance_public]
    facts = [
        Fact(fact_id=f"f{i}", covers=[iid], topic=iid, ask_when="-", answer=text)
        for i, (iid, text) in enumerate(items, 1)
    ]
    shown = task.model_copy(
        update={"facts": facts, "request_mentions": [], "background": None, "request": None}
    )
    return shown, {f.fact_id: f.covers[0] for f in facts}


def sources(task: TaskItem) -> dict[str, str]:
    """지원자가 묻지 않고 볼 수 있는 글 — 이름 → 본문. 인용 확인이 이것만 본다."""
    out = {
        "title": task.title or "",
        "background": task.background or "",
        "request": task.request or "",
    }
    out |= {f.fact_id: f.answer_hint or "" for f in task.facts}
    out |= {c.clarification_id: c.answer_hint or "" for c in task.clarifications}
    out |= {a.artifact_id: f"{a.title}\n{a.content}" for a in task.artifacts}
    return out


def _in_scope(name: str, scope: str) -> bool:
    if scope == "text":
        return _kind(name) != "artifact"
    if scope == "artifact":
        return name == "title" or _kind(name) == "artifact"
    return True


def judge_input(task: TaskItem, scope: str = "all") -> JudgeLeakInput:
    # 모호한 답 앞의 주제명은 지원자에게 안 보인다 — 넣으면 주제명을 근거로 셀 수 있다 (2026-11-16 보정)
    hints = [f"{f.fact_id} {f.answer_hint}" for f in task.facts if f.answer_hint]
    hints += [f"{c.clarification_id} {c.answer_hint}" for c in task.clarifications if c.answer_hint]
    visible = [f"[title] {task.title or NONE}"]
    if scope != "artifact":
        visible += [f"[background] {task.background or NONE}", f"[request] {task.request or NONE}"]
    arts = "\n\n".join(f"[{a.artifact_id}] {a.title}\n{a.content}" for a in task.artifacts)
    return JudgeLeakInput(
        visible="\n".join(visible),
        hints=("\n".join(hints) or NONE) if scope != "artifact" else NONE,
        artifacts=(arts or NONE) if scope != "text" else NONE,
        facts="\n".join(f"{f.fact_id} ({f.topic}) {f.answer}" for f in task.facts),
    )


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().strip("\"'`“”‘’").strip()


def _kind(name: str) -> str:
    if name in VISIBLE:
        return "visible"
    return "artifact" if name.startswith("a") else "hint"


def _located(quote: str, texts: dict[str, str]) -> list[str]:
    """인용이 있는 글. 통째로 없으면 줄·쉼표·말줄임으로 나눠 조각(6자 이상)이 한 글에 모두 있을 때 — 여러 줄을 건너뛴 인용 (2026-11-16 보정)."""
    q = _norm(quote)
    if not q:
        return []
    if where := [k for k, t in texts.items() if q in t]:
        return where
    parts = [_norm(x) for x in re.split(r"\n|, |…|\.\.\.", quote)]
    parts = [x for x in parts if len(x) >= 6]
    if len(parts) < 2:
        return []
    return [k for k, t in texts.items() if all(x in t for x in parts)]


def _evidence(point: Any) -> list[LeakEvidence]:
    if hasattr(point, "evidence"):
        return list(point.evidence)
    return [LeakEvidence(source=point.source or "?", quote=point.quote)] if point.quote else []


def score(
    task: TaskItem, out: JudgeLeakOutput | JudgeLeakOutputV2, scope: str = "all"
) -> dict[str, Any]:
    """인용을 원문에서 확인해 사실마다 leaked/partial/safe 를 정한다. 인용이 어느 글에도 없으면 세지 않는다.
    mentioned = 사실이 덮는 r# 를 요청 글 단계가 "보이는 글이 직접 말한 요구"(request_mentions)로 적었음 — 그 요구는 보이는 것이 설계."""
    texts = {k: _norm(v) for k, v in sources(task).items() if _in_scope(k, scope)}
    by_id = {f.fact_id: f for f in out.facts}
    said = set(task.request_mentions)
    facts = []
    for fact in task.facts:
        mentioned = bool(said & set(fact.covers))
        judged = by_id.get(fact.fact_id)
        if judged is None:
            facts.append(
                {
                    "fact_id": fact.fact_id,
                    "verdict": "missing",
                    "mentioned": mentioned,
                    "points": [],
                }
            )
            continue
        points = []
        for p in judged.points:
            evidence, kinds = [], set()
            for e in _evidence(p):
                where = _located(e.quote, texts)
                if e.source in where:
                    where = [e.source]
                kinds |= {_kind(w) for w in where}
                evidence.append({"source": e.source, "quote": e.quote, "ok": bool(where)})
            ok = any(e["ok"] for e in evidence)
            status = "found" if ok else ("unverified" if evidence else "not_found")
            points.append(
                {"point": p.point, "status": status, "kinds": sorted(kinds), "evidence": evidence}
            )
        found = sum(p["status"] == "found" for p in points)
        verdict = "leaked" if found == len(points) else "partial" if found else "safe"
        facts.append(
            {"fact_id": fact.fact_id, "verdict": verdict, "mentioned": mentioned, "points": points}
        )
    return {"facts": facts}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    facts = [f for r in rows if "facts" in r for f in r["facts"]]
    points = [p for f in facts for p in f["points"]]
    found = [p for p in points if p["status"] == "found"]

    def seen_in(kind: str) -> int:  # 직접 말한 요구가 아닌 사실 중 그곳에서 핵심이 보인 것
        return sum(
            not f["mentioned"]
            and any(p["status"] == "found" and kind in p["kinds"] for p in f["points"])
            for f in facts
        )

    return {
        "tasks": len(rows),
        "judged": sum("facts" in r for r in rows),
        "facts": len(facts),
        **{
            v: sum(f["verdict"] == v for f in facts)
            for v in ("leaked", "partial", "safe", "missing")
        },
        "points": len(points),
        "points_found": len(found),
        "points_unverified": sum(p["status"] == "unverified" for p in points),
        **{
            f"found_in_{k}": sum(k in p["kinds"] for p in found)
            for k in ("visible", "hint", "artifact")
        },
        "facts_mentioned": sum(f["mentioned"] for f in facts),
        **{f"unmentioned_facts_seen_in_{k}": seen_in(k) for k in ("visible", "hint", "artifact")},
    }


async def judge(
    llm: LLMClient,
    prompts: PromptRegistry,
    task: TaskItem,
    version: int | None,
    scope: str = "all",
) -> dict[str, Any]:
    template = prompts.get(TASK, version)
    prompt = assemble(template, judge_input(task, scope), ontology_version="eval")
    try:
        r = await llm.call(prompt, OUTPUTS[template.meta.output_schema])
    except Exception as exc:  # 평가 스크립트 — 한 건 실패로 전체를 멈추지 않는다
        return {"error": type(exc).__name__}
    return {
        **score(task, r.parsed, scope),
        "cost_usd": r.provenance.cost_usd or 0.0,
        "provenance": r.provenance.model_dump(mode="json"),
    }


def samples_md(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    md = [
        f"# 흘림 판정 ({summary.get('scope', 'all')}) — {summary['tasks']}건",
        "",
        "```json",
        json.dumps(summary, ensure_ascii=False, indent=1),
        "```",
    ]
    for r in rows:
        md += ["", f"## {r['name']} — {r['title']}"]
        if "error" in r:
            md.append(f"판정 실패: {r['error']}")
            continue
        for f in r["facts"]:
            md.append(
                f"- {f['fact_id']} **{f['verdict']}**"
                + (" (직접 말한 요구)" if f["mentioned"] else "")
            )
            for p in f["points"]:
                md.append(f"  - [{p['status']}] {p['point']}")
                md += [
                    f'    - {"" if e["ok"] else "(인용 불일치) "}{e["source"]}: "{e["quote"]}"'
                    for e in p["evidence"]
                ]
    return "\n".join(md) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", type=Path, required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument(
        "--version",
        type=int,
        default=None,
        help="judge_fact_leak 판 (없으면 CURRENT — 보정 기록 prompts/judge_fact_leak/PROMOTIONS.md)",
    )
    ap.add_argument(
        "--scopes",
        nargs="+",
        choices=("text", "artifact", "all"),
        default=["text", "artifact"],
        help="판정 범위 — 범위마다 한 번씩 부른다",
    )
    ap.add_argument(
        "--protect",
        choices=("facts", "spec"),
        default="facts",
        help="지킬 답 — facts: 사실(문제 분석 판) / spec: 공식 요구·공개 수용 기준 (생성기 평가, 자료 범위만)",
    )
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    tasks = [x for p in a.inputs for x in load_tasks(p)]
    items: dict[str, dict[str, str]] = {}
    if a.protect == "spec":
        a.scopes = ["artifact"]  # 보이는 글은 문제 분석 전이라 잴 것이 없다
        for i, (n, t) in enumerate(tasks):
            tasks[i] = (n, spec_items(t)[0])
            items[n] = spec_items(t)[1]
    tasks = [(n, t) for n, t in tasks if t.facts]
    llm = build_llm_client(Settings())
    prompts = PromptRegistry(ROOT / "prompts")
    sem = asyncio.Semaphore(4)

    async def one(name: str, task: TaskItem, scope: str) -> dict[str, Any]:
        async with sem:
            j = await judge(llm, prompts, task, a.version, scope)
        return {"name": name, "title": task.title, **j}

    out = a.out or EVAL / f"{date.today()}-{a.tag}"
    out.mkdir(parents=True, exist_ok=True)
    for scope in a.scopes:
        rows = await asyncio.gather(*(one(n, t, scope) for n, t in tasks))
        for r in rows:  # spec 모드 — 사실 자리 번호를 r#·p# 로 되돌려 적는다
            for f in r.get("facts", []):
                f["item"] = items.get(r["name"], {}).get(f["fact_id"], f["fact_id"])
        summary = {
            "tag": a.tag,
            "prompt": prompts.get(TASK, a.version).ref,
            "scope": scope,
            **summarize(rows),
            "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in rows), 4),
        }
        name = "report" if scope == "all" else f"report-{scope}"
        (out / f"{name}.json").write_text(
            json.dumps({**summary, "rows": rows}, ensure_ascii=False, indent=1), "utf-8"
        )
        (out / f"samples-{scope}.md").write_text(samples_md(rows, summary), "utf-8")
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
