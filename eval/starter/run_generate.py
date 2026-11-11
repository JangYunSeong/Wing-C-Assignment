"""생성기 4종 스모크 — 배송 감사 손 예시 명세 + plan_contract 스모크 계약으로 시작 코드·숨은 테스트·참조 구현을 실제로 만든다
(docs/design/spec-to-code.md §11 5단계).

입력: 손 예시 README·진행자 자료(git 밖 data/feature_starters/delivery-audit, `run_plan_contract.build_task`), 레포 재료(sw.backend),
plan_contract 스모크가 만든 계약·HiddenPlan(json, git 밖). 가상 과제 — 지원자 데이터 아님.
`--build`: 개발 서버 Gradle 로 템플릿에 조립해 돌린다 — 시작 코드(기존 테스트), 참조 구현마다 숨은 테스트(하네스 + 케이스 + 조절값).
네트워크 없는 격리 실행·반복은 verifier(#55, 7단계) 몫이고 여기서는 첫 신호만 본다.

출력: data/eval/build/<날짜>-generate-<tag>/ (overlays/<kind>/…, README.md, report.json, builds/) — git 밖.

사용:
  uv run python -m eval.starter.run_generate --model openai:gpt-4.1 --openai-key-env OPENAI_PREMIUM_API_KEY --budget-usd 0.5 --build
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

from eval.spec.run_feature import ModelOverride, make_client
from eval.starter.gradle_verify import GRADLE, GradleVerifier, assemble, gradle_env
from eval.starter.gradle_verify import write_overlay as _write
from eval.starter.run_plan_contract import EXAMPLE, JAVA, build_task
from task_spec.nodes.starter.build import build_and_repair
from task_spec.nodes.starter.generate import (
    BuildInputs,
    GenResult,
    gen_accepted,
    gen_hidden,
    gen_naive,
    gen_starter,
)
from task_spec.prompting import PromptRegistry
from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.spec.feature import _design
from task_spec.spec.incidents import load_incidents
from task_spec.starter.contract import SpecView
from task_spec.starter.render import render_readme
from task_spec.starter.static_check import Overlay, static_check
from task_spec.starter.templates import Template, load_template

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "eval" / "build"
VERSION = "smoke"


REJECTED_MAX_FILES = 200  # 버린 생성 출력을 남길 때 상한 — 파일 수 · 파일 하나 글자 수
REJECTED_MAX_CHARS = 200_000


def write_rejected(rejected: list[Any], root: Path) -> list[str]:
    """채택하지 않은 생성 출력(문제가 남은 답)을 root/<라벨>-<n>/ 에 — 원인 확인용, data/ 아래(git 밖). 남긴 폴더 이름 목록.
    2026-11-11 재측정 gen-item: 숨은 테스트 재생성이 '케이스 id 가 여러 파일에 나옴'으로 실패했는데 그 출력이 남지 않아 원인을 코드로만 짐작했다."""
    names: list[str] = []
    left = REJECTED_MAX_FILES
    for n, o in enumerate(rejected, 1):
        if left <= 0:
            break
        name = f"{o.label or o.kind}-{n}"
        files = [f for f in o.files if len(f.content) <= REJECTED_MAX_CHARS][:left]
        left -= len(files)
        _write(type(o)(kind=o.kind, files=tuple(files), label=o.label), root / name)
        names.append(name)
    return names


def usage_by_prompt(provenance: list[Any]) -> dict[str, dict[str, Any]]:
    """프롬프트별 호출·재시도·토큰(입력·출력·그중 추론)·비용 — 출력 상한에 어디서 걸리는지 리포트에서 바로 보게 (2026-11-09:
    빈 응답이 본문이 아니라 추론 토큰 폭주였음을 따로 재야 알았다). 실패한 호출도 provenance 가 있으면 들어간다."""
    out: dict[str, dict[str, Any]] = {}
    for p in provenance:
        row = out.setdefault(
            f"{p.prompt} {p.model}",
            {
                "calls": 0,
                "retries": 0,
                "in": 0,
                "out": 0,
                "reasoning": 0,
                "max_out": 0,
                "cost_usd": 0.0,
            },
        )
        row["calls"] += 1
        row["retries"] += p.retries
        row["in"] += p.tokens.in_
        row["out"] += p.tokens.out
        row["reasoning"] += p.tokens.reasoning
        row["max_out"] = max(row["max_out"], p.tokens.out)
        row["cost_usd"] = round(row["cost_usd"] + (p.cost_usd or 0.0), 4)
    return out


class PinnedVersions:
    """지정한 프롬프트만 버전 고정 (CURRENT 는 평가 뒤에 바꾼다)."""

    def __init__(self, base: PromptRegistry, pins: dict[str, int]) -> None:
        self.base, self.pins = base, pins

    def get(self, task: str, version: int | None = None) -> Any:
        return self.base.get(task, version or self.pins.get(task))


async def run_starter_only(a: Any, b: BuildInputs, kw: dict[str, Any], template: Template) -> None:
    """시작 코드만 a.starter_only 번 — 생성(정적 검사 포함) → 개발 서버 Gradle 로 기존 테스트."""
    out = OUT / f"{date.today()}-starter{('-' + a.tag) if a.tag else ''}"
    if out.exists():
        shutil.rmtree(out)
    verifier = GradleVerifier(template, out / "builds")
    rows: list[dict[str, Any]] = []
    for n in range(1, a.starter_only + 1):
        res = await gen_starter(b, **kw)
        row = _row(res) | {"run": n}
        if res.overlay is not None:
            _write(res.overlay, out / f"starter-{n}")
            verifier.rounds = n
            run = await asyncio.to_thread(
                verifier._target, "starter", "starter", (res.overlay,), None
            )
            row["build"] = _run_row(run) | {
                "failures": [t.model_dump() for t in run.tests if t.result != "PASS"]
            }
        rows.append(row)
        print(
            f"  run {n}: ok={res.ok} ${row['cost_usd']} prompt={res.provenance[-1].prompt if res.provenance else '-'} → {row.get('build')}"
        )
    cost = round(sum(r["cost_usd"] for r in rows), 4)
    (out / "report.json").write_text(
        json.dumps(
            {"model": a.model, "pins": a.pin, "cost_usd": cost, "runs": rows},
            ensure_ascii=False,
            indent=1,
        ),
        "utf-8",
    )
    print(f"→ ${cost} · {out}")


def latest_contract() -> Path:
    found = list(OUT.glob("*-plan-contract-*/contract-1.json"))
    if not found:
        raise SystemExit(
            "plan_contract 스모크 결과가 없습니다 — 먼저 eval.starter.run_plan_contract"
        )
    return max(
        found, key=lambda p: p.stat().st_mtime
    )  # 가장 최근 — 스키마가 바뀐 옛 결과는 검증에서 걸린다


def _row(r: GenResult) -> dict[str, Any]:
    return {
        "task": r.task,
        "ok": r.ok,
        "label": r.overlay.label if r.overlay else None,
        "files": [f.path for f in r.overlay.files] if r.overlay else [],
        "first_problems": r.first_problems,
        "problems": r.problems,
        "flags": r.flags,
        "fail_reasons": r.fail_reasons,
        "calls": len(r.provenance),
        "cost_usd": round(sum(p.cost_usd or 0.0 for p in r.provenance), 4),
        "latency_s": round(sum(p.latency_ms or 0 for p in r.provenance) / 1000, 1),
    }


def gradle_test(work: Path, log: Path, timeout: int = 1200) -> dict[str, Any]:
    env = gradle_env()
    t0 = time.monotonic()
    with log.open("w") as fh:
        proc = subprocess.run(
            [str(GRADLE), "test", "--console=plain", "--no-daemon", "--continue"],
            cwd=work,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    results: dict[str, str] = {}
    failures: dict[str, list[str]] = {}
    for xml in sorted((work / "build" / "test-results" / "test").glob("TEST-*.xml")):
        text = xml.read_text("utf-8", errors="ignore")
        head = re.search(r"<testsuite [^>]*>", text)
        if not head:
            continue
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', head.group(0)))
        bad = int(attrs.get("failures", 0)) + int(attrs.get("errors", 0))
        name = attrs.get("name", xml.stem).rsplit(".", 1)[-1]
        results[name] = "PASS" if bad == 0 else "FAIL"
        msgs = [
            html.unescape(m)[:400]
            for m in re.findall(r'<(?:failure|error) message="([^"]*)"', text)
        ]
        if msgs:
            failures[name] = msgs
    compiled = (work / "build" / "classes").exists()
    reasons = sorted(
        set(
            re.findall(
                r"\[reason:([a-z0-9_]+)\]", " ".join(m for v in failures.values() for m in v)
            )
        )
    )
    return {
        "exit": proc.returncode,
        "seconds": round(time.monotonic() - t0, 1),
        "compiled": compiled,
        "classes": results,
        "failures": failures,
        "reasons_seen": reasons,
        "setup_error": not results and proc.returncode != 0,
    }


_GRADING = "grading/cases/"
_JAVAC = re.compile(r"^/\S+?/builds/[^/]+/(src/\S+\.java):(\d+): error: (.+)$")


def compile_errors(log: Path, limit: int = 15) -> list[str]:
    """javac 오류 → `경로:줄 메시지 — 코드` (작업 폴더 경로는 뺀다)."""
    lines = log.read_text("utf-8", errors="ignore").splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        m = _JAVAC.match(line)
        if m:
            code = lines[i + 1].strip() if i + 1 < len(lines) else ""
            item = f"- {m.group(1)}:{m.group(2)} {m.group(3)} — `{code}`"
            if item not in out:
                out.append(item)
    return out[:limit]


def build_all(
    t: Template,
    work: Path,
    starter: Overlay,
    hidden: Overlay | None,
    refs: list[Overlay],
    suffix: str = "",
) -> dict[str, Any]:
    """시작 코드(기존 테스트) + 참조 구현마다 숨은 테스트. 조립 폴더는 지우고 결과·로그만 남긴다."""
    builds: dict[str, Any] = {}
    jobs: list[tuple[str, tuple[Overlay, ...]]] = [] if suffix else [("starter", (starter,))]
    if hidden is not None:
        jobs += [(o.label or o.kind, (starter, o, hidden)) for o in refs]
    for name, parts in jobs:
        key = name + suffix
        assemble(t, work / key, *parts)
        res = gradle_test(work / key, work / f"{key}.log")
        res["compile_errors"] = compile_errors(work / f"{key}.log")
        builds[key] = res
        shutil.rmtree(work / key, ignore_errors=True)
        print(f"  build {key}: { {k: v for k, v in res.items() if k != 'compile_errors'} }")
    return builds


def _run_row(r: Any) -> dict[str, Any]:
    tests = {
        f"{t.group}:{t.name}": t.result + (f"[{t.reason}]" if t.reason else "")
        for t in r.tests
        if t.group != "selftest"
    }
    return {"target": r.target, "setup": r.setup, "seconds": r.seconds, "tests": tests}


async def run_loop(
    a: Any, b: BuildInputs, designs: list[Any], kw: dict[str, Any], src: Path, template: Template
) -> None:
    out = OUT / f"{date.today()}-loop{('-' + a.tag) if a.tag else ''}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)  # 첫 생성에서 멈춰 오버레이·검증이 없어도 report.json 은 남긴다
    verifier = GradleVerifier(template, out / "builds")
    res = await build_and_repair(b, designs, verifier=verifier, compiler=verifier, **kw)
    for key, o in res.overlays.items():
        _write(o, out / "overlays" / key)
    for n, snap in enumerate(res.snapshots):  # 검증한 회차마다 (다시 만든 산출물만 새로 씀)
        prev = res.snapshots[n - 1] if n else {}
        for key, o in snap.items():
            if prev.get(key) is not o:
                _write(o, out / f"round-{n}" / key)
    report = {
        "contract": str(src.relative_to(ROOT)),
        "model": a.model,
        "status": res.status,
        "rounds": res.rounds,
        "cost_usd": res.cost_usd,
        "calls": len(res.provenance),
        "regen_counts": res.regen_counts,
        "repairs": [r.model_dump() for r in res.repairs],
        "review_flags": res.review_flags,
        "adjustments": res.adjustments,
        "static_retries": res.static_retries,
        "rejected": write_rejected(res.rejected, out / "rejected"),
        "verify": [[_run_row(r) for r in rep.runs] for rep in res.reports],
        "verify_failures": [
            [{r.target: [t.model_dump() for t in r.tests if t.result != "PASS"]} for r in rep.runs]
            for rep in res.reports
        ],
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    for n, rep in enumerate(res.reports):
        print(
            f"  검증 {n}: "
            + " | ".join(f"{r.target} {r.setup} {_run_row(r)['tests']}" for r in rep.runs)
        )
    for r in res.repairs:
        print(f"  수리 {r.gate} {r.target} {r.result}: {r.reason}")
    print(
        f"→ {res.status} · 회차 {res.rounds} · 호출 {len(res.provenance)} · ${res.cost_usd} · {out}"
    )
    for f in res.review_flags:
        print(f"  검토: {f}")
    for f in res.adjustments:
        print(f"  코드가 고침: {f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--contract", default=None, help="plan_contract 결과 json (기본: 가장 최근 v2 스모크)"
    )
    ap.add_argument("--model", default=None)
    ap.add_argument("--openai-key-env", default=None)
    ap.add_argument("--budget-usd", type=float, default=0.5)
    ap.add_argument("--build", action="store_true")
    ap.add_argument(
        "--repair", action="store_true", help="숨은 테스트 컴파일 오류를 gen_hidden 에 1회 돌려준다"
    )
    ap.add_argument(
        "--loop",
        action="store_true",
        help="수리 루프 (nodes/starter/build.py) — 생성 → 정적 검사 → Gradle 검증 → 배정 → 그 생성기만 다시",
    )
    ap.add_argument("--designs", type=int, default=1, help="--loop 에서 만들 허용 설계 수")
    ap.add_argument(
        "--pin",
        action="append",
        default=[],
        help="프롬프트 버전 고정 (CURRENT 는 그대로) — 예: --pin gen_starter=2",
    )
    ap.add_argument(
        "--starter-only",
        type=int,
        default=0,
        help="시작 코드만 N 번 생성·빌드 (다른 생성기는 돌리지 않음 — 시작 코드 프롬프트 확인용)",
    )
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    src = Path(a.contract).resolve() if a.contract else latest_contract()
    data = json.loads(src.read_text("utf-8"))
    contract, plan = (
        TaskContract.model_validate(data["contract"]),
        HiddenPlan.model_validate(data["hidden_plan"]),
    )
    inc = load_incidents(ROOT / "incidents" / "public_v1")["sw.backend"]
    seed = inc.feature
    assert seed is not None
    task, _ = build_task(EXAMPLE)
    designs = [_design(i, d) for i, d in enumerate(seed.accepted_designs, 1)]
    task = task.model_copy(update={"accepted_designs": designs})
    template = load_template(JAVA)
    b = BuildInputs(
        task=task,
        contract=contract,
        plan=plan,
        template=template,
        seed=seed,
        avoid_terms=tuple(inc.avoid_terms),
    )
    llm = make_client(a.openai_key_env)
    pins = {k: int(v) for k, _, v in (x.partition("=") for x in a.pin)}
    prompts = ModelOverride(PinnedVersions(PromptRegistry(ROOT / "prompts"), pins), a.model)  # type: ignore[arg-type]
    kw = {"llm": llm, "prompts": prompts, "ontology_version": VERSION}
    print(
        f"계약 {src.relative_to(ROOT)} · 허용 설계 {[d.design_id + ' ' + d.summary[:30] for d in designs]}"
    )

    if a.starter_only:
        await run_starter_only(a, b, kw, template)
        return
    if a.loop:
        await run_loop(a, b, designs[: a.designs], kw, src, template)
        return
    starter = await gen_starter(b, **kw)  # type: ignore[arg-type]
    rows = [_row(starter)]
    spent = rows[0]["cost_usd"]
    print(f"  gen_starter ok={starter.ok} calls={rows[0]['calls']} ${spent}")
    others: list[GenResult] = []
    skipped: list[str] = []
    if starter.overlay is None:
        print("시작 코드가 없어 멈춤")
    else:
        so = starter.overlay
        jobs = {
            "gen_hidden": lambda: gen_hidden(b, **kw),  # type: ignore[arg-type]
            "gen_naive": lambda: gen_naive(b, so, **kw),  # type: ignore[arg-type]
            "gen_accepted": lambda: gen_accepted(b, so, designs[0], **kw),  # type: ignore[arg-type]
        }
        worst = spent
        for name, job in jobs.items():  # 하나씩 — 지금까지 가장 비싼 생성만큼 남지 않으면 건너뜀
            if spent + worst > a.budget_usd:
                skipped.append(name)
                continue
            r = await job()
            others.append(r)
            rows.append(_row(r))
            spent += rows[-1]["cost_usd"]
            worst = max(worst, rows[-1]["cost_usd"])
            print(f"  {name} ok={r.ok} calls={rows[-1]['calls']} ${rows[-1]['cost_usd']}")
        if skipped:
            print(f"예산 {a.budget_usd} 때문에 건너뜀: {', '.join(skipped)}")

    out = OUT / f"{date.today()}-generate{('-' + a.tag) if a.tag else ''}"
    if out.exists():
        shutil.rmtree(out)
    overlays = [r.overlay for r in [starter, *others] if r.overlay is not None]
    for o in overlays:
        _write(o, out / "overlays" / (o.kind + (f"-{o.label}" if o.label else "")))
    readme = render_readme(task, contract, template)
    (out / "README.md").write_text(readme.content, "utf-8")
    full = static_check(
        overlays,
        template=template,
        plan=plan,
        spec=SpecView.from_task(task),
        contract=contract,
        seed=seed,
        readme=readme,
        avoid_terms=inc.avoid_terms,
    )
    report: dict[str, Any] = {
        "contract": str(src.relative_to(ROOT)),
        "model": a.model,
        "generators": rows,
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "skipped": skipped,
        "static_check": {
            "problems": [p.__dict__ for p in full.problems],
            "review_flags": full.review_flags,
        },
    }

    if a.build and overlays:
        by = {(o.kind, o.label): o for o in overlays}
        st = by.get(("starter", ""))
        hidden = by.get(("hidden", ""))
        refs = [o for o in overlays if o.kind in ("ref_naive", "ref_accepted")]
        work = out / "builds"
        work.mkdir(parents=True, exist_ok=True)
        if st is not None:
            builds = build_all(template, work, st, hidden, refs)
            report["builds"] = builds
            # 수리 맛보기 (§7, 6단계 전): 숨은 테스트 컴파일 오류만 gen_hidden 에 1회 돌려준다
            errors = sorted(
                {e for b_ in builds.values() for e in b_.get("compile_errors", []) if _GRADING in e}
            )
            if a.repair and errors and hidden is not None and spent * 2 <= a.budget_usd:
                note = (
                    "이전 생성의 숨은 테스트가 컴파일되지 않았다. 같은 실수를 하지 않는다:\n"
                    + "\n".join(errors)
                )
                fixed = await gen_hidden(b, note=note, **kw)  # type: ignore[arg-type]
                rows.append(_row(fixed) | {"task": "gen_hidden (수리)"})
                report["cost_usd"] = round(sum(r["cost_usd"] for r in rows), 4)
                print(f"  gen_hidden 수리 ok={fixed.ok} ${rows[-1]['cost_usd']}")
                if fixed.overlay is not None:
                    _write(fixed.overlay, out / "overlays" / "hidden-repaired")
                    report["repair"] = {
                        "note": note,
                        "builds": build_all(template, work, st, fixed.overlay, refs, suffix="-r"),
                    }

    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")

    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    print(
        json.dumps(
            {k: report[k] for k in ("cost_usd", "static_check")}, ensure_ascii=False, indent=1
        )[:3000]
    )
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
