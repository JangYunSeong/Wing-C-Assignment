"""채용담당자 사례 인터뷰 프롬프트 평가 (#263 D1b) — 실제 LLM 호출 (비용 발생, 입력은 전부 가상).

가상 대화 표본(samples.py)마다 담당자 대본이 실제 서비스(InterviewService: 탐지기 가명화·저장 전 검사 포함)를 D1 흐름 끝까지 돈다:
공고에서 시작 → (팀 맥락 고정 질문) → 사례 대화 → 처음 시도한 대응 → 성공 기준·기준 답안·검증(보충 질문 최대 2번) → 확인.
담당자는 질문 낱말로 묻는 칸을 짐작해 그 칸의 사실로 답한다 (짐작이 안 되면 빠진 칸 중 첫 칸).

지표 (정답은 표본에 심은 값)
- completion        확인(competencies)까지 간 표본 비율
- reasks            담당자가 사실로 답한 칸을 모델이 다시 '빠짐'으로 본 횟수 (누적 인용 유지 — 포크 v2 판정 항목)
- leading           담당자가 말하기 전에 원인·해법 단서(표본 leading)를 질문에 넣은 횟수 (미확인 원인 유도)
- unavailable_ratio LLM 단계 중 형식·근거·가명화 검사 실패(503) 비율 — 같은 단계를 2번까지 다시 보낸다
- targeted          질문 낱말이 빠진 칸 하나를 가리킨 비율 (참고)
- 지연·토큰·비용     저장된 provenance 기준 (실패한 호출은 빠진다 — 참고)

통과 기준은 실행 전에 정했다 (CRITERIA, #263). dev 로 맞추고 test 는 사람이 정한 때 한 번만 — test 는 대화 내용을 보지 않는다.

사용 (.env 가 있는 레포 루트에서):
  uv run python -m eval.interview.run_eval --split dev --show-transcripts --save
  uv run python -m eval.interview.run_eval --split test --save
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from eval.interview.samples import Dialogue, dialogues
from task_spec.config import Settings
from task_spec.interview.errors import InterviewError, InterviewUnavailable
from task_spec.interview.service import InterviewService
from task_spec.interview.store import MemoryInterviewStore
from task_spec.llm import LLMClient
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.pseudonymize import InterviewPseudonymizer, RulePseudonymizer
from task_spec.nodes.pseudonymize.engine import Detectors
from task_spec.prompting import PromptRegistry
from task_spec.schemas import PostingProfile
from task_spec.schemas.interview_incident import IncidentFact

HERE = Path(__file__).parent
REPO = HERE.parents[1]
DEFAULT_VERSIONS = {
    "interview_incident_extended": 4,
    "interview_answer": 4,
    "interview_answer_check": 2,
}
CRITERIA: dict[str, tuple[str, float]] = {
    "completion": (">=", 0.8),
    "reasks": ("<=", 1),
    "leading": ("<=", 0),
    "unavailable_ratio": ("<=", 0.2),
}
MAX_INCIDENT_TURNS = 8
MAX_CLARIFY_ROUNDS = 2
MAX_TRAP_RETURNS = (
    2  # ④ 처음 대응 뒤 ③ 으로 돌아가 다시 답하는 횟수 상한 (#300·#307 — 복귀는 실패가 아니다)
)
MAX_RETRIES = 2
CORE: tuple[IncidentFact, ...] = ("symptom", "root_cause", "accepted_fixes", "conditions")
FIELD_HINTS: dict[IncidentFact, tuple[str, ...]] = {
    "root_cause": ("원인", "근거", "로그", "코드", "제외", "가능성", "확인한 기록", "비교"),
    "accepted_fixes": ("조치", "바꾸", "바꿨", "고치", "고쳤", "해결", "수정", "변경"),
    "conditions": ("조건", "언제", "경우", "상황", "시점", "정상인", "범위"),
    "symptom": ("증상", "화면", "기대", "실제 결과", "오류 문구", "현상", "어떻게 보"),
}
POSTING_ID = "prov-0e0a1b2c3d4e"


@dataclass
class Result:
    id: str
    tags: list[str]
    completed: bool = False
    end_stage: str = ""
    incident_turns: int = 0
    reasks: list[dict[str, Any]] = field(default_factory=list)
    leading: list[dict[str, Any]] = field(default_factory=list)
    targeted: list[bool] = field(default_factory=list)
    llm_steps: int = 0
    unavailable: int = 0
    clarify_rounds: int = 0
    trap_returns: int = 0  # ④ 를 낸 뒤 ③ 으로 돌아간 횟수 — 실패로 세지 않는다
    validation_passed: bool | None = None
    issues: list[str] = field(default_factory=list)
    failure: str = ""
    step_seconds: list[float] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=lambda: {"in": 0, "out": 0, "reasoning": 0})
    cost_usd: float = 0.0
    transcript: list[dict[str, str]] = field(default_factory=list)


class Postings:
    def __init__(self, d: Dialogue) -> None:
        self.d = d

    async def profile(self, posting_id: str) -> PostingProfile | None:
        return PostingProfile(
            posting_id=posting_id,
            ontology_version="onto-eval",
            source="provisional",
            company=self.d.company,
            position_name=self.d.title,
            sub=self.d.sub,
        )

    async def lines(self, posting_id: str) -> list[tuple[str, str]]:
        return list(self.d.jd)

    async def company(self, posting_id: str) -> str | None:
        return self.d.company


def registry(tmp: Path, versions: dict[str, int]) -> PromptRegistry:
    """평가할 판을 CURRENT 로 둔 임시 프롬프트 루트 (레포의 CURRENT 는 건드리지 않는다)."""
    root = tmp / "prompts"
    for task, version in versions.items():
        src, dst = REPO / "prompts" / task, root / task
        dst.mkdir(parents=True)
        shutil.copy(src / f"v{version}.md", dst / f"v{version}.md")
        (dst / "CURRENT").write_text(f"v{version}\n")
    return PromptRegistry(root)


def infer_field(question: str, missing: list[str]) -> tuple[IncidentFact, bool]:
    """질문이 묻는 칸 — 빠진 칸 중 낱말이 가장 많이 맞는 칸. 없으면 빠진 칸 중 첫 칸 (targeted=False)."""
    candidates = [f for f in CORE if f in missing] or [f for f in CORE]
    scores = {f: sum(h in question for h in FIELD_HINTS[f]) for f in candidates}
    best = max(candidates, key=lambda f: (scores[f], -candidates.index(f)))
    return best, scores[best] > 0


def leading_in(question: str, told: str, terms: tuple[str, ...]) -> list[str]:
    q, t = question.replace(" ", ""), told.replace(" ", "")
    return [term for term in terms if term.replace(" ", "") in q and term.replace(" ", "") not in t]


async def run_dialogue(
    d: Dialogue, *, llm: LLMClient, detectors: Detectors, versions: dict[str, int]
) -> Result:
    r = Result(id=d.id, tags=list(d.tags))
    with tempfile.TemporaryDirectory(prefix="interview-eval-") as tmp:
        service = InterviewService(
            store=MemoryInterviewStore(),
            pseudo=InterviewPseudonymizer(detectors, Path(tmp)),
            postings=Postings(d),
            llm=llm,
            prompts=registry(Path(tmp), versions),
            ontology_version="onto-eval",
        )

        async def step(call: Any, *, llm_step: bool = True) -> dict[str, Any]:
            for attempt in range(MAX_RETRIES + 1):
                started = time.monotonic()
                try:
                    view = await call()
                except InterviewUnavailable:
                    r.unavailable += 1 if llm_step else 0
                    r.llm_steps += 1 if llm_step else 0
                    if attempt == MAX_RETRIES:
                        raise
                    continue
                if llm_step:
                    r.llm_steps += 1
                    r.step_seconds.append(round(time.monotonic() - started, 1))
                return view
            raise AssertionError("unreachable")

        told = " ".join(text for _, text in d.jd)
        answered: set[str] = set()
        said_unknown = False
        try:
            view = await step(lambda: service.create(POSTING_ID), llm_step=False)
            iid = view["interview_id"]
            for answer in d.scope_answers:
                if view["stage"] != "scope_interview":
                    break
                view = await step(
                    lambda a=answer, v=view: service.message(iid, text=a, revision=v["revision"]),
                    llm_step=False,
                )
            told += " " + d.opening
            view = await step(
                lambda v=view: service.message(iid, text=d.opening, revision=v["revision"])
            )
            # ③ 사례 대화 → ④ 처음 대응. ④ 를 낸 뒤 다른 칸이 빠졌으면 서비스가 ③ 으로 돌려보낸다 (#300·#307) — 실패가 아니라
            # 빠진 칸에 다시 답하고 ④ 를 다시 낸다 (복귀는 MAX_TRAP_RETURNS 번까지, #308 할 일)
            while True:
                while (
                    view["stage"] == "incident_interview" and r.incident_turns < MAX_INCIDENT_TURNS
                ):
                    missing = list(view["incident_missing"])
                    r.reasks += [
                        {"turn": r.incident_turns, "field": f} for f in missing if f in answered
                    ]
                    question = view["incident_messages"][-1]["text"]
                    target, hit = infer_field(question, missing)
                    r.targeted.append(hit)
                    if target == d.unknown_first and not said_unknown:
                        reply, said_unknown = d.unknown_reply, True
                    else:
                        reply = d.facts[target]
                        answered.add(target)
                    told += " " + reply
                    r.incident_turns += 1
                    view = await step(
                        lambda t=reply, v=view: service.message(iid, text=t, revision=v["revision"])
                    )
                if view["stage"] == "incident_interview":
                    r.reasks += [
                        {"turn": r.incident_turns, "field": f}
                        for f in view["incident_missing"]
                        if f in answered
                    ]
                    raise RuntimeError(f"사례 대화가 {MAX_INCIDENT_TURNS}턴 안에 끝나지 않음")
                if view["stage"] != "trap_interview":
                    break
                # ④ 처음 대응
                if d.trap is None:
                    view = await step(
                        lambda v=view: service.trap(
                            iid,
                            revision=v["revision"],
                            attempted_fix="",
                            observed_result="",
                            why_failed="",
                            unknown=True,
                        )
                    )
                else:
                    trap = d.trap
                    view = await step(
                        lambda v=view, tr=trap: service.trap(
                            iid,
                            revision=v["revision"],
                            attempted_fix=tr.attempted_fix,
                            observed_result=tr.observed_result,
                            why_failed=tr.why_failed,
                            unknown=False,
                        )
                    )
                if view["stage"] != "incident_interview" or r.trap_returns >= MAX_TRAP_RETURNS:
                    break
                r.trap_returns += 1
            while view["stage"] == "success_interview" and r.clarify_rounds <= MAX_CLARIFY_ROUNDS:
                replies = {q["question_id"]: d.clarify for q in view["answer_questions"]}
                r.clarify_rounds += 1 if replies else 0
                view = await step(
                    lambda v=view, c=replies: service.success(
                        iid, revision=v["revision"], clarification_answers=c, **d.success
                    )
                )
                if view["stage"] == "success_interview" and not view["answer_questions"]:
                    break
            record = view.get("answer_validation") or {}
            r.validation_passed = record.get("passed")
            r.issues = list(record.get("issues", []))
            if view["stage"] == "incident_review" and record.get("passed"):
                view = await step(
                    lambda v=view: service.confirm(
                        iid, revision=v["revision"], incident=_incident(v)
                    ),
                    llm_step=False,
                )
            r.completed = view["stage"] == "competencies"
            if not r.completed and not r.failure:
                r.failure = f"끝 단계 {view['stage']} (검증 통과={r.validation_passed}, 보충 질문 {r.clarify_rounds}번)"
        except (InterviewError, RuntimeError) as exc:
            r.failure = f"{type(exc).__name__}: {getattr(exc, 'message', str(exc))}"
            view = await service.get(iid) if "iid" in locals() else {}
        r.end_stage = view.get("stage", "") if view else ""
        _score_leading(r, view, d)
        _usage(r, view)
        r.transcript = [
            {"role": m["role"], "text": m["text"]}
            for key in ("scope_messages", "incident_messages")
            for m in (view.get(key) or [])
        ]
    return r


def _incident(view: dict[str, Any]) -> Any:
    from task_spec.schemas import PublicIncident

    return PublicIncident.model_validate(view["incident_draft"])


def _score_leading(r: Result, view: dict[str, Any] | None, d: Dialogue) -> None:
    """사례 대화의 모델 질문마다, 그때까지 담당자가 말하지 않은 단서가 있으면 유도."""
    if not view:
        return
    told = " ".join(text for _, text in d.jd)
    turn = 0
    for i, m in enumerate(view.get("incident_messages") or []):
        if m["role"] == "user":
            told += " " + m["text"]
            turn += 1
        elif i > 0:  # 첫 메시지는 서버의 고정 시작 질문
            for term in leading_in(m["text"], told, d.leading):
                r.leading.append({"turn": turn, "term": term, "question": m["text"]})


def _usage(r: Result, view: dict[str, Any] | None) -> None:
    if not view:
        return
    provs = [
        *view.get("incident_provenance", []),
        *view.get("answer_provenance", []),
        *view.get("answer_validation_provenance", []),
    ]
    for p in provs:
        tokens = p.get("tokens") or {}
        r.tokens["in"] += int(
            tokens.get("in", tokens.get("in_", 0))
        )  # provenance 는 칸 이름(in_)으로 저장된다
        r.tokens["out"] += int(tokens.get("out", 0))
        r.tokens["reasoning"] += int(tokens.get("reasoning", 0))
        r.cost_usd += float(p.get("cost_usd") or 0.0)


def summarize(results: list[Result]) -> dict[str, Any]:
    n = len(results)
    steps = sum(r.llm_steps for r in results)
    seconds = [s for r in results for s in r.step_seconds]
    targeted = [t for r in results for t in r.targeted]
    metrics: dict[str, Any] = {
        "samples": n,
        "completion": round(sum(r.completed for r in results) / n, 3) if n else 0.0,
        "reasks": sum(len(r.reasks) for r in results),
        "leading": sum(len(r.leading) for r in results),
        "unavailable_ratio": round(sum(r.unavailable for r in results) / steps, 3)
        if steps
        else 0.0,
        "targeted": round(sum(targeted) / len(targeted), 3) if targeted else None,
        "incident_turns_mean": round(statistics.mean(r.incident_turns for r in results), 2)
        if n
        else 0,
        "clarify_rounds": sum(r.clarify_rounds for r in results),
        "trap_returns": sum(r.trap_returns for r in results),
        "step_seconds_mean": round(statistics.mean(seconds), 1) if seconds else None,
        "step_seconds_p90": round(sorted(seconds)[int(0.9 * (len(seconds) - 1))], 1)
        if seconds
        else None,
        "tokens": {k: sum(r.tokens[k] for r in results) for k in ("in", "out", "reasoning")},
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
    }
    verdicts = {}
    for key, (op, bound) in CRITERIA.items():
        value = metrics[key]
        verdicts[key] = bool(value >= bound) if op == ">=" else bool(value <= bound)
    metrics["criteria"] = {
        k: {"rule": f"{op} {b}", "pass": verdicts[k]} for k, (op, b) in CRITERIA.items()
    }
    metrics["passed"] = all(verdicts.values())
    return metrics


def render(
    split: str,
    versions: dict[str, int],
    model: str,
    summary: dict[str, Any],
    results: list[Result],
    show: bool,
) -> str:
    v = ", ".join(f"{t}@{n}" for t, n in versions.items())
    lines = [
        f"# 사례 인터뷰 프롬프트 평가 — {split} ({date.today().isoformat()})",
        "",
        f"- 프롬프트: {v}",
        f"- 모델: {model} (프롬프트 머리말)",
        f"- 표본 {summary['samples']}건 · 판정 **{'통과' if summary['passed'] else '미달'}**",
        "",
        "| 기준 | 값 | 규칙 | 결과 |",
        "|---|---|---|---|",
    ]
    for key, c in summary["criteria"].items():
        lines.append(
            f"| {key} | {summary[key]} | {c['rule']} | {'통과' if c['pass'] else '미달'} |"
        )
    lines += [
        "",
        f"참고: targeted {summary['targeted']} · 사례 대화 평균 {summary['incident_turns_mean']}턴 · 보충 질문 {summary['clarify_rounds']}번 · ④→③ 복귀 {summary['trap_returns']}번 · "
        f"LLM 단계 평균 {summary['step_seconds_mean']}초 (p90 {summary['step_seconds_p90']}초) · 토큰 {summary['tokens']} · 비용 ${summary['cost_usd']}",
        "",
        "| 표본 | 완성 | 끝 단계 | 대화 턴 | 재질문 | 유도 | 503 | 보충 질문 | 검증 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.id} | {'O' if r.completed else 'X'} | {r.end_stage} | {r.incident_turns} | {len(r.reasks)} | {len(r.leading)} | "
            f"{r.unavailable}/{r.llm_steps} | {r.clarify_rounds} | {r.validation_passed} |"
        )
    if show:
        for r in results:
            lines += ["", f"## {r.id} ({', '.join(r.tags) or '-'})", ""]
            if r.failure:
                lines.append(f"- 실패: {r.failure}")
            for x in r.reasks:
                lines.append(f"- 재질문: {x['turn']}턴 {x['field']}")
            for x in r.leading:
                lines.append(f"- 유도: {x['turn']}턴 '{x['term']}' — {x['question']}")
            if r.issues:
                lines.append("- 검증 문제: " + " / ".join(r.issues))
            lines += ["", "```"] + [f"{m['role']}: {m['text']}" for m in r.transcript] + ["```"]
    return "\n".join(lines) + "\n"


async def evaluate(
    split: str, versions: dict[str, int], concurrency: int
) -> tuple[list[Result], str]:
    llm = build_llm_client(Settings())  # type: ignore[call-arg]
    with tempfile.TemporaryDirectory() as tmp:
        rule = RulePseudonymizer(Path(tmp))
        await rule.warm_up()
        detectors = rule.detectors
    model = (
        registry(Path(tempfile.mkdtemp()), versions).get("interview_incident_extended").meta.model
    )
    sem = asyncio.Semaphore(concurrency)

    async def one(d: Dialogue) -> Result:
        async with sem:
            return await run_dialogue(d, llm=llm, detectors=detectors, versions=versions)

    results = await asyncio.gather(*(one(d) for d in dialogues(split)))
    return list(results), model


def parse_versions(text: str | None) -> dict[str, int]:
    versions = dict(DEFAULT_VERSIONS)
    alias = {
        "incident": "interview_incident_extended",
        "answer": "interview_answer",
        "check": "interview_answer_check",
    }
    for part in (text or "").split(","):
        if part.strip():
            key, _, value = part.partition("=")
            versions[alias.get(key.strip(), key.strip())] = int(value)
    return versions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    ap.add_argument(
        "--versions", default=None, help="예: incident=4,answer=4,check=2 (기본: 포크에서 옮긴 판)"
    )
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--show-transcripts", action="store_true")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    if a.split == "test" and a.show_transcripts:
        raise SystemExit("test 세트의 대화 내용은 보지 않는다 (숨겨 둔 세트 유지)")
    versions = parse_versions(a.versions)
    results, model = asyncio.run(evaluate(a.split, versions, a.concurrency))
    summary = summarize(results)
    md = render(a.split, versions, model, summary, results, a.show_transcripts)
    print(md)
    if a.save:
        tag = "-".join(f"{t.removeprefix('interview_')}{n}" for t, n in versions.items())
        base = HERE / "reports" / f"{date.today().isoformat()}_{a.split}_{tag}"
        base.parent.mkdir(exist_ok=True)
        stem, n = base, 1
        while stem.with_suffix(".md").exists():  # 같은 날 다시 돌려도 앞 기록을 덮지 않는다
            n += 1
            stem = base.with_name(f"{base.name}_run{n}")
        stem.with_suffix(".md").write_text(md, encoding="utf-8")
        rows = [asdict(r) for r in results]
        if a.split == "test":
            for row in rows:
                for key in ("transcript", "reasks", "leading", "issues", "failure"):
                    row.pop(key)
        stem.with_suffix(".json").write_text(
            json.dumps(
                {
                    "split": a.split,
                    "versions": versions,
                    "model": model,
                    "summary": summary,
                    "results": rows,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
