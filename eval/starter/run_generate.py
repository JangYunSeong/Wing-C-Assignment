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
import os
import re
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

from eval.spec.run_feature import ModelOverride, make_client
from eval.starter.run_plan_contract import EXAMPLE, JAVA, build_task
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
GRADLE = ROOT / "data" / "feature_starters" / ".tools" / "dist" / "gradle-8.10.2" / "bin" / "gradle"
VERSION = "smoke"


def latest_contract() -> Path:
    found = sorted(OUT.glob("*-plan-contract-*v2*/contract-1.json"))
    if not found:
        raise SystemExit(
            "plan_contract v2 스모크 결과가 없습니다 — 먼저 eval.starter.run_plan_contract --prompt-version 2"
        )
    return found[-1]


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


def _write(overlay: Overlay, root: Path) -> None:
    for f in overlay.files:
        p = root / f.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f.content, "utf-8")


def assemble(t: Template, work: Path, *overlays: Overlay) -> None:
    """템플릿 project/ + 오버레이 (+ hidden 이면 잠긴 하네스·자체 점검) — 채점용 이미지 조립과 같은 순서."""
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(t.project, work)
    for o in overlays:
        if o.kind == "hidden" and t.manifest.hidden_layout is not None:
            hl = t.manifest.hidden_layout
            for sub, dest in (("hidden/harness", hl.harness), ("hidden/selftest", hl.selftest)):
                for rel in t.files(sub):
                    target = work / dest / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(t.root / sub / rel, target)
        _write(o, work)


def gradle_test(work: Path, log: Path, timeout: int = 1200) -> dict[str, Any]:
    env = os.environ | {"GRADLE_USER_HOME": str(ROOT.parent / ".gradle-make-task-eval")}
    if Path("/root/.claude/jobs/2da1d8ae/tmp/gradle-home").is_dir():
        env["GRADLE_USER_HOME"] = (
            "/root/.claude/jobs/2da1d8ae/tmp/gradle-home"  # 2단계 확인 때 받은 의존성 재사용
        )
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
    prompts = ModelOverride(PromptRegistry(ROOT / "prompts"), a.model)
    kw = {"llm": llm, "prompts": prompts, "ontology_version": VERSION}
    print(
        f"계약 {src.relative_to(ROOT)} · 허용 설계 {[d.design_id + ' ' + d.summary[:30] for d in designs]}"
    )

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
