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
  # 함정 틀 (#318 5단계): 재료 분석 → 틀(CURRENT) → 틀 적용 재료로 명세·계약·빌드. 가상 세션 유지 재료(허용 설계 셋)에서 JWT 설계를
  # 사람이 '뺀다'고 한 것처럼 빼고 템플릿 v5 로 — 리포트 frame 칸에 틀·검사 1~4 결과
  uv run python -m eval.starter.run_alpha --posting 'autoever_1_both#001' --virtual-incident session --trap-frame \\
      --remove-design '[설계] 서명된 토큰(JWT)' --template-version 5 --budget-usd 0.6 --tag frame-1
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
from eval.spec.run_feature_frame import SIGNED_WITH_MEMORY, frame_run, session_material
from eval.starter.run_generate import PinnedVersions, _run_row, usage_by_prompt, write_rejected
from eval.starter.run_plan_contract import JAVA
from task_spec.config import Settings
from task_spec.interview.frame_leaks import frame_leak_flags
from task_spec.interview.trap_frame import design_questions, evidence_gaps, match_design
from task_spec.nodes.starter.build import build_and_repair
from task_spec.nodes.starter.generate import BuildInputs, plan_text
from task_spec.nodes.starter.plan_contract import plan_contract
from task_spec.ontology.queries import evidence as query_evidence
from task_spec.ontology.queries import posting_profile
from task_spec.ontology.relatedness import load_relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry
from task_spec.schemas import PublicIncident, TaskItem, TrapFrame
from task_spec.spec.build import build_common
from task_spec.spec.incidents import (
    feature_incident,
    incident_by_id,
    load_incidents,
    load_interfaces,
    pin_feature_incident,
    with_incident,
)
from task_spec.starter.hand_refs import load_references
from task_spec.starter.interface import interface_ref
from task_spec.starter.material_guide import guide_ref, load_guides
from task_spec.starter.render import render_readme, thin_readme
from task_spec.starter.templates import load_template, select_template
from task_spec.starter.verify import verifier_for
from task_spec.starter.verify.workspace import assemble
from task_spec.starter.verify.workspace import write_overlay as _write

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
    ap.add_argument(
        "--model-for",
        action="append",
        default=[],
        help="단계별 모델 (--model 보다 우선) — 예: --model-for plan_contract=openai:gpt-4.1",
    )
    ap.add_argument("--openai-key-env", default=None)
    ap.add_argument("--budget-usd", type=float, default=0.5)
    ap.add_argument(
        "--designs",
        type=int,
        default=None,
        help="참조 구현을 만들 허용 설계 수 (기본: 명세의 허용 설계 전부 — #171)",
    )
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
    ap.add_argument(
        "--incident",
        default=None,
        help="기능형 재료 고정 (공고 해시 배정 대신) — 예: --incident pub:sw.frontend:previous-user-data-after-logout",
    )
    ap.add_argument(
        "--template-version",
        type=int,
        default=None,
        help="템플릿 판 고정 (CURRENT 는 그대로) — 예: --template-version 10",
    )
    ap.add_argument(
        "--virtual-incident",
        choices=["session"],
        default=None,
        help="가상 재료 (평가 코드 안 — 공개 재료 목록 밖): session = 세션 유지 (#318, 허용 설계 셋)",
    )
    ap.add_argument(
        "--trap-frame",
        action="store_true",
        help="함정 틀 (#318): 재료 분석 → 틀(CURRENT) → 틀 적용 재료로 명세·계약·빌드 (인터뷰 서비스·빌더와 같은 길)",
    )
    ap.add_argument(
        "--remove-design",
        action="append",
        default=[],
        help="⑧ 설계 질문에 '뺀다'라고 답한 설계 (재료 허용 설계의 앞부분) — --trap-frame 과 함께",
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
        "model_for": a.model_for,
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
    # 직군 → 템플릿 (없는 직군은 Java 로 — 종이 단계 밖). 틀은 템플릿을 보고 만든다 — 명세보다 먼저 정한다
    template = select_template(p.sub) or load_template(JAVA)
    if (
        a.template_version is not None
    ):  # 템플릿 판 고정 (CURRENT 는 그대로) — 예: 코드 뼈대 v10 을 v9 와 비교
        template = load_template(template.manifest.template_id, a.template_version)
        report["template_pinned"] = template.manifest.ref
    # 어느 판의 검사·수리 칸으로 돌렸는지 (같은 vN 에서 고칠 수 있는 칸 — #176)
    report["template"] = {"ref": template.manifest.ref, "manifest_sha256": template.manifest_sha256}
    llm = make_client(a.openai_key_env)
    per_task = dict(x.split("=", 1) for x in a.model_for)
    prompts = ModelOverride(
        PinnedVersions(PromptRegistry(ROOT / "prompts"), pins), a.model, per_task=per_task
    )  # type: ignore[arg-type]
    if a.incident:
        incidents = pin_feature_incident(incidents, p.sub, a.incident)
        report["incident_pinned"] = a.incident
    base: PublicIncident | None = None
    if a.virtual_incident == "session":
        base = session_material(s.incidents_dir, extra_designs=[SIGNED_WITH_MEMORY])
        if base.sub != p.sub:
            raise SystemExit(f"가상 재료 직군({base.sub})과 공고 직군({p.sub})이 다름")
        incidents = with_incident(incidents, base)
        report["incident_virtual"] = base.incident_id
    frame: TrapFrame | None = None
    frame_cost = 0.0
    if a.trap_frame:
        base = base or feature_incident(incidents, p.sub, a.posting)
        if base is None or base.feature is None:
            raise SystemExit(f"기능형 재료가 없는 직군: {p.sub}")
        run = await frame_run(llm, base, template, a.remove_design)
        frame, frame_cost = run.frame, run.cost_usd
        # 틀 적용 재료로 명세·계약·빌드 — 인터뷰 서비스의 _framed·빌더 Job.framed 와 같은 자리
        incidents = with_incident(incidents, base.model_copy(update={"feature": run.framed}))
        report["frame"] = {
            "kind": frame.kind,
            "reason": frame.reason,
            "hidden_requirement": frame.hidden_requirement,
            "forbidden_storage": [f"{x.entity} ({x.why})" for x in frame.forbidden_storage],
            "forbidden_terms": frame.forbidden_terms,
            "stateful_target_forbidden": frame.stateful_target_forbidden,
            "public_rules": frame.public_rules,
            "design_matrix": [f"{d.holds}: {d.design[:70]} — {d.why}" for d in frame.design_matrix],
            "notes": run.notes,
            # 검사 1·2 — 인터뷰라면 ⑧ 에서 물었을 것
            "evidence_gaps": [
                e.flag for e in evidence_gaps(frame.reason, template, base.feature, run.analysis)
            ],
            "design_questions": [q for q, _ in design_questions(frame, base.feature)],
            "removed": [match_design(r, base.feature.accepted_designs) for r in a.remove_design],
            "accepted_designs": run.framed.accepted_designs,
            "cost_usd": round(frame_cost, 4),
        }
        print(
            f"⓪ 틀 금지 {[x.entity for x in frame.forbidden_storage]} · 설계 질문 "
            f"{len(report['frame']['design_questions'])} · 허용 설계 {len(run.framed.accepted_designs)} (${frame_cost:.4f})"
        )
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

    if frame is not None and built.ctx is not None:
        built.ctx.trap_frame = frame  # 명세 초안 입력 (v13~ — 지금 CURRENT v12 는 받지 않는다)
    task: TaskItem | None
    if reuse is not None:
        task = TaskItem.model_validate_json(reuse.read_text("utf-8"))
        spent = 0.0
        for name in ("spec.md",):  # 같은 명세의 사람 검토용 본문도 옆에 둔다
            if (reuse.parent / name).exists():
                shutil.copy2(reuse.parent / name, out / name)
        # worktree 의 data/ 는 본 체크아웃으로 가는 링크라 resolve 하면 레포 밖일 수 있다 — 그때는 절대 경로로 적는다
        shown = str(reuse.relative_to(ROOT)) if reuse.is_relative_to(ROOT) else str(reuse)
        report["spec"] = {"reused": shown, "cost_usd": 0.0}
        print(f"① 명세 재사용 — {task.title} ({shown})")
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
    spent += frame_cost
    if frame is not None and base is not None:  # 검사 3 공개 중립성 — 명세 1단계 글에 틀이 막은 것
        report["frame"]["public_leaks"] = frame_leak_flags(task, frame, base.feature)

    # ② 명세 → 공개 계약 + 숨은 테스트 계획
    # 명세가 쓴 재료 그대로 (없으면 직군 템플릿이 시험할 수 있는 사례 — 공고 id 로 고정 배정)
    inc = incident_by_id(incidents, task.source_incident) or feature_incident(
        incidents, p.sub, a.posting
    )
    if inc is None or inc.feature is None:
        raise SystemExit(f"기능형 재료가 없는 직군: {p.sub}")
    interface = load_interfaces(s.incidents_dir).get(inc.incident_id)  # #256 재료 인터페이스
    # 같은 프롬프트 판이라도 재료 인터페이스 파일이 바뀌면 계약 입력이 달라진다 — 어느 판으로 돌렸는지 남긴다
    report["interface"] = (
        {"incident_id": interface.incident_id, "sha256": interface_ref(interface)}
        if interface
        else None
    )
    guide = load_guides(s.incidents_dir).get(inc.incident_id)  # #256 재료 숨은 안내
    report["guide"] = (
        {"incident_id": guide.incident_id, "sha256": guide_ref(guide)} if guide else None
    )
    res = await plan_contract(
        task,
        template,
        llm=llm,
        prompts=prompts,  # type: ignore[arg-type]
        ontology_version=version,
        seed=inc.feature,
        interface=interface,
        guide=guide,
        # 함정 틀 검사 4 (#318) — 금지 저장 엔티티를 계약 입력에 알리고 생성 뒤 대조 (빌더 steps.contract 와 같게)
        forbidden_storage=frame.forbidden_storage if frame is not None else (),
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
        "forbidden_storage_asks": [
            x for x in res.first_problems if "공개 계약에 두지 않는다" in x
        ],  # 검사 4 가 재요청한 것
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
    hand = load_references(s.incidents_dir).get(inc.incident_id)  # #256 3단계 손 구현 관문
    b = BuildInputs(
        task=task,
        contract=res.contract,
        plan=res.hidden_plan,
        template=template,
        seed=inc.feature,
        avoid_terms=tuple(inc.avoid_terms),
        references=hand.checked_against(interface) if hand is not None else None,
        guide=guide,
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
    if (
        built_out.hidden_plan is not None
    ):  # 채점에서 뺀 케이스·보정한 조절값 — 이미지·저장이 읽는 계획도 같게 (원래 계획은 contract.orig.json)
        (out / "contract.json").rename(out / "contract.orig.json")
        (out / "contract.json").write_text(
            json.dumps(
                {
                    "contract": res.contract.model_dump(mode="json"),
                    "hidden_plan": built_out.hidden_plan.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=1,
            ),
            "utf-8",
        )
        (out / "plan.md").write_text(plan_text(built_out.hidden_plan, res.contract), "utf-8")
    for key, o in built_out.overlays.items():
        _write(o, out / "overlays" / key)
    starter = built_out.overlays.get("starter")
    if (
        starter is not None
    ):  # 응시자가 받는 모양 — 응시용 이미지와 같은 층 (템플릿 project/ + 시작 코드 + README)
        assemble(template, out / "candidate", starter)
        # 시작 코드가 정해진 뒤 다시 — '시작 코드' 절(이미 있는 파일·함수 이름)을 그 시작 코드에서 뽑아 넣는다 (starter/code_map.py)
        readme = render_readme(task, res.contract, template, starter=starter.files)
        (out / "candidate" / readme.path).write_text(readme.content, "utf-8")
        if thin_readme(task):  # 문제 분석 판 — 원래 판은 응시자 폴더 밖에 (diff 로 두 판을 견준다)
            full = render_readme(task, res.contract, template, starter=starter.files, full=True)
            (out / full.path).write_text(full.content, "utf-8")
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
        "rejected": write_rejected(built_out.rejected, out / "rejected"),
        "verify": [[_run_row(r) for r in rep.runs] for rep in built_out.reports],
        "dropped_cases": built_out.dropped_cases,  # 변별하지 못해 채점에서 뺀 숨은 케이스
        # 공개 테스트 변별 관문 (#164) — 변형마다 잡힘·살아남음·무효, 버린 변형, 변형 실행 결과
        "variants": [r.line() for r in built_out.variants],
        "variant_dropped": built_out.variant_dropped,
        "variant_verify": [
            [r.model_dump(mode="json") for r in rep.runs] for rep in built_out.variant_reports
        ],
        # 숨은 테스트 함정 관문 (#171) — 허용 설계의 함정에 빠진 변형마다 잡힘·살아남음·무효
        "traps": [r.line() for r in built_out.traps],
        "trap_dropped": built_out.trap_dropped,
        "trap_verify": [
            [r.model_dump(mode="json") for r in rep.runs] for rep in built_out.trap_reports
        ],
        # 손 구현 관문 (#256 3단계) — 손 구현마다 마지막 결과 한 줄, 채점에 남긴 케이스, 실행 결과
        "hand": built_out.hand,
        "hand_kept": built_out.hand_kept,
        "hand_verify": [
            [r.model_dump(mode="json") for r in rep.runs] for rep in built_out.hand_reports
        ],
        # 조절값 보정 (#301 PR 2) — 결과·고른 값·창·조합마다 결과, 조합마다 실행 결과
        "calibration": built_out.calibration.row() if built_out.calibration is not None else None,
        "calibration_verify": [
            [r.model_dump(mode="json") for r in rep.runs] for rep in built_out.calibration_reports
        ],
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
    for line in built_out.hand:
        print(f"  손 구현: {line}")
    if (cal := built_out.calibration) is not None:
        print(f"  조절값 보정: {cal.status} {dict(cal.values) or ''} (시험 {len(cal.trials)}회)")
    for f in built_out.review_flags:
        print(f"  검토: {f}")
    for f in built_out.adjustments:
        print(f"  코드가 고침: {f}")


if __name__ == "__main__":
    asyncio.run(main())
