"""알파 테스트 — 처음 쓰는 공고 하나로 공고 → 기능형 명세 → 공개 계약 → 생성기 4종·수리 루프 → 응시용 프로젝트까지 (spec-to-code §11).

지금까지 생성 스모크는 배송 감사 손 예시 명세로만 돌렸다. 여기서는 명세도 공고에서 실제 경로(build_common → draft_feature_task)로 만든다.
사람 승인(interrupt)은 건너뛴다 — 평가 전용. 그래프 연결은 6b.

입력: 온톨로지 시범 공고(지원자 데이터 아님), 레포 재료 incidents/public_v1, Java 템플릿.
출력: data/eval/build/<날짜>-alpha-<tag>/ — git 밖
  spec.md      명세 전체 (숨김 부분 포함 — 사람 검토용)
  plan.md      숨은 테스트 계획 (숨김)
  contract.json  계약·숨은 계획 원본 (run_generate --contract 로 다시 쓸 수 있음)
  candidate/   응시자가 받는 프로젝트 (템플릿 project/ + 시작 코드 + README)
  overlays/    생성기 산출물 그대로 (starter · hidden · ref_naive · ref_accepted-*)
  builds/      Gradle 로그, report.json

사용:
  uv run python -m eval.starter.run_alpha --posting 'kakaobank_2026#005' \\
      --model openai:gpt-4.1 --openai-key-env OPENAI_PREMIUM_API_KEY --budget-usd 0.5
  # 명세는 그대로 두고 ② 계약부터 다시: --task-json data/eval/build/<이전 알파>/task.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from eval.spec.run_feature import ModelOverride, make_client, run_one, sample_md
from eval.starter.gradle_verify import assemble
from eval.starter.gradle_verify import write_overlay as _write
from eval.starter.run_generate import PinnedVersions, _run_row, usage_by_prompt
from eval.starter.run_plan_contract import JAVA
from eval.starter.verifiers import verifier_for
from task_spec.config import Settings
from task_spec.nodes.starter.build import build_and_repair
from task_spec.nodes.starter.generate import BuildInputs, plan_text
from task_spec.nodes.starter.plan_contract import plan_contract
from task_spec.ontology.queries import evidence as query_evidence
from task_spec.ontology.queries import posting_profile
from task_spec.ontology.relatedness import load_relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry
from task_spec.schemas import TaskItem
from task_spec.spec.build import build_common
from task_spec.spec.incidents import feature_incident, incident_by_id, load_incidents
from task_spec.starter.render import render_readme
from task_spec.starter.templates import load_template, select_template

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "eval" / "build"
LOOP_ESTIMATE_USD = (
    0.25  # 지난 수리 루프 8회 $0.13~0.19 — 남은 예산이 이보다 적으면 빌드 전에 멈춘다
)


def _cost(prov: list[Any]) -> float:
    return round(sum(p.cost_usd or 0.0 for p in prov), 4)


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--posting", required=True, help="온톨로지 시범 공고 id")
    ap.add_argument("--model", default=None)
    ap.add_argument("--openai-key-env", default=None)
    ap.add_argument("--budget-usd", type=float, default=0.5)
    ap.add_argument("--designs", type=int, default=1, help="참조 구현을 만들 허용 설계 수")
    ap.add_argument(
        "--task-json",
        default=None,
        help="이미 만든 명세(task.json)로 ② 계약부터 — 명세를 다시 만들지 않는다",
    )
    ap.add_argument(
        "--pin",
        action="append",
        default=[],
        help="프롬프트 버전 고정 (CURRENT 는 그대로) — 예: --pin plan_contract=8",
    )
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    pins = {k: int(v) for k, _, v in (x.partition("=") for x in a.pin)}
    reuse = Path(a.task_json).resolve() if a.task_json else None

    out = OUT / f"{date.today()}-alpha{('-' + a.tag) if a.tag else ''}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    report: dict[str, Any] = {
        "posting": a.posting,
        "model": a.model,
        "pins": pins,
        "approval": "생략(평가 전용)",
    }

    def save() -> None:
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")

    # ① 공고 → 기능형 명세 (run_feature 와 같은 경로)
    s = Settings()  # type: ignore[call-arg]
    repo = await OntologyRepository.open(s.database_url.get_secret_value(), "dev")
    version = repo.version.version
    incidents = load_incidents(s.incidents_dir)
    p = await posting_profile(repo, a.posting)
    if p is None or not (p.sub and p.industry_id):
        raise SystemExit(f"공고 프로필이 없음: {a.posting}")
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
        task_id=f"alpha-{a.posting}",
        ontology_version=version,
        ontology=repo,
        rel=await load_relatedness(repo),
        incidents=incidents,
        task_kind="feature",
    )
    await repo.close()
    report |= {"ontology_version": version, "sub": p.sub, "industry": p.industry_id}
    if built.task is None or built.ctx is None:
        report["stopped"] = f"공통 과제 틀을 못 만듦: {built.flags}"
        save()
        raise SystemExit(report["stopped"])

    llm = make_client(a.openai_key_env)
    prompts = ModelOverride(PinnedVersions(PromptRegistry(ROOT / "prompts"), pins), a.model)  # type: ignore[arg-type]
    task: TaskItem | None
    if reuse is not None:
        task = TaskItem.model_validate_json(reuse.read_text("utf-8"))
        spent = 0.0
        for name in ("spec.md",):  # 같은 명세의 사람 검토용 본문도 옆에 둔다
            if (reuse.parent / name).exists():
                shutil.copy2(reuse.parent / name, out / name)
        report["spec"] = {"reused": str(reuse.relative_to(ROOT)), "cost_usd": 0.0}
        print(f"① 명세 재사용 — {task.title} ({reuse.relative_to(ROOT)})")
    else:
        row = await run_one(llm, prompts, a.posting, built, asyncio.Semaphore(1))
        spent = row["cost_usd"]
        (out / "spec.md").write_text(sample_md(row), "utf-8")
        task = row.pop("_task", None)
        report["spec"] = {k: v for k, v in row.items() if k != "_task"}
        print(
            f"① 명세 ok_final={row['ok_final']} ${row['cost_usd']:.4f} — {task.title if task else '-'}"
        )
        if task is None or not row["ok_final"]:
            report["stopped"] = "명세 생성 실패"
            save()
            raise SystemExit(report["stopped"])
    (out / "task.json").write_text(
        task.model_dump_json(indent=1), "utf-8"
    )  # ② 부터 다시 돌릴 때 쓴다

    # ② 명세 → 공개 계약 + 숨은 테스트 계획
    # 명세가 쓴 재료 그대로 (없으면 직군 템플릿이 시험할 수 있는 사례 — 공고 id 로 고정 배정)
    inc = incident_by_id(incidents, task.source_incident) or feature_incident(
        incidents, p.sub, a.posting
    )
    if inc is None or inc.feature is None:
        raise SystemExit(f"기능형 재료가 없는 직군: {p.sub}")
    # 직군 → 템플릿 (없는 직군은 Java 로 — 종이 단계 밖)
    template = select_template(p.sub) or load_template(JAVA)
    res = await plan_contract(
        task,
        template,
        llm=llm,
        prompts=prompts,  # type: ignore[arg-type]
        ontology_version=version,
        seed=inc.feature,
    )
    spent += _cost(res.provenance)
    report["contract"] = {
        "ok": res.ok,
        "calls": len(res.provenance),
        "cost_usd": _cost(res.provenance),
        "first_problems": res.first_problems,
        "problems": res.problems,
        "spec_gaps": res.spec_gaps,
        "fail_reasons": res.fail_reasons,
        "adjustments": res.adjustments,
    }
    print(f"② 계약 ok={res.ok} ${_cost(res.provenance)} (누적 ${spent:.4f})")
    if res.contract is not None and res.hidden_plan is not None:
        (out / "contract.json").write_text(
            json.dumps(
                {
                    "contract": res.contract.model_dump(mode="json"),
                    "hidden_plan": res.hidden_plan.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=1,
            ),
            "utf-8",
        )
        (out / "plan.md").write_text(plan_text(res.hidden_plan, res.contract), "utf-8")
    if not res.ok or res.contract is None or res.hidden_plan is None:
        report["stopped"] = "계약 생성 실패"
        report["cost_usd"] = round(spent, 4)
        save()
        raise SystemExit(report["stopped"])
    readme = render_readme(task, res.contract, template)
    if spent + LOOP_ESTIMATE_USD > a.budget_usd:
        report["stopped"] = f"예산 — 지금까지 ${spent:.4f}, 빌드 예상 ${LOOP_ESTIMATE_USD}"
        report["cost_usd"] = round(spent, 4)
        (out / "README.md").write_text(readme.content, "utf-8")
        save()
        raise SystemExit(report["stopped"])

    # ③ 생성기 4종 + 수리 루프 (개발 서버 Gradle)
    designs = list(task.accepted_designs)[: a.designs]
    b = BuildInputs(
        task=task,
        contract=res.contract,
        plan=res.hidden_plan,
        template=template,
        seed=inc.feature,
        avoid_terms=tuple(inc.avoid_terms),
    )
    built_out = await build_and_repair(
        b,
        designs,
        llm=llm,
        prompts=prompts,  # type: ignore[arg-type]
        ontology_version=version,
        verifier=(g := verifier_for(template, out / "builds")),
        compiler=g,  # 채점 코드를 먼저 컴파일 — 오류는 쓴 생성기에 (build.compile_and_fix)
    )
    spent += built_out.cost_usd
    for key, o in built_out.overlays.items():
        _write(o, out / "overlays" / key)
    starter = built_out.overlays.get("starter")
    if (
        starter is not None
    ):  # 응시자가 받는 모양 — 응시용 이미지와 같은 층 (템플릿 project/ + 시작 코드 + README)
        assemble(template, out / "candidate", starter)
        (out / "candidate" / readme.path).write_text(readme.content, "utf-8")
    report["build"] = {
        "status": built_out.status,
        "rounds": built_out.rounds,
        "calls": len(built_out.provenance),
        "cost_usd": built_out.cost_usd,
        "designs": [f"{d.design_id} {d.summary}" for d in designs],
        "regen_counts": built_out.regen_counts,
        "repairs": [r.model_dump() for r in built_out.repairs],
        "review_flags": built_out.review_flags,
        "usage": usage_by_prompt(built_out.provenance),
        "adjustments": built_out.adjustments,
        "static_retries": built_out.static_retries,
        "verify": [[_run_row(r) for r in rep.runs] for rep in built_out.reports],
    }
    report["cost_usd"] = round(spent, 4)
    report["candidate_files"] = tree(out / "candidate") if starter is not None else []
    save()
    for n, rep in enumerate(built_out.reports):
        print(
            f"  검증 {n}: "
            + " | ".join(f"{r.target} {r.setup} {_run_row(r)['tests']}" for r in rep.runs)
        )
    for r in built_out.repairs:
        print(f"  수리 {r.gate} {r.target} {r.result}: {r.reason}")
    print(f"③ 빌드 {built_out.status} · 회차 {built_out.rounds} · 누적 ${spent:.4f} · {out}")
    for f in built_out.review_flags:
        print(f"  검토: {f}")
    for f in built_out.adjustments:
        print(f"  코드가 고침: {f}")


if __name__ == "__main__":
    asyncio.run(main())
