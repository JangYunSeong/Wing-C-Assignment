"""D2 새 프롬프트 평가 (#266) — interview_feature_seed · interview_targeted_answer. 실제 LLM 호출 (비용 발생, 입력은 전부 가상).

1) 기능형 재료: 표본 사례(가상 이름 포함 원문)를 실제 탐지기로 가명화해 확인된 사례·기준 답안을 만들고, 서비스와 같은 함수
   (`prepare_coding_incident` — 템플릿 실패 사유·실행 조건 대조 포함)로 재료를 만든다.
2) 답할 범위 초안: 서비스와 같은 입력(가명화한 질문·짚은 조건·과제 글)으로 초안을 받고 저장 전 검사(derived)를 거친다.
표본마다 한 번씩 부른다 (첫 응답의 질 — 서비스는 실패하면 담당자가 다시 누르고, 초안은 자료 id·기준 답안 복사면 한 번 더 받는다).

지표 (정답은 표본에 심은 값, samples.py)
- seed_valid         재료가 서비스 검사를 통과한 비율 (형식·템플릿 실패 사유·실행 조건)
- seed_reason_hit    심은 실패 사유가 하나 이상 들어간 비율 (통과한 재료 중)
- seed_concept_hit   숨은 요구에 심은 개념이 들어간 비율
- seed_harness_ok    성능 사례가 아니면 실행 조건(harness)을 비웠는가 (프롬프트 규칙)
- seed_need_leak     업무 목적(business_need)에 원인·해법 낱말이 나온 수 — 지원자에게 보이는 상황의 재료다
- seed_designs_ok    인정 설계가 2개 이상인 비율
- seed_blocked       회사 사례 등록을 막는 연락처·링크가 생긴 수
- answer_valid       초안이 형식·저장 전 검사를 통과한 비율
- answer_solution_leak  해법 낱말이 초안에 나온 수
- answer_artifact_id    내부 자료 id(a1…)를 쓴 수 (서비스는 다시 받는다)
- answer_invented_numbers  입력에 없는 숫자를 쓴 수
- answer_too_long     300자를 넘은 수 (프롬프트는 250자 이내)
- answer_pointer_hit  관련 자료 제목을 가리킨 비율

통과 기준은 실행 전에 정했다 (CRITERIA). dev 로 맞추고 test 는 사람이 정한 때 한 번만 — test 는 출력·실패 내용을 보지 않는다.

사용 (.env 를 Settings 가 읽는다 — 셸로 열지 않는다):
  uv run python -m eval.interview_task.run_eval --split dev --show --save --env-file /path/to/.env
  uv run python -m eval.interview_task.run_eval --split test --save --env-file /path/to/.env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import statistics
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from eval.interview_task.samples import AnswerCase, SeedCase, Split, answer_cases, seed_cases
from task_spec.config import Settings
from task_spec.interview.approval import blocked_findings
from task_spec.interview.task import (
    FEATURE_SEED_PROMPT,
    TARGETED_PROMPT,
    coding_template,
    mentions_artifact_id,
    prepare_coding_incident,
)
from task_spec.interview.text import numbers
from task_spec.llm import LLMClient
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.pseudonymize import InterviewPseudonymizer, RulePseudonymizer
from task_spec.nodes.pseudonymize.engine import Detectors, RunMapping
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import FeatureSeed, Provenance, PublicIncident
from task_spec.schemas.interview_answer import TargetedAnswerDraftInput, TargetedAnswerDraftOutput
from task_spec.schemas.interview_case import KnownIncidentCase
from task_spec.spec.company_incidents import new_incident_id
from task_spec.spec.incidents import incident_block, known_cases_block

HERE = Path(__file__).parent
REPO = HERE.parents[1]
DEFAULT_VERSIONS = {FEATURE_SEED_PROMPT: 1, TARGETED_PROMPT: 1}
CRITERIA: dict[str, tuple[str, float]] = {
    "seed_valid": (">=", 0.8),
    "seed_reason_hit": (">=", 0.8),
    "seed_concept_hit": (">=", 0.8),
    "seed_harness_ok": (">=", 0.8),
    "seed_need_leak": ("<=", 0),
    "seed_designs_ok": (">=", 0.8),
    "seed_blocked": ("<=", 0),
    "answer_valid": (">=", 0.9),
    "answer_solution_leak": ("<=", 0),
    "answer_artifact_id": ("<=", 1),
    "answer_invented_numbers": ("<=", 1),
    "answer_too_long": ("<=", 1),
    "answer_pointer_hit": (">=", 0.6),
}
ANSWER_MAX = 300
_ARTIFACT_ID = re.compile(r"(?<![A-Za-z0-9])[aA][0-9]+(?![A-Za-z0-9])")


@dataclass
class SeedResult:
    id: str
    valid: bool = False
    failure: str = ""
    reason_hit: bool = False
    concept_hit: bool = False
    harness_ok: bool = False
    need_leak: list[str] = field(default_factory=list)
    designs: int = 0
    blocked: int = 0
    seconds: float = 0.0
    tokens: dict[str, int] = field(default_factory=lambda: {"in": 0, "out": 0, "reasoning": 0})
    cost_usd: float = 0.0
    output: dict[str, Any] | None = None


@dataclass
class AnswerResult:
    id: str
    valid: bool = False
    failure: str = ""
    solution_leak: list[str] = field(default_factory=list)
    artifact_id: bool = False
    invented_numbers: list[str] = field(default_factory=list)
    length: int = 0
    pointer_hit: bool = False
    seconds: float = 0.0
    tokens: dict[str, int] = field(default_factory=lambda: {"in": 0, "out": 0, "reasoning": 0})
    cost_usd: float = 0.0
    answer: str = ""


# ---------------------------------------------------------------- 채점 (LLM 없이 — 자기 검증 테스트가 부른다)
def score_seed(case: SeedCase, seed: FeatureSeed, incident: PublicIncident) -> SeedResult:
    r = SeedResult(id=case.id, valid=True)
    r.reason_hit = bool(set(seed.reasons) & case.reasons)
    r.concept_hit = any(term in seed.hidden_requirement for term in case.concept)
    r.harness_ok = case.perf or not seed.harness
    r.need_leak = [t for t in case.need_forbidden if t.lower() in seed.business_need.lower()]
    r.designs = len(seed.accepted_designs)
    r.blocked = sum(f.count for f in blocked_findings(incident))
    r.output = seed.model_dump(mode="json")
    return r


def score_answer(case: AnswerCase, answer: str, inputs: str) -> AnswerResult:
    r = AnswerResult(id=case.id, valid=True, answer=answer, length=len(answer))
    low = answer.lower()
    r.solution_leak = [t for t in case.solution if t.lower() in low]
    r.artifact_id = mentions_artifact_id(answer)
    # 자료 id(a2)의 숫자는 지어낸 수치가 아니다 — artifact_id 로 따로 센다
    r.invented_numbers = sorted(numbers(_ARTIFACT_ID.sub("", answer)) - numbers(inputs))
    r.pointer_hit = any(p in answer for p in case.pointer)
    return r


def _usage(r: SeedResult | AnswerResult, prov: Provenance) -> None:
    r.tokens["in"] += prov.tokens.in_
    r.tokens["out"] += prov.tokens.out
    r.tokens["reasoning"] += int(getattr(prov.tokens, "reasoning", 0) or 0)
    r.cost_usd += float(prov.cost_usd or 0.0)


# ---------------------------------------------------------------- 실행
def registry(tmp: Path, versions: dict[str, int]) -> PromptRegistry:
    """평가할 판을 CURRENT 로 둔 임시 프롬프트 루트 (레포의 CURRENT 는 건드리지 않는다)."""
    root = tmp / "prompts"
    for task, version in versions.items():
        src, dst = REPO / "prompts" / task, root / task
        dst.mkdir(parents=True)
        shutil.copy(src / f"v{version}.md", dst / f"v{version}.md")
        (dst / "CURRENT").write_text(f"v{version}\n")
    return PromptRegistry(root)


def _new_mapping(pseudo: InterviewPseudonymizer, company: str) -> RunMapping:
    return pseudo.new_mapping(f"int-{uuid.uuid4().hex}", company)


def _case_incident(
    case: SeedCase, pseudo: InterviewPseudonymizer, mapping: RunMapping
) -> PublicIncident:
    def p(text: str) -> str:
        return str(pseudo.pseudonymize(text, mapping))

    return PublicIncident(
        incident_id=new_incident_id(case.sub),
        sub=case.sub,
        title=p(case.title),
        org="",
        url="",
        published="2026-11-19",
        source_lang="ko",
        symptom=p(case.symptom),
        naive_fix=p(case.naive_fix),
        root_cause=p(case.root_cause),
        why_naive_fails=p(case.why_naive_fails),
        key_insight=p(case.key_insight),
        deep_structure=p(case.deep_structure),
        surface=list(case.surface),
        tech=list(case.tech),
        verified=False,
        accepted_fixes=[p(x) for x in case.accepted_fixes],
        known_incident_cases=[
            KnownIncidentCase(
                case_id=str(uuid.uuid4()),
                interview_text=p(case.interview_text),
                answer_text=p(case.answer_text),
                is_trap=False,
            ),
            KnownIncidentCase(
                case_id=str(uuid.uuid4()),
                interview_text=p(case.trap_text),
                answer_text=p(case.trap_answer_text),
                is_trap=True,
            ),
        ],
    )


async def run_seed(
    case: SeedCase, *, llm: LLMClient, prompts: PromptRegistry, pseudo: InterviewPseudonymizer
) -> SeedResult:
    mapping = _new_mapping(pseudo, case.company)
    incident = _case_incident(case, pseudo, mapping)
    text = pseudo.stored(
        "\n".join([incident_block(incident), known_cases_block(incident)]), mapping
    )
    started = time.monotonic()
    try:
        coding, provs, _fit = await prepare_coding_incident(
            incident,
            coding_template(case.sub),
            interview=text,
            llm=llm,
            prompts=prompts,
            ontology_version="onto-eval",
        )
    except Exception as exc:  # noqa: BLE001 — 실패 종류를 센다 (형식·템플릿 대조·공급자)
        return SeedResult(
            id=case.id,
            failure=f"{type(exc).__name__}: {str(exc)[:160]}",
            seconds=round(time.monotonic() - started, 1),
        )
    assert coding.feature is not None
    r = score_seed(case, coding.feature, coding)
    r.seconds = round(time.monotonic() - started, 1)
    for prov in provs:
        _usage(r, prov)
    return r


async def run_answer(
    case: AnswerCase, *, llm: LLMClient, prompts: PromptRegistry, pseudo: InterviewPseudonymizer
) -> AnswerResult:
    mapping = _new_mapping(pseudo, "가상테크")
    question = str(pseudo.pseudonymize(case.question, mapping))
    condition = str(pseudo.pseudonymize(case.hidden_condition, mapping))
    task_json = json.dumps(case.task, ensure_ascii=False)
    started = time.monotonic()
    try:
        result = await llm.call(
            assemble(
                prompts.get(TARGETED_PROMPT),
                TargetedAnswerDraftInput(
                    question=pseudo.stored(question, mapping),
                    hidden_condition=pseudo.stored(condition, mapping),
                    task_json=pseudo.stored(task_json, mapping),
                ),
                ontology_version="onto-eval",
            ),
            TargetedAnswerDraftOutput,
        )
        answer = pseudo.derived(result.parsed.answer, mapping)
    except Exception as exc:  # noqa: BLE001
        return AnswerResult(
            id=case.id,
            failure=f"{type(exc).__name__}: {str(exc)[:160]}",
            seconds=round(time.monotonic() - started, 1),
        )
    r = score_answer(case, answer, "\n".join([question, condition, task_json]))
    r.seconds = round(time.monotonic() - started, 1)
    _usage(r, result.provenance)
    return r


def _ratio(values: list[bool]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def summarize(seeds: list[SeedResult], answers: list[AnswerResult]) -> dict[str, Any]:
    ok_seeds = [r for r in seeds if r.valid]
    ok_answers = [r for r in answers if r.valid]
    both: list[SeedResult | AnswerResult] = [*seeds, *answers]
    seconds = [r.seconds for r in both if r.valid]
    m: dict[str, Any] = {
        "seed_samples": len(seeds),
        "answer_samples": len(answers),
        "seed_valid": _ratio([r.valid for r in seeds]),
        "seed_reason_hit": _ratio([r.reason_hit for r in ok_seeds]),
        "seed_concept_hit": _ratio([r.concept_hit for r in ok_seeds]),
        "seed_harness_ok": _ratio([r.harness_ok for r in ok_seeds]),
        "seed_need_leak": sum(len(r.need_leak) for r in ok_seeds),
        "seed_designs_ok": _ratio([r.designs >= 2 for r in ok_seeds]),
        "seed_blocked": sum(r.blocked for r in ok_seeds),
        "answer_valid": _ratio([r.valid for r in answers]),
        "answer_solution_leak": sum(len(r.solution_leak) for r in ok_answers),
        "answer_artifact_id": sum(r.artifact_id for r in ok_answers),
        "answer_invented_numbers": sum(bool(r.invented_numbers) for r in ok_answers),
        "answer_too_long": sum(r.length > ANSWER_MAX for r in ok_answers),
        "answer_pointer_hit": _ratio([r.pointer_hit for r in ok_answers]),
        "seconds_mean": round(statistics.mean(seconds), 1) if seconds else None,
        "tokens": {k: sum(r.tokens[k] for r in both) for k in ("in", "out", "reasoning")},
        "cost_usd": round(sum(r.cost_usd for r in both), 4),
    }
    verdicts: dict[str, bool] = {}
    for key, (op, bound) in CRITERIA.items():
        if (key.startswith("seed_") and not seeds) or (key.startswith("answer_") and not answers):
            continue  # 그 부분을 돌리지 않음
        value = m[key]
        verdicts[key] = bool(value >= bound) if op == ">=" else bool(value <= bound)
    m["criteria"] = {
        k: {"rule": f"{CRITERIA[k][0]} {CRITERIA[k][1]}", "pass": v} for k, v in verdicts.items()
    }
    m["passed"] = bool(verdicts) and all(verdicts.values())
    return m


def render(
    split: str,
    versions: dict[str, int],
    summary: dict[str, Any],
    seeds: list[SeedResult],
    answers: list[AnswerResult],
    show: bool,
) -> str:
    v = ", ".join(f"{t}@{n}" for t, n in versions.items())
    lines = [
        f"# D2 새 프롬프트 평가 — {split} ({date.today().isoformat()})",
        "",
        f"- 프롬프트: {v}",
        f"- 표본: 기능형 재료 {summary['seed_samples']}건 · 답할 범위 초안 {summary['answer_samples']}건 · "
        f"판정 **{'통과' if summary['passed'] else '미달'}**",
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
        f"참고: 호출 평균 {summary['seconds_mean']}초 · 토큰 {summary['tokens']} · 비용 ${summary['cost_usd']}",
        "",
    ]
    if seeds:
        lines += [
            "| 재료 표본 | 통과 | 사유 | 개념 | harness | 목적 누설 | 인정 설계 | 등록 막힘 | 초 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in seeds:
            lines.append(
                f"| {r.id} | {'O' if r.valid else 'X'} | {'O' if r.reason_hit else 'X'} | {'O' if r.concept_hit else 'X'} | "
                f"{'O' if r.harness_ok else 'X'} | {len(r.need_leak)} | {r.designs} | {r.blocked} | {r.seconds} |"
            )
        lines.append("")
    if answers:
        lines += [
            "| 초안 표본 | 통과 | 해법 누설 | 자료 id | 지어낸 수치 | 길이 | 자료 가리킴 | 초 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for ar in answers:
            lines.append(
                f"| {ar.id} | {'O' if ar.valid else 'X'} | {len(ar.solution_leak)} | {'O' if ar.artifact_id else '-'} | "
                f"{len(ar.invented_numbers)} | {ar.length} | {'O' if ar.pointer_hit else 'X'} | {ar.seconds} |"
            )
    if show:
        for r in seeds:
            lines += ["", f"## {r.id}", ""]
            if r.failure:
                lines.append(f"- 실패: {r.failure}")
            if r.need_leak:
                lines.append(f"- 업무 목적 누설: {r.need_leak}")
            if r.output:
                o = r.output
                lines += [
                    f"- reasons {o.get('reasons')} · harness {o.get('harness')}",
                    f"- business_need: {o.get('business_need')}",
                    f"- hidden_requirement: {o.get('hidden_requirement')}",
                    f"- accepted_designs: {o.get('accepted_designs')}",
                ]
        for ar in answers:
            lines += ["", f"## {ar.id}", ""]
            if ar.failure:
                lines.append(f"- 실패: {ar.failure}")
            if ar.solution_leak or ar.invented_numbers:
                lines.append(f"- 해법 누설 {ar.solution_leak} · 지어낸 수치 {ar.invented_numbers}")
            lines.append(f"- 초안: {ar.answer}")
    return "\n".join(lines) + "\n"


async def evaluate(
    split: Split, versions: dict[str, int], part: str, concurrency: int, env_file: str
) -> tuple[list[SeedResult], list[AnswerResult]]:
    llm = build_llm_client(Settings(_env_file=env_file))  # type: ignore[call-arg]
    with tempfile.TemporaryDirectory() as tmp:
        rule = RulePseudonymizer(Path(tmp))
        await rule.warm_up()
        detectors: Detectors = rule.detectors
    with tempfile.TemporaryDirectory(prefix="interview-task-eval-") as tmp:
        prompts = registry(Path(tmp), versions)
        pseudo = InterviewPseudonymizer(detectors, Path(tmp) / "mappings")
        sem = asyncio.Semaphore(concurrency)

        async def seed(c: SeedCase) -> SeedResult:
            async with sem:
                return await run_seed(c, llm=llm, prompts=prompts, pseudo=pseudo)

        async def answer(c: AnswerCase) -> AnswerResult:
            async with sem:
                return await run_answer(c, llm=llm, prompts=prompts, pseudo=pseudo)

        seeds = (
            await asyncio.gather(*(seed(c) for c in seed_cases(split)))
            if part in ("seed", "all")
            else []
        )
        answers = (
            await asyncio.gather(*(answer(c) for c in answer_cases(split)))
            if part in ("answer", "all")
            else []
        )
    return list(seeds), list(answers)


def parse_versions(text: str | None) -> dict[str, int]:
    versions = dict(DEFAULT_VERSIONS)
    alias = {"seed": FEATURE_SEED_PROMPT, "answer": TARGETED_PROMPT}
    for part in (text or "").split(","):
        if part.strip():
            key, _, value = part.partition("=")
            versions[alias.get(key.strip(), key.strip())] = int(value)
    return versions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], required=True)
    ap.add_argument("--part", choices=["seed", "answer", "all"], default="all")
    ap.add_argument("--versions", default=None, help="예: seed=1,answer=1")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--env-file", default=".env", help="LLM 설정을 읽을 .env (Settings 가 읽는다)")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    if a.split == "test" and a.show:
        raise SystemExit("test 세트의 출력은 보지 않는다 (숨겨 둔 세트 유지)")
    versions = parse_versions(a.versions)
    seeds, answers = asyncio.run(evaluate(a.split, versions, a.part, a.concurrency, a.env_file))
    summary = summarize(seeds, answers)
    md = render(a.split, versions, summary, seeds, answers, a.show)
    print(md)
    if a.save:
        tag = "-".join(f"{t.removeprefix('interview_')}{n}" for t, n in versions.items())
        base = HERE / "reports" / f"{date.today().isoformat()}_{a.split}_{a.part}_{tag}"
        base.parent.mkdir(exist_ok=True)
        stem, n = base, 1
        while stem.with_suffix(".md").exists():  # 같은 날 다시 돌려도 앞 기록을 덮지 않는다
            n += 1
            stem = base.with_name(f"{base.name}_run{n}")
        stem.with_suffix(".md").write_text(md, encoding="utf-8")
        seed_rows = [asdict(r) for r in seeds]
        answer_rows = [asdict(r) for r in answers]
        if a.split == "test":  # 숨김 세트 — 수치만
            for row in seed_rows:
                for key in ("failure", "need_leak", "output"):
                    row.pop(key)
            for row in answer_rows:
                for key in ("failure", "solution_leak", "invented_numbers", "answer"):
                    row.pop(key)
        stem.with_suffix(".json").write_text(
            json.dumps(
                {
                    "split": a.split,
                    "part": a.part,
                    "versions": versions,
                    "summary": summary,
                    "seeds": seed_rows,
                    "answers": answer_rows,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
