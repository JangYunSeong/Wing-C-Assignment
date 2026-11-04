"""함정 사실 판정 — 공통 과제의 함정 설명이 기술적으로 맞는가 (평가 전용, judge_trap_facts).

2026-11-03 웹 예시: "풀을 20으로 늘려도 32개가 필요해 교착 재발" 함정이 틀렸다(스레드 16·작업당 2개면 17 이상이면 풀림).
함정이 틀리면 정확한 근거로 그 처방을 낸 숙련자가 감점된다. 사실 검산은 사람이 매번 하기 어렵다.

입력: 이미 생성된 과제 파일 (run_solo report.json · run_draft drafts.jsonl · 예시 common_with_slots.json). 가상 데이터·공개 사례만.
판정: gpt-4.1 이 숨김 부분까지 보고 함정마다 sound / unsound / unclear + 검산, 감점되는 정답, 수치 불일치.
     원 사례 기준(원인·흔한 처방이 틀리는 이유·인정할 조치)을 함께 준다.
결정적 검사도 같이 센다: 원 사례 문구·수치 복사(incident_copied), 자료의 판정·요약 줄.
출력: data/eval/spec/<날짜>-trapfacts-<tag>/ (report.json, samples.md) — git 밖.
사용: uv run python -m eval.spec.run_trap_facts --inputs A.json B.jsonl --tag before \\
        [--openai-key-env OPENAI_PREMIUM_API_KEY]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

from eval.spec.run_draft import ROOT
from eval.spec.run_solo import _client
from task_spec.config import Settings
from task_spec.llm import LLMClient
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import JudgeTrapsInput, JudgeTrapsOutput, PublicIncident
from task_spec.spec.draft import _CONCLUSION_LINE, incident_copied
from task_spec.spec.incidents import load_incidents

EVAL = ROOT / "data" / "eval" / "spec"
TASK = "judge_trap_facts"


def load_drafts(path: Path) -> list[dict[str, Any]]:
    """파일 형식마다 (sub, src, draft) 로 맞춘다. 자료가 없는 v1·v2 초안은 뺀다."""
    out: list[dict[str, Any]] = []
    if path.suffix == ".jsonl":
        for line in path.read_text("utf-8").splitlines():
            r = json.loads(line)
            if r.get("draft"):
                out.append(
                    {
                        "sub": r["sub"],
                        "src": f"{path.parent.name}:{r['posting']}",
                        "draft": r["draft"],
                    }
                )
    else:
        d = json.loads(path.read_text("utf-8"))
        if "items" in d:  # run_solo report
            out += [
                {
                    "sub": it["sub"],
                    "src": f"{path.parent.name}:{it['posting']}",
                    "draft": it["draft"],
                }
                for it in d["items"]
                if it["kind"] == "common"
            ]
        elif "track" in d:  # TaskItem (예시)
            sub = (d.get("source_incident") or "pub:?:?").split(":")[1]
            out.append({"sub": sub, "src": path.parent.name, "draft": d})
    return [x for x in out if x["draft"].get("artifacts")]


def task_text(d: dict[str, Any]) -> str:
    anchors = d.get("behavioral_anchors") or {}
    view = {
        "title": d.get("title"),
        "scenario": d.get("scenario"),
        "deliverables": d.get("deliverables"),
        "max_requests": d.get("max_requests"),
        "artifacts": [
            {k: a[k] for k in ("artifact_id", "role", "title", "content")}
            for a in d.get("artifacts") or []
        ],
        # 출제자의 근거 설명(why_ai_fails)은 뺀다 — v1 초안 시험: 설명의 틀린 계산(16×2=32)을 그대로 따라 함
        "traps": [
            {"trap_id": t["trap_id"], "description": t["description"]} for t in d.get("traps") or []
        ],
        "behavioral_anchors": {
            k: (v.get("behavior") if isinstance(v, dict) else v) for k, v in anchors.items()
        },
    }
    return json.dumps(view, ensure_ascii=False, indent=1)


def reference(inc: PublicIncident | None) -> str:
    if inc is None:
        return "(없음)"
    return "\n".join(
        [
            f"실제 원인: {inc.root_cause}",
            f"흔한 처방: {inc.naive_fix}",
            f"흔한 처방이 틀리는 이유: {inc.why_naive_fails}",
            "정답으로 인정할 조치: " + (" / ".join(inc.accepted_fixes) or "(없음)"),
        ]
    )


def deterministic(d: dict[str, Any], inc: PublicIncident | None) -> dict[str, Any]:
    arts = d.get("artifacts") or []
    text = "\n".join(
        [d["title"], d["scenario"], *d["deliverables"], *(a["title"] for a in arts)]
        + [a["content"] for a in arts]
    )
    lines = [
        f"{a['artifact_id']}[{a['role']}] {m.group(0)[:70]}"
        for a in arts
        if a["role"] in ("key", "support")
        for m in [_CONCLUSION_LINE.search(a["content"])]
        if m
    ]
    return {"copied": incident_copied(text, inc) if inc else [], "conclusion_lines": lines}


async def judge(
    llm: LLMClient, prompts: PromptRegistry, d: dict[str, Any], inc: PublicIncident | None
) -> dict[str, Any]:
    payload = JudgeTrapsInput(task=task_text(d), reference=reference(inc))
    prompt = assemble(prompts.get(TASK), payload, ontology_version="eval")
    try:
        r = await llm.call(prompt, JudgeTrapsOutput)
    except Exception as exc:  # 평가 스크립트 — 한 건 실패로 전체를 멈추지 않는다
        return {"error": type(exc).__name__}
    return {**r.parsed.model_dump(), "cost_usd": r.provenance.cost_usd or 0.0}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", type=Path, required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--subs", nargs="*", default=None)
    ap.add_argument(
        "--openai-key-env",
        default=None,
        help="이번 실행에서 OpenAI 키를 읽을 .env 변수 이름 (별도 예산 키 — 값은 출력하지 않음)",
    )
    a = ap.parse_args()
    st = Settings()
    incidents = load_incidents(st.incidents_dir)
    seen: set[tuple[str, str]] = set()
    rows = []
    for r in (r for p in a.inputs for r in load_drafts(p)):  # 같은 과제를 여러 풀이 실행이 공유한다
        key = (r["sub"], r["draft"]["title"])
        if key not in seen:
            seen.add(key)
            rows.append(r)
    if a.subs:
        rows = [r for r in rows if r["sub"] in a.subs]
    llm = _client(a.openai_key_env)
    prompts = PromptRegistry(ROOT / "prompts")
    sem = asyncio.Semaphore(6)

    async def one(r: dict[str, Any]) -> dict[str, Any]:
        inc = incidents.get(r["sub"])
        async with sem:
            j = await judge(llm, prompts, r["draft"], inc)
        return {"sub": r["sub"], "src": r["src"], **deterministic(r["draft"], inc), "judge": j}

    results = await asyncio.gather(*(one(r) for r in rows))
    ok = [x for x in results if "error" not in x["judge"]]
    traps = [t for x in ok for t in x["judge"]["traps"]]
    report = {
        "tag": a.tag,
        "n": len(results),
        "judged": len(ok),
        "traps": len(traps),
        "trap_unsound": sum(t["verdict"] == "unsound" for t in traps),
        "trap_unclear": sum(t["verdict"] == "unclear" for t in traps),
        "tasks_with_unsound": sum(
            any(t["verdict"] == "unsound" for t in x["judge"]["traps"]) for x in ok
        ),
        "tasks_with_correct_but_penalized": sum(
            bool(x["judge"]["correct_but_penalized"]) for x in ok
        ),
        "tasks_with_inconsistency": sum(bool(x["judge"]["inconsistencies"]) for x in ok),
        "tasks_copied": sum(bool(x["copied"]) for x in results),
        "tasks_conclusion_line": sum(bool(x["conclusion_lines"]) for x in results),
        "cost_usd": round(sum(x["judge"].get("cost_usd", 0.0) for x in ok), 4),
    }
    out = EVAL / f"{date.today()}-trapfacts-{a.tag}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({**report, "rows": results}, ensure_ascii=False, indent=1), "utf-8"
    )
    md = []
    for x in results:
        j = x["judge"]
        body = (
            "\n".join(
                f"- {t['trap_id']} **{t['verdict']}** — {t['reason']}"
                + (f"\n  - 검산: {t['check']}" if t["check"] else "")
                for t in j.get("traps", [])
            )
            or f"판정 실패: {j.get('error')}"
        )
        md.append(
            f"### {x['sub']} · {x['src']}\n\n{body}\n\n"
            f"감점되는 정답: {j.get('correct_but_penalized') or '없음'}  \n"
            f"수치·사실 불일치: {j.get('inconsistencies') or '없음'}  \n"
            f"원 사례 복사: {x['copied'] or '없음'}  \n판정·요약 줄: {x['conclusion_lines'] or '없음'}\n"
        )
    (out / "samples.md").write_text("\n---\n\n".join(md), "utf-8")
    print(json.dumps(report, ensure_ascii=False))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
