"""plan_contract 스모크 — 배송 감사 손 예시 명세로 공개 계약·숨은 테스트 계획을 만들고 손 예시 API 와 비교 (spec-to-code.md §11 3단계).

입력: git 밖 손 예시 `data/feature_starters/delivery-audit/` 의 r2b 지원자 README(상황·요구·수용 기준·제출물만 — **API 계약 절은 넣지 않는다**,
그게 생성 대상이다)와 진행자 자료(`solo_harness/facilitator/data-r2b.json`: 요청 자료·추가 요구 답), 그리고 레포의 기능형 재료(sw.backend).
가상 과제라 지원자 데이터가 아니다. 비교 기준(손 예시 API 계약)은 같은 README 의 "API 계약" 절에서 실행 때 읽는다.

출력: data/eval/build/<날짜>-plan-contract-<tag>/ (report.json · contract-<n>.json · compare.md) — git 밖.

사용:
  uv run python -m eval.starter.run_plan_contract --model openai:gpt-4.1 --openai-key-env OPENAI_PREMIUM_API_KEY --runs 2 --budget-usd 0.5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from eval.spec.run_feature import ModelOverride, make_client
from task_spec.nodes.starter.plan_contract import PlanResult, plan_contract
from task_spec.prompting import PromptRegistry
from task_spec.schemas import TargetCompetency, TaskContract, TaskItem, TechEnv
from task_spec.spec.incidents import load_incidents
from task_spec.starter.contract import SpecView, extract_values
from task_spec.starter.templates import load_template

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "data" / "feature_starters" / "delivery-audit"
JAVA = "backend/java-spring-boot3-jpa"
# 손 예시 하네스가 실제로 쓴 운영 값 (r2b 채점 환경변수 주입 · T1ConcurrencyTest) — HiddenPlan 비교 기준
EXPECTED_HARNESS = {
    "pool_size": 8,
    "connection_timeout_ms": 5000,
    "server_threads": 32,
    "concurrency": 32,
}
# 손 예시 숨은 테스트(T1·T2CommitFailure)를 명세의 숨김 수용 기준 문장으로 — 재료(sw.backend) 숨은 요구 t1 하나에 연결
HIDDEN_ACCEPTANCE = [
    (
        "h1",
        "운영 피크와 같은 수의 상태 변경 요청이 한꺼번에 들어와도 모든 요청이 정상 응답(성공 또는 규칙 위반)하고, "
        "요청마다 감사 기록이 결과와 맞게 1건 남는다.",
    ),
    (
        "h2",
        "전이 규칙은 통과했지만 저장 단계에서 실패해 롤백된 변경은 SUCCESS 로 남지 않고 FAILURE 로 남는다.",
    ),
]
TOPICS = {"c1": "요청량·피크", "c2": "실패 처리", "c3": "조회 범위"}


def _section(readme: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", readme, re.M | re.S)
    return m.group(1).strip() if m else ""


def build_task(example: Path, variant: str = "r2b") -> tuple[TaskItem, str]:
    """손 예시 README·진행자 자료 → 승인된 기능형 명세 모양의 TaskItem. API 계약 절은 넣지 않는다."""
    readme = (example / "variants" / variant / "starter" / "README.md").read_text("utf-8")
    data = json.loads(
        (example / "solo_harness" / "facilitator" / f"data-{variant}.json").read_text("utf-8")
    )
    seed = load_incidents(ROOT / "incidents" / "public_v1")["sw.backend"].feature
    assert seed is not None
    title = re.search(r"^# (.+)$", readme, re.M).group(1).strip()  # type: ignore[union-attr]
    reqs = re.findall(r"^- \*\*(r\d+)\*\* (.+)$", _section(readme, "요구사항"), re.M)
    accs = re.findall(r"^- (.+)$", _section(readme, "수용 기준"), re.M)
    deliv = re.findall(r"^\d+\. (.+(?:\n {3}.+)*)", _section(readme, "제출물"), re.M)
    roles = {"a2": "key", "a3": "key", "a6": "support", "a8": "distractor"}
    task = TaskItem(
        task_id="smoke-delivery-audit",
        track="common",
        task_kind="feature",
        target_competencies=[TargetCompetency(competency_id="comp:sw.backend", label="백엔드")],
        tech_env=TechEnv(languages=["Java"], basis="손 예시"),
        difficulty_target="high",
        time_limit_min=60,
        title=title,
        scenario=_section(readme, "상황"),
        requirements=[{"req_id": i, "text": t} for i, t in reqs],  # type: ignore[misc]
        acceptance_public=[{"ac_id": f"p{n}", "text": t} for n, t in enumerate(accs, 1)],  # type: ignore[misc]
        deliverables=[" ".join(d.split()) for d in deliv],
        traps=[
            {  # type: ignore[list-item]
                "trap_id": "t1",
                "description": f"{seed.hidden_requirement} (운영 조건: {seed.trigger})",
                "why_ai_fails": f"{seed.naive_impl} — {seed.why_naive_breaks}",
            }
        ],
        acceptance_hidden=[{"ac_id": h, "trap_id": "t1", "text": t} for h, t in HIDDEN_ACCEPTANCE],  # type: ignore[misc]
        artifacts=[
            {
                "artifact_id": m["id"],
                "title": m["name"],
                "content": m["body"],
                "role": roles.get(m["id"], "support"),
            }  # type: ignore[misc]
            for m in data["materials"]
        ],
        clarifications=[
            {
                "clarification_id": c["id"],
                "topic": TOPICS.get(c["id"], c["id"]),
                "answer": c["answer"],
                "reveals": ["t1"] if c["id"] == "c1" else [],
            }  # type: ignore[misc]
            for c in data["clarifications"]
        ],
    )
    return task, readme


class PinnedPrompts:
    """plan_contract 버전을 고정한 레지스트리 (CURRENT 는 평가 뒤에 바꾼다)."""

    def __init__(self, base: PromptRegistry, version: int | None) -> None:
        self.base, self.version = base, version

    def get(self, task: str, version: int | None = None) -> Any:
        return self.base.get(task, version or (self.version if task == "plan_contract" else None))


def hand_api(readme: str) -> dict[str, Any]:
    """손 예시 README "API 계약" 절 — 비교 기준."""
    api = _section(readme, "API 계약")
    endpoints = {
        f"{m} {re.sub(r'{[^}]+}', '{}', p)}": []
        for m, p in re.findall(r"^### `(\w+) (\S+)`", api, re.M)
    }
    statuses: dict[str, set[int]] = {}
    current = None
    for line in api.splitlines():
        h = re.match(r"^### `(\w+) (\S+)`", line)
        if h:
            current = f"{h.group(1)} {re.sub(r'{[^}]+}', '{}', h.group(2))}"
            statuses[current] = set()
        elif current:
            statuses[current] |= {int(s) for s in re.findall(r"`(\d{3})`", line)}
    return {
        "endpoints": sorted(endpoints),
        "statuses": {k: sorted(v) for k, v in statuses.items()},
        "headers": sorted(set(re.findall(r"헤더 `([A-Za-z-]+)`", api))),
        "fields": sorted(set(re.findall(r"^\s*\| `(\w+)` \|", api, re.M))),
        "body_keys": sorted(set(re.findall(r'"(\w+)":', api))),
        "error_codes": sorted(set(re.findall(r'"code": "([A-Z_]+)"', api))),
    }


def generated_api(c: TaskContract) -> dict[str, Any]:
    eps = {f"{e.method} {re.sub(r'{[^}]+}', '{}', e.path)}": e for e in c.endpoints}
    fields = {f.name for e in c.endpoints for r in e.responses for f in r.body}
    fields |= {f.name for e in c.endpoints for f in e.request}
    fields |= {f.name for m in c.entities for f in m.fields}
    text = json.dumps(c.model_dump(), ensure_ascii=False)
    return {
        "endpoints": sorted(eps),
        "statuses": {k: sorted({r.status for r in e.responses}) for k, e in eps.items()},
        "headers": sorted({h.name for e in c.endpoints for h in e.headers}),
        "fields": sorted(fields),
        "error_codes": sorted(
            set(re.findall(r"\b[A-Z][A-Z_]{4,}\b", text)) - {"SUCCESS", "FAILURE"}
        ),
    }


def compare(hand: dict[str, Any], gen: dict[str, Any]) -> dict[str, Any]:
    he, ge = set(hand["endpoints"]), set(gen["endpoints"])
    return {
        "endpoints_same": sorted(he & ge),
        "endpoints_missing": sorted(he - ge),
        "endpoints_extra": sorted(ge - he),
        "statuses": {
            k: {"hand": hand["statuses"].get(k, []), "gen": gen["statuses"].get(k, [])}
            for k in sorted(he | ge)
        },
        "headers_missing": sorted(set(hand["headers"]) - set(gen["headers"])),
        "audit_fields_missing": sorted(set(hand["fields"]) - set(gen["fields"])),
        "body_keys_missing": sorted(
            set(hand["body_keys"]) - set(gen["fields"]) - {"code", "message"}
        ),
        "error_codes_missing": sorted(set(hand["error_codes"]) - set(gen["error_codes"])),
    }


def row(n: int, res: PlanResult, hand: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "run": n,
        "ok": res.ok,
        "first_problems": res.first_problems,
        "problems": res.problems,
        "spec_gaps": res.spec_gaps,
        "flags": res.flags,
        "fail_reasons": res.fail_reasons,
        "calls": len(res.provenance),
        "cost_usd": round(sum(p.cost_usd or 0.0 for p in res.provenance), 4),
        "latency_s": round(sum(p.latency_ms or 0 for p in res.provenance) / 1000, 1),
        "model": res.provenance[0].model if res.provenance else None,
    }
    if res.contract is not None and res.hidden_plan is not None:
        gen = generated_api(res.contract)
        out["generated"] = gen
        out["compare"] = compare(hand, gen)
        out["harness"] = res.hidden_plan.harness
        out["harness_vs_hand"] = {
            k: {"hand": v, "gen": res.hidden_plan.harness.get(k)}
            for k, v in EXPECTED_HARNESS.items()
        }
        out["cases"] = [c.model_dump() for c in res.hidden_plan.cases]
    return out


def compare_md(rows: list[dict[str, Any]], hand: dict[str, Any]) -> str:
    lines = [
        "# plan_contract 스모크 — 배송 감사 손 예시와 비교",
        "",
        f"손 예시 API: {', '.join(hand['endpoints'])} · 헤더 {', '.join(hand['headers'])} · "
        f"감사 필드 {', '.join(hand['fields'])} · 오류 코드 {', '.join(hand['error_codes'])}",
        "",
    ]
    for r in rows:
        lines.append(
            f"## run {r['run']} — {'통과' if r['ok'] else '실패'} · 호출 {r['calls']} · ${r['cost_usd']} · {r['latency_s']}s"
        )
        if r["first_problems"]:
            lines.append("- 첫 답 문제: " + " / ".join(p[:160] for p in r["first_problems"]))
        if r["problems"] or r["spec_gaps"]:
            lines.append(
                "- 남은 문제: " + " / ".join(p[:160] for p in r["problems"] + r["spec_gaps"])
            )
        if "compare" in r:
            c = r["compare"]
            lines += [
                f"- 엔드포인트: 같음 {c['endpoints_same']} · 빠짐 {c['endpoints_missing']} · 더함 {c['endpoints_extra']}",
                f"- 상태 코드: {c['statuses']}",
                f"- 빠진 헤더 {c['headers_missing']} · 빠진 감사 필드 {c['audit_fields_missing']} · "
                f"빠진 응답 키 {c['body_keys_missing']} · 빠진 오류 코드 {c['error_codes_missing']}",
                f"- 하네스 값: {r['harness_vs_hand']}",
                "- 숨은 케이스: "
                + " / ".join(
                    f"{x['case_id']} {x['ac_id']} {x['intended_reason']}: {x['scenario']}"
                    for x in r["cases"]
                ),
            ]
        lines.append("")
    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="예: openai:gpt-4.1 (기본: 프롬프트 머리말 모델)")
    ap.add_argument("--openai-key-env", default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--budget-usd", type=float, default=0.5)
    ap.add_argument("--variant", default="r2b")
    ap.add_argument("--tag", default="")
    ap.add_argument(
        "--prompt-version", type=int, default=None, help="plan_contract 버전 (기본 CURRENT)"
    )
    a = ap.parse_args()

    task, readme = build_task(EXAMPLE, a.variant)
    hand = hand_api(readme)
    template = load_template(JAVA)
    seed = load_incidents(ROOT / "incidents" / "public_v1")["sw.backend"].feature
    view = SpecView.from_task(task)
    print(
        "명세에서 뽑은 운영 수치:",
        {
            p.name: extract_values(p, view.visible_texts)
            for p in template.manifest.harness_params
            if p.visible
        },
    )
    llm = make_client(a.openai_key_env)
    prompts = ModelOverride(
        PinnedPrompts(PromptRegistry(ROOT / "prompts"), a.prompt_version), a.model
    )  # type: ignore[arg-type]
    version = "smoke"  # 온톨로지를 읽지 않는 스모크 — provenance 의 ontology_version 자리
    rows: list[dict[str, Any]] = []
    spent, worst = 0.0, 0.0
    for n in range(1, a.runs + 1):
        if worst and spent + worst > a.budget_usd:
            print(f"예산 {a.budget_usd} 때문에 run {n}부터 건너뜀")
            break
        res = await plan_contract(
            task,
            template,
            llm=llm,
            prompts=prompts,
            ontology_version=str(version),
            seed=seed,  # type: ignore[arg-type]
        )
        r = row(n, res, hand)
        rows.append(r)
        spent += r["cost_usd"]
        worst = max(worst, r["cost_usd"])
        print(
            f"  run {n}: ok={r['ok']} calls={r['calls']} ${r['cost_usd']} (누적 ${spent:.4f})",
            flush=True,
        )
        if res.contract is not None:
            out_dir = (
                ROOT
                / "data"
                / "eval"
                / "build"
                / f"{date.today()}-plan-contract{('-' + a.tag) if a.tag else ''}"
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"contract-{n}.json").write_text(
                json.dumps(
                    {
                        "contract": res.contract.model_dump(),
                        "hidden_plan": res.hidden_plan.model_dump() if res.hidden_plan else None,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                "utf-8",
            )
    out_dir = (
        ROOT
        / "data"
        / "eval"
        / "build"
        / f"{date.today()}-plan-contract{('-' + a.tag) if a.tag else ''}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "model": a.model,
        "variant": a.variant,
        "hand_api": hand,
        "runs": rows,
        "cost_usd": round(spent, 4),
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    (out_dir / "compare.md").write_text(compare_md(rows, hand), "utf-8")
    print(compare_md(rows, hand))
    print(f"→ {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
