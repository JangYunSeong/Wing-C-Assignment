"""기능 개발형 공통 과제(draft_feature_task) 실제 공고 점검 — 정식 평가(판정 LLM·사실 검산·AI 단독 풀이) 전 단계.

표본: 온톨로지 시범 공고 — 지원자 데이터 아님.
- 기본: 백엔드(sw.backend) 공고 중 기본값 규칙(spec/kind.py)이 feature 로 고르는 공고, id 순으로 번갈아 dev / test.
- --subs: 그 직군들에서 공고를 per-sub 건씩, 규칙과 상관없이 기능형으로 만든다 (직군별 생성 점검). 규칙 결과는 기록만.
자동 지표: ok_first(첫 답 검사 통과) · ok_final(재요청 뒤 통과, 고쳐 쓰기 없음) · failed · 개수(공개 요구·숨은 요구·
허용 설계·추가 요구·1·2단계 자료) · cost_usd · latency_s. 판정 LLM 은 아직 없다 (judge_feature_task — 설계 §9, PR-4).
출력: data/eval/spec/<날짜>-feature-<split|subs>/ (report.json, drafts.jsonl, samples.md) — git 밖.
samples.md 는 사람 검토용 전체 본문(숨김 부분 포함).

사용:
  uv run python -m eval.spec.run_feature --split dev --n 3
  uv run python -m eval.spec.run_feature --subs sw.frontend sw.backend data.eng infra.dba data.ml --per-sub 1 \\
      --model openai:gpt-4.1 --openai-key-env OPENAI_PREMIUM_API_KEY --budget-usd 0.5
--save-raw (#280 D): 받은 답 원문을 전부 모아 같은 파서·출력 모델로 다시 읽어 본다 — 형식 오류율(분모 있음)·고쳐 읽기·
JSON 깨짐 원인·추가 요구 항목별 칸을 raw_report.json 에, 실패·고쳐 읽은 원문을 raw/ 에, INFO 로그를 llm.log 에.
시범 공고라 원문을 남겨도 된다 (samples.md 도 전체 본문).
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

from pydantic import BaseModel, ValidationError

from task_spec.config import Settings
from task_spec.llm import LLMClient
from task_spec.llm.backend import ChatBackend, Completion, TextCallback
from task_spec.llm.client import _parse_json, _violation_kinds
from task_spec.llm.factory import build_llm_client
from task_spec.ontology.queries import evidence as query_evidence
from task_spec.ontology.queries import posting_profile
from task_spec.ontology.relatedness import load_relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import Message, PromptRegistry, PromptTemplate
from task_spec.schemas import DraftFeatureOutput, FieldFixOutput, PublicIncident, TaskItem
from task_spec.spec.build import CommonDraft, build_common
from task_spec.spec.draft import FLAG_DRAFT_FAILED, FLAG_DRAFT_INVALID
from task_spec.spec.feature import apply_feature, draft_feature_task
from task_spec.spec.incidents import feature_incident, load_incidents
from task_spec.spec.kind import choose_task_kind

ROOT = Path(__file__).resolve().parents[2]
SUB = "sw.backend"
# 모델 바꿔 끼우기 (평가 전용). 기능형 출력이 길어 max_tokens 를 크게 둔다
PARAMS: dict[str, dict[str, Any]] = {
    "openai:gpt-4.1": {"temperature": 0.3, "max_tokens": 16000},
    "openai:gpt-5.4-mini": {"max_tokens": 16000, "reasoning_effort": "medium"},
}


class ModelOverride:
    """프롬프트 본문·버전은 그대로 두고 모델·설정만 바꾼 레지스트리."""

    def __init__(
        self,
        base: PromptRegistry,
        model: str | None,
        version: int | None = None,
        per_task: dict[str, str] | None = None,
    ) -> None:
        self.base, self.model, self.version = base, model, version
        self.per_task = (
            per_task or {}
        )  # 단계별 모델 (예: gen_hidden → opencode:glm-5.3) — model 보다 우선

    def get(self, task: str, version: int | None = None) -> PromptTemplate:
        t = self.base.get(task, version or (self.version if task == "draft_feature_task" else None))
        # 템플릿별 프롬프트(gen_hidden_react · gen_hidden_py 등)는 기본 이름(gen_hidden)의 지정을 따른다
        model = (
            self.per_task.get(task)
            or self.per_task.get(task.removesuffix("_react").removesuffix("_py"))
            or self.model
        )
        if not model:
            return t
        params = PARAMS.get(model, t.meta.params)
        meta = t.meta.model_copy(update={"model": model, "params": params})
        return t.model_copy(update={"meta": meta})


_POSTING: contextvars.ContextVar[str] = contextvars.ContextVar("posting", default="?")


class RecordingBackend:
    """--save-raw: 받은 답 원문을 공고별로 모은다 (평가 전용 — 시범 공고, 지원자 데이터 아님)."""

    def __init__(self, inner: ChatBackend, sink: list[dict[str, Any]]) -> None:
        self.inner, self.sink = inner, sink

    async def complete(
        self,
        model: str,
        messages: list[Message],
        params: dict[str, Any],
        *,
        on_delta: TextCallback | None = None,
    ) -> Completion:
        c = await self.inner.complete(model, messages, params, on_delta=on_delta)
        self.sink.append(
            {
                "posting": _POSTING.get(),
                "model": model,
                "finish_reason": c.finish_reason,
                "tokens_out": c.usage.output,
                "content": c.content,
            }
        )
        return c


def _clarification_shape(data: Any) -> list[dict[str, Any]] | None:
    """추가 요구 항목마다 단서인지(reveals)·있는 칸 — answer 를 빠뜨린 항목이 단서인지 보려고 (#280 D3 가설)."""
    items = data.get("clarifications") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [
        {"i": i, "clue": bool(it.get("reveals")), "keys": sorted(it)}
        if isinstance(it, dict)
        else {"i": i, "type": type(it).__name__}
        for i, it in enumerate(items)
    ]


def classify(content: str, out_model: type[BaseModel]) -> dict[str, Any]:
    """클라이언트와 같은 파서·출력 모델로 다시 읽기 — ok·repaired·json_error·schema_error, 고쳐 읽기 기능형 초안이 아닌
    호출(부분 수리 fix_feature_fields)은 fix_fields."""
    try:
        data, fixed = _parse_json(content)
    except ValueError as exc:
        return {"status": "json_error", "kind": _violation_kinds(exc)}
    if (
        isinstance(data, dict)
        and set(FieldFixOutput.model_fields) & set(data)
        and "title" not in data
    ):
        return {"status": "fix_fields", "repaired": list(fixed)}
    try:
        out_model.model_validate(data)
    except ValidationError as exc:
        clar = [
            f"{'.'.join(map(str, e['loc']))}:{e['type']}"
            for e in exc.errors()
            if e["loc"] and e["loc"][0] == "clarifications"
        ]
        return {
            "status": "schema_error",
            "repaired": list(fixed),
            "kind": _violation_kinds(exc),
            "clarification_errors": clar,
            "clarifications": _clarification_shape(data) if clar else None,
        }
    return {"status": "repaired" if fixed else "ok", "repaired": list(fixed)}


def raw_report(
    raw: list[dict[str, Any]], out_model: type[BaseModel], out_dir: Path
) -> dict[str, Any]:
    """원문마다 다시 읽은 결과(raw.jsonl — 원문 없이) + 실패·고쳐 읽은 원문(raw/) + 요약."""
    (out_dir / "raw").mkdir(exist_ok=True)
    rows: list[dict[str, Any]] = []
    for n, rec in enumerate(raw, 1):
        info = classify(rec["content"], out_model)
        row = {k: v for k, v in rec.items() if k != "content"} | {"n": n, **info}
        if info["status"] in ("json_error", "schema_error", "repaired"):
            name = f"{n:02d}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', rec['posting'])}.txt"
            (out_dir / "raw" / name).write_text(rec["content"], "utf-8")
            row["raw_file"] = f"raw/{name}"
        rows.append(row)
    with (out_dir / "raw.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    drafts = [r for r in rows if r["status"] != "fix_fields"]
    return {
        "draft_calls": len(drafts),
        "status": dict(Counter(r["status"] for r in drafts)),
        "finish_reason": dict(Counter(str(r["finish_reason"]) for r in drafts)),
        "repaired": dict(Counter(k for r in drafts for k in r.get("repaired", []))),
        "json_error": [r["kind"] for r in drafts if r["status"] == "json_error"],
        "schema_error": [r["kind"] for r in drafts if r["status"] == "schema_error"],
        "clarifications_on_error": [
            {
                "n": r["n"],
                "posting": r["posting"],
                "errors": r["clarification_errors"],
                "items": r["clarifications"],
            }
            for r in drafts
            if r["status"] == "schema_error" and r["clarification_errors"]
        ],
        "fix_fields_calls": len(rows) - len(drafts),
    }


def make_client(key_env: str | None) -> LLMClient:
    """기본 = 운영 설정. key_env 를 주면 OpenAI 만 그 키(예: OPENAI_PREMIUM_API_KEY)로 (run_solo 와 같은 방식)."""
    st = Settings()  # type: ignore[call-arg]
    if not key_env:
        return build_llm_client(st)
    from dotenv import dotenv_values
    from pydantic import SecretStr

    from task_spec.llm.openai_compat import OpenAICompatBackend

    key = dotenv_values(ROOT / ".env").get(key_env) or os.environ.get(key_env)
    if not key:
        raise SystemExit(f"{key_env} 가 .env·환경 변수에 없습니다")
    return LLMClient(
        {
            "openai": OpenAICompatBackend(api_key=SecretStr(key), timeout=st.llm_timeout_seconds),
            "opencode": OpenAICompatBackend(
                api_key=st.opencode_api_key,
                base_url=st.opencode_base_url,
                timeout=st.llm_timeout_seconds,
                max_tokens_field=True,  # 추론 예산이 듣게 (openai_compat 머리말)
            ),
        }
    )


async def pick(
    repo: OntologyRepository,
    version: str,
    incidents: dict[str, PublicIncident],
    sub: str = SUB,
    *,
    force: bool = False,
    limit: int | None = None,
    incident: str | None = None,
) -> tuple[list[tuple[str, CommonDraft]], dict[str, Any]]:
    """force=False: 기본값 규칙이 feature 인 공고만 / True: 규칙과 상관없이 기능형으로 (직군별 점검).
    incident: 공고 해시 배정(feature_incident)이 그 사례인 공고만 — 새 재료 점검 (배정 규칙은 그대로, 공고만 고른다)."""
    rel = await load_relatedness(repo)
    rows = await repo.fetch(
        "SELECT id FROM position WHERE in_pilot AND sub = %s ORDER BY id", (sub,)
    )
    kinds: Counter[str] = Counter()
    basis: list[dict[str, Any]] = []
    out: list[tuple[str, CommonDraft]] = []
    for r in rows:
        p = await posting_profile(repo, r["id"])
        if p is None or not (p.sub and p.industry_id):
            continue
        if incident is not None:
            inc = feature_incident(incidents, p.sub, p.posting_id)
            if inc is None or inc.incident_id != incident:
                continue
        choice = choose_task_kind(p)
        kinds[choice.kind] += 1
        basis.append({"posting": r["id"], "kind": choice.kind, "basis": choice.basis})
        if (choice.kind != "feature" and not force) or (limit is not None and len(out) >= limit):
            continue
        ev = await query_evidence(
            repo,
            p.competency_ids,
            sub=p.sub,
            industry_id=p.industry_id,
            secondary_sub=p.secondary_sub,
            in_pilot=p.in_pilot,
        )
        built = await build_common(
            p,
            ev,
            task_id=f"eval-{r['id']}",
            ontology_version=version,
            ontology=repo,
            rel=rel,
            incidents=incidents,
            task_kind="feature",
        )
        if built.task is not None and built.ctx is not None:
            out.append((r["id"], built))
    return out, {"n_postings": sum(kinds.values()), "kinds": dict(kinds), "basis": basis}


async def run_one(
    llm: LLMClient,
    reg: Any,
    pid: str,
    built: CommonDraft,
    sem: asyncio.Semaphore,
    *,
    repair_first: bool = True,
) -> dict[str, Any]:
    assert built.ctx is not None and built.task is not None
    _POSTING.set(pid)  # --save-raw 원문을 공고에 묶는다 (gather 한 태스크마다 따로)
    async with sem:
        t0 = time.monotonic()
        res = await draft_feature_task(built.ctx, llm=llm, prompts=reg, repair_first=repair_first)
        dt = time.monotonic() - t0
    row: dict[str, Any] = {
        "posting": pid,
        "sub": built.ctx.sub_label,
        "model": res.provenance[0].model if res.provenance else None,
        "incident": res.incident_id,
        "failed": FLAG_DRAFT_FAILED in res.flags,
        "ok_first": res.output is not None and not res.first_problems,
        "ok_final": res.output is not None
        and not [f for f in res.flags if f.startswith(FLAG_DRAFT_INVALID)],
        "first_problems": res.first_problems,
        "patched": res.patched,  # 부분 수리(spec/patch.py)로 풀린 문제
        # 전체 생성 시도 수 — 성공한 기능형 초안 호출 + 통째로 실패한 호출 (재요청이면 2)
        "attempts": sum(1 for p in res.provenance if p.prompt.startswith("draft_feature_task"))
        + len(res.fail_reasons),
        "flags": res.flags,
        "fail_reasons": res.fail_reasons,
        "cost_usd": sum(p.cost_usd or 0.0 for p in res.provenance),
        "latency_s": round(dt, 1),
        "competencies": [t.label for t in built.ctx.targets],
        "tech_env": built.ctx.env.languages,
    }
    out = res.output
    if isinstance(out, DraftFeatureOutput):
        dbg = out.debug_stage
        row |= {
            "n_req": len(out.requirements),
            "n_trap": len(out.traps),
            "n_design": len(out.accepted_designs),
            "n_clar": len(out.clarifications),
            "n_art1": len(out.artifacts),
            "n_art2": len(dbg.artifacts),
            "scen_len": len(out.scenario),
            "debug_len": len(dbg.scenario),
            "_task": apply_feature(built.task, res),
        }
    return row


def _arts(arts: list[Any]) -> list[str]:
    return [f"- {a.artifact_id} [{a.role}] {a.title}\n```\n{a.content}\n```" for a in arts]


def sample_md(row: dict[str, Any]) -> str:
    head = (
        f"## {row['sub']} · {row['posting']} — 사례 {row.get('incident') or '없음'} · {row.get('model')}\n"
        f"역량: {', '.join(row['competencies'])} · 기술 {', '.join(row['tech_env']) or '-'} · "
        f"첫 답 {'통과' if row['ok_first'] else '실패'} · 최종 {'통과' if row['ok_final'] else '실패'} · "
        f"${row['cost_usd']:.4f} · {row['latency_s']}s\n"
    )
    if row.get("first_problems"):
        head += "첫 답 문제: " + " / ".join(row["first_problems"]) + "\n"
    if row.get("fail_reasons"):
        head += "실패 사유: " + " / ".join(row["fail_reasons"]) + "\n"
    task: TaskItem | None = row.get("_task")
    if task is None:
        return head + "\n(서술 없음)\n"
    lines = [head, f"### {task.title}", task.scenario or "", "", "**공개 요구**"]
    lines += [f"- {r.req_id} {r.text}" for r in task.requirements]
    lines += ["", "**공개 수용 기준**"] + [f"- {a.ac_id} {a.text}" for a in task.acceptance_public]
    lines += ["", "**제출물 (1단계)**"] + [f"- {d}" for d in task.deliverables]
    lines += ["", f"**1단계 자료** (최대 {task.max_requests}개 요청)"] + _arts(task.artifacts)
    lines += ["", f"**추가 요구** (질문 최대 {task.max_clarifications}개)"] + [
        f"- {c.clarification_id} {c.topic}: {c.answer} (단서 {', '.join(c.reveals) or '-'})"
        for c in task.clarifications
    ]
    lines += ["", "**숨은 요구**"] + [
        f"- {t.trap_id} {t.description} — AI: {t.why_ai_fails}" for t in task.traps
    ]
    lines += ["", "**숨김 수용 기준**"] + [
        f"- {h.ac_id} ({h.trap_id}) {h.text}" for h in task.acceptance_hidden
    ]
    lines += ["", "**허용 설계**"] + [
        f"- {d.design_id} ({', '.join(d.trap_ids)}) {d.summary} — {d.conditions}"
        for d in task.accepted_designs
    ]
    dbg = task.debug_stage
    if dbg is not None:
        lines += ["", f"**2단계 디버깅 ({dbg.time_limit_min}분, 원인 {', '.join(dbg.trap_ids)})**"]
        lines += [dbg.scenario, ""] + [f"- 제출물: {d}" for d in dbg.deliverables]
        lines += _arts(dbg.artifacts)
        lines += ["- 관찰: " + ", ".join(f"{o.kind}={o.value}" for o in dbg.observables)]
    lines += ["", "**관찰 (1단계)**: " + ", ".join(f"{o.kind}={o.value}" for o in task.observables)]
    if task.behavioral_anchors is not None:
        a = task.behavioral_anchors
        lines += [
            "",
            f"**행동 기준** 1: {a.level_1.behavior}",
            f"3: {a.level_3.behavior}",
            f"5: {a.level_5.behavior}",
        ]
    if task.review_flags:
        lines += ["", "**검토 표시**: " + " / ".join(task.review_flags)]
    return "\n".join(lines) + "\n"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    done = [r for r in rows if "n_req" in r]

    def avg(k: str) -> float | None:
        return round(mean(r[k] for r in done), 2) if done else None

    return {
        "n": len(rows),
        "ok_first": sum(r["ok_first"] for r in rows),
        "second_attempts": sum(r.get("attempts", 1) >= 2 for r in rows),
        "ok_final": sum(r["ok_final"] for r in rows),
        "failed": sum(r["failed"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "latency_s": round(mean(r["latency_s"] for r in rows), 1) if rows else None,
        **{k: avg(k) for k in ("n_req", "n_trap", "n_design", "n_clar", "n_art1", "n_art2")},
        "first_problems": dict(
            Counter(p.split(" (")[0][:60] for r in rows for p in r["first_problems"])
        ),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument(
        "--n", type=int, default=3, help="표본 수 상한 (기본 모델 qwen: 공고당 약 $0.005–0.012)"
    )
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--tag", default="")
    ap.add_argument(
        "--subs", nargs="*", default=None, help="이 직군들에서 기능형 강제 (직군별 점검)"
    )
    ap.add_argument("--per-sub", type=int, default=1)
    ap.add_argument(
        "--incident",
        default=None,
        help="--subs 와 함께: 공고 해시 배정이 이 사례(incident_id)인 공고만 (새 재료 점검)",
    )
    ap.add_argument("--model", default=None, help="예: openai:gpt-4.1 (기본: 프롬프트 머리말 모델)")
    ap.add_argument("--openai-key-env", default=None, help="예: OPENAI_PREMIUM_API_KEY")
    ap.add_argument(
        "--prompt-version", type=int, default=None, help="draft_feature_task 버전 (기본 CURRENT)"
    )
    ap.add_argument(
        "--no-repair-first",
        action="store_true",
        help="코드로 고칠 수 있는 문제도 전체 재요청 (예전 순서 — #280 D 비교용)",
    )
    ap.add_argument(
        "--save-raw",
        action="store_true",
        help="받은 답 원문을 모아 다시 읽어 본다 — raw_report.json·raw/·llm.log (#280 D)",
    )
    ap.add_argument(
        "--budget-usd",
        type=float,
        default=None,
        help="누적 비용 상한 — 하나씩 돌리고, 지금까지 가장 비싼 공고만큼 남지 않으면 나머지를 건너뜀",
    )
    a = ap.parse_args()

    s = Settings()  # type: ignore[call-arg]
    repo = await OntologyRepository.open(s.database_url.get_secret_value(), "dev")
    version = repo.version.version
    incidents = load_incidents(s.incidents_dir)
    picked: list[tuple[str, CommonDraft]] = []
    dist: dict[str, Any] = {}
    if a.subs:
        for sub in a.subs:
            got, d = await pick(
                repo, version, incidents, sub, force=True, limit=a.per_sub, incident=a.incident
            )
            picked += got
            dist[sub] = d["kinds"]
        split_name = "subs"
    else:
        picked, dist = await pick(repo, version, incidents)
        split = {"dev": 0, "test": 1}
        picked = [x for i, x in enumerate(picked) if a.split == "all" or i % 2 == split[a.split]]
        picked = picked[: a.n]
        split_name = a.split
    await repo.close()
    shown = dist if a.subs else dist["kinds"]
    print(f"유형 분포 {shown} — 표본 {len(picked)}건: " + ", ".join(pid for pid, _ in picked))

    out_dir = (
        ROOT
        / "data"
        / "eval"
        / "spec"
        / f"{date.today()}-feature-{split_name}{('-' + a.tag) if a.tag else ''}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = make_client(a.openai_key_env)
    raw: list[dict[str, Any]] = []
    if a.save_raw:
        # 평가 전용 — 클라이언트의 공급자마다 원문 기록기를 씌운다
        llm = LLMClient({k: RecordingBackend(v, raw) for k, v in llm._backends.items()})
        log = logging.FileHandler(out_dir / "llm.log", encoding="utf-8")
        log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger("task_spec").addHandler(log)
        logging.getLogger("task_spec").setLevel(logging.INFO)
    reg = ModelOverride(PromptRegistry(ROOT / "prompts"), a.model, a.prompt_version)
    sem = asyncio.Semaphore(a.concurrency)
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    if a.budget_usd is None:
        rows = list(
            await asyncio.gather(
                *(
                    run_one(llm, reg, pid, b, sem, repair_first=not a.no_repair_first)
                    for pid, b in picked
                )
            )
        )
    else:
        spent, worst = 0.0, 0.0
        for pid, b in picked:
            if worst and spent + worst > a.budget_usd:
                skipped.append(pid)
                continue
            r = await run_one(llm, reg, pid, b, sem, repair_first=not a.no_repair_first)
            rows.append(r)
            spent += r["cost_usd"]
            worst = max(worst, r["cost_usd"])
            print(f"  {r['sub']} {pid} — ${r['cost_usd']:.4f} (누적 ${spent:.4f})", flush=True)
        if skipped:
            print(f"예산 {a.budget_usd} 때문에 건너뜀: {', '.join(skipped)}")

    samples = [sample_md(r) for r in rows]
    with (out_dir / "drafts.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            task = r.pop("_task", None)
            rec = {**r, "task": task.model_dump(mode="json") if task else None}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    report = {
        "split": split_name,
        "model": a.model,
        "prompt_version": a.prompt_version,
        "repair_first": not a.no_repair_first,
        "distribution": dist,
        "skipped_for_budget": skipped,
        "summary": summarize(rows),
    }
    if a.save_raw:
        out_model = reg.get("draft_feature_task").output_model
        report["raw"] = raw_report(raw, out_model, out_dir)
        (out_dir / "raw_report.json").write_text(
            json.dumps(report["raw"], ensure_ascii=False, indent=1), "utf-8"
        )
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    (out_dir / "samples.md").write_text("\n---\n\n".join(samples), "utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    if a.save_raw:
        print(
            json.dumps(
                {k: v for k, v in report["raw"].items() if k != "clarifications_on_error"},
                ensure_ascii=False,
            )
        )
    print(f"→ {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
