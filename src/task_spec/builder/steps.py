"""builder 의 실제 단계 — 알파(`eval/starter/run_alpha.py`)와 같은 순서·같은 함수를 승인된 공통 과제에서 시작한다.

① 계약: 명세의 재료(source_incident) → 직군 템플릿(CURRENT) → `plan_contract`
② 빌드: `build_and_repair` (생성기 4종 + 수리 루프, 검증기는 격리 컨테이너 #127) → README 렌더 (시작 코드가 정해진 뒤)
③ 이미지: 템플릿 층·채점 기반 층 → 응시용·채점용 → 응시용 오프라인 테스트·제출물 채점 → 응시용 레이어 금지 경로 검사
④ 등록: 역할별 저장(zstd)·descriptor(승인 전 approval=pending)·SHA256SUMS → 빌드 저장소 (`starter/loader.py`)

무거운 동기 작업(podman 빌드·저장)은 스레드에서 — heartbeat 가 멈추지 않게. 생성 코드·명세 원문은 로그에 남기지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from task_spec.builder.runner import (
    BuildResult,
    ContractResult,
    ImagesResult,
    Job,
    StepFailed,
)
from task_spec.llm import LLMClient
from task_spec.nodes.starter.build import HIDDEN, PUBLIC, build_and_repair
from task_spec.nodes.starter.generate import BuildInputs
from task_spec.nodes.starter.plan_contract import PlanResult, plan_contract
from task_spec.prompting import PromptRegistry
from task_spec.schemas import PublicIncident, TaskItem
from task_spec.schemas.build_record import BuildFailure, BuildTrace
from task_spec.schemas.package import PackageHeader
from task_spec.spec.company_incidents import CompanyIncidentReader, is_company_incident
from task_spec.spec.incidents import incident_by_id, load_incidents, load_interfaces
from task_spec.starter.artifacts import summaries
from task_spec.starter.contract import SpecView
from task_spec.starter.hand_refs import load_references
from task_spec.starter.images import (
    TaskImages,
    TaskMaterial,
    build_layers,
    build_task_images,
    scan_candidates,
    verify_task_images,
)
from task_spec.starter.layers import Podman
from task_spec.starter.loader import ImageRef, PackageInputs, register_packages
from task_spec.starter.material_guide import guide_problems, load_guides
from task_spec.starter.render import render_readme, thin_readme
from task_spec.starter.repair import MATERIAL_VERDICT, STARTER
from task_spec.starter.static_check import Overlay, readme_flags
from task_spec.starter.store import BuildRepo
from task_spec.starter.templates import select_template
from task_spec.starter.verify import verifier_for
from task_spec.starter.verify.workspace import write_overlay

PENDING_NOTE = "승인 전 묶음 — 시작 코드 승인(#130) 뒤에만 전달한다"
# 수리 루프가 실패한 빌드의 마지막 생성물 (작업 폴더 안, 실패 작업 폴더는 남는다 — runner.keep_failed_work).
# 2026-11-18 ct-10eb910705dd-b2: 검증 폴더는 실행마다 지워져 로그만 남아, 허용 구현이 왜 숨은 케이스에 떨어졌는지 코드로 확인하지 못했다
FAILED_OVERLAYS = "overlays"


def keep_overlays(overlays: Mapping[str, Overlay], root: Path) -> list[str]:
    """오버레이를 `root/<키>/<경로>` 로 쓴다 (알파 실행 폴더의 overlays/ 와 같은 모양 — 경로는 GeneratedFile 이 `..`·절대 경로를 막는다).
    쓴 키 목록."""
    for key, o in overlays.items():
        write_overlay(o, root / key)
    return list(overlays)


def _cost(provenance: list[object]) -> float:
    return round(sum(getattr(p, "cost_usd", None) or 0.0 for p in provenance), 4)


# 검사 문장에서 값·인용·괄호가 시작되는 곳 — 종류는 그 앞까지 ('HiddenPlan request_timeout_s=1 이 근거 인용 …' → 조절값 이름까지)
_VALUE_START = re.compile(r"[=(\"'“‘]")


def _kind(item: str) -> str:
    """검사 문제·명세 결함 한 줄의 종류만 — 첫 ' — '·':' 앞, 그리고 값·인용·괄호가 시작되기 전까지.
    뒤에는 명세의 숨은 요구·테스트 문구나 자료 인용이 올 수 있어 사유에 넣지 않는다 (2026-11-12 스모크: 계약 실패 사유에
    숨은 요구 문장이 그대로 들어갔다. 2026-11-15: HiddenPlan 조절값 문장은 머리에 값과 자료 인용이 온다)."""
    head = _VALUE_START.split(item.split(" — ")[0].split(":")[0], maxsplit=1)[0]
    return " ".join(head.split())[:60]


def _call_kind(item: str) -> str:
    """호출 실패 한 줄의 종류 — 예외 이름과 끝난 이유 (plan_contract 가 'EmptyResponse(finish_reason=stop): …' 로 적는다)."""
    return " ".join(item.split(":")[0].split())[:60]


def _counted(kinds: list[str], n: int = 3) -> str:
    """종류별 개수 — 많은 순(같으면 먼저 나온 순)으로 n 종까지."""
    seen = Counter(k for k in kinds if k)
    shown = [f"{k} ×{c}" if c > 1 else k for k, c in seen.most_common(n)]
    rest = len(seen) - len(shown)
    return " | ".join(shown) + (f" 외 {rest}종" if rest > 0 else "")


def contract_failure(res: PlanResult) -> str:
    """계약 단계 실패 사유 — 호출 실패·명세 결함·검사 문제를 모두 싣는다. 2026-11-15 운영 계약 실패: 첫 번째로 비어 있지 않은
    목록만 실어, 세 번 중 한 번 빈 응답이 나면 나머지 두 번이 검사에서 걸린 사유가 빠지고 EmptyResponse 만 남았다.
    검사 문제·명세 결함은 채택한 답(가장 문제가 적은 회차)의 것, 호출 실패는 실패한 호출마다 하나."""
    parts = [
        f"{label} {_counted(kinds)}"
        for label, kinds in (
            ("호출 실패", [_call_kind(x) for x in res.fail_reasons]),
            ("명세 결함", [_kind(x) for x in res.spec_gaps]),
            ("검사 문제", [_kind(x) for x in res.problems]),
        )
        if kinds
    ]
    return " · ".join(parts) or "사유 없음"


def build_trace(
    out: Any, contract_adjustments: Sequence[str] = (), failure_kind: str | None = None
) -> BuildTrace:
    """빌드 기록 칸 (설계 검사 #332 T-F16) — 알파 보고서에만 있던 코드 맞춤·정적 재생성·보정·관문 결과. 원문 없음 (값·판정 한 줄).
    out 은 build_and_repair 의 결과 — 칸이 없는 대역(테스트)도 받는다."""
    calibration: list[str] = []
    cal = getattr(out, "calibration", None)
    if cal is not None:
        row = cal.row()
        trials = row["trials"]
        calibration.append(
            f"{row['status']} 사유 {row['reason'] or '-'} · 값 {row['values'] or '-'} · 창 {row['window'] or '-'} · "
            f"시험 {len(trials)}회 (흔한 구현 떨어짐 {sum(1 for t in trials if t['caught'])}회)"
        )
    return BuildTrace(
        failure_kind=failure_kind,  # type: ignore[arg-type]
        contract_adjustments=list(contract_adjustments),
        build_adjustments=list(getattr(out, "adjustments", [])),
        static_retries=list(getattr(out, "static_retries", [])),
        calibration=calibration,
        gates=[
            *(v.line() for v in getattr(out, "variants", [])),
            *(t.line() for t in getattr(out, "traps", [])),
            *getattr(out, "hand", []),
        ],
    )


class RealSteps:
    def __init__(
        self,
        llm: LLMClient,
        prompts: PromptRegistry,
        incidents_dir: Path,
        images_root: Path,
        designs: int | None = None,  # None = 허용 설계 전부 (#171)
        company_incidents: CompanyIncidentReader | None = None,  # 회사 결정적 사건 (co:…) — 읽기만
    ) -> None:
        self.llm, self.prompts = llm, prompts
        self.incidents = load_incidents(incidents_dir)
        # 재료가 정한 공개 인터페이스 (#256) — 공개 사례 파일만. 회사 사례(co:…)는 없어 계약 인터페이스를 LLM 이 정한다
        self.interfaces = load_interfaces(incidents_dir)
        # 재료 손 구현 묶음 (#256 3단계) — 손 구현 관문(⑫). 인터페이스가 있는 재료만 (손 구현은 인터페이스대로 실행된다)
        self.references = load_references(incidents_dir)
        # 재료 숨은 안내 (#256 재료 옮기기) — 재료의 사유 정의·숨은 테스트 규칙·공개 테스트 입력 범위. 공개 사례 파일만
        self.guides = load_guides(incidents_dir)
        self.images_root = images_root
        self.designs = designs
        self.company_incidents = company_incidents

    async def incident_for(self, task: TaskItem) -> PublicIncident | None:
        """명세가 적은 재료(source_incident)를 찾는다 — 공개 사례는 파일, 회사 사례(co:…)는 DB (내용은 등록 뒤 바뀌지 않는다).
        재료 id 가 있는데 찾지 못하면 실패한다 — 예전에는 그 직군의 다른 사례로 말없이 바꿔 만들어, 승인된 명세와 다른 재료로
        빌드했다. 재료 id 가 없는 명세는 None — contract 가 직군(템플릿) 단계에서 먼저 멈춘다."""
        sid = task.source_incident
        inc = incident_by_id(self.incidents, sid)
        if inc is None and is_company_incident(sid) and self.company_incidents is not None:
            assert sid is not None
            try:
                rec = await self.company_incidents.get(sid)
            except Exception as exc:  # 권한 없음 등 — 사유를 남기고 멈춘다
                raise StepFailed(
                    f"contract: 회사 사례 {sid} 를 읽지 못함 ({type(exc).__name__}) — builder DB 역할의 "
                    "company_incident 읽기 권한 확인 (docker/builder/db-role.sql)"
                ) from exc
            inc = rec.incident if rec is not None else None
        if inc is None and sid:
            raise StepFailed(
                f"contract: 명세의 재료 {sid} 를 찾을 수 없음 — 다른 재료로 바꿔 만들지 않는다"
            )
        return inc

    # ① 계약
    async def contract(self, job: Job) -> ContractResult:
        task = job.task
        sub = (task.source_incident or "").split(":")[1] if task.source_incident else ""
        template = select_template(sub) if sub else None
        if template is None:
            raise StepFailed(f"contract: 템플릿이 없는 직군 ({sub or '재료 없음'})")
        inc = await self.incident_for(task)
        if inc is None or inc.feature is None:
            raise StepFailed(f"contract: 기능형 재료가 없는 직군 ({sub})")
        framed = job.framed
        if framed is not None:
            # 함정 틀 (#318 D1) — 이 공고의 틀 적용 재료로 (숨은 요구·사유·필요 요소·금지 낱말). 공용 재료는 그대로 두었다
            inc = inc.model_copy(update={"feature": framed.seed})
        interface = self.interfaces.get(inc.incident_id)
        guide = self.guides.get(inc.incident_id)
        if guide is not None and (bad := guide_problems(guide, inc)):
            # 재료 데이터 결함 — 다른 재료의 빌드는 그대로 돈다. 안내 없이 만들면 틀린 안내로 조용히 빌드된다
            raise StepFailed(
                f"contract: 재료 숨은 안내가 템플릿과 맞지 않음 — {'; '.join(bad)}"[:280]
            )
        forbidden = tuple(framed.frame.forbidden_storage) if framed is not None else ()
        res = await plan_contract(
            task,
            template,
            llm=self.llm,
            prompts=self.prompts,
            ontology_version=job.build.ontology_version,
            seed=inc.feature,
            interface=interface,
            guide=guide,
            forbidden_storage=forbidden,
            check_terms=tuple(inc.hidden_terms),
            avoid_terms=tuple(
                inc.avoid_terms
            ),  # 계약 글(시드·fixture·공개 테스트 값)의 고유어 — 설계 검사 #332 T-F6
        )
        if not res.ok or res.contract is None or res.hidden_plan is None:
            raise StepFailed(
                f"contract: 계약 생성 실패 — {contract_failure(res)}",
                cost_usd=_cost(list(res.provenance)),
                provenance=res.provenance,
            )
        return ContractResult(
            template=template,
            contract=res.contract,
            plan=res.hidden_plan,
            cost_usd=_cost(list(res.provenance)),
            seed=inc.feature,
            interface=interface,
            guide=guide,
            avoid_terms=tuple(inc.avoid_terms),
            forbidden_storage=forbidden,
            check_terms=tuple(inc.hidden_terms),
            adjustments=tuple(res.adjustments),
            review_flags=tuple(res.flags),
            provenance=tuple(res.provenance),
        )

    # ② 빌드
    async def build(self, job: Job, c: ContractResult) -> BuildResult:
        refs = self.references.get(c.interface.incident_id) if c.interface is not None else None
        b = BuildInputs(
            task=job.task,
            contract=c.contract,
            plan=c.plan,
            template=c.template,
            seed=c.seed,
            avoid_terms=c.avoid_terms,
            references=refs.checked_against(c.interface) if refs is not None else None,
            guide=c.guide,
            forbidden_storage=c.forbidden_storage,
            check_terms=c.check_terms,
        )
        g = verifier_for(c.template, job.work / "verify")
        out = await build_and_repair(
            b,
            list(job.task.accepted_designs)[: self.designs],
            llm=self.llm,
            prompts=self.prompts,
            ontology_version=job.build.ontology_version,
            verifier=g,
            compiler=g,  # 채점 코드를 먼저 컴파일 — 오류는 쓴 생성기에 (build.compile_and_fix)
        )
        if out.status != "review" or STARTER not in out.overlays or HIDDEN not in out.overlays:
            await asyncio.to_thread(keep_overlays, out.overlays, job.work / FAILED_OVERLAYS)
            last = out.repairs[-1] if out.repairs else None
            # 수리 사유(reason)에는 숨은 테스트 단언 문구가 올 수 있다 — 관문·대상·결과만
            why = (
                f"{last.gate} {last.target} {last.result}" if last is not None else "수리 기록 없음"
            )
            # 실패 종류를 사유 머리에 (설계 검사 #332 B-F1) — 재료 판정은 같은 명세로 다시 요청해도 되풀이되기 쉬워 담당자가 알아야 한다.
            # 판정 문장은 실패 빌드 기록(DB)에만, 사유에는 종류·회차·관문만
            material = any(f.startswith(MATERIAL_VERDICT) for f in out.review_flags)
            head = "재료 판정으로 멈춤" if material else "수리 루프 실패"
            raise StepFailed(
                f"build: {head} (회차 {out.rounds}) — {why}",
                cost_usd=out.cost_usd,
                provenance=out.provenance,
                # 사유 문장(숨은 테스트 문구가 올 수 있음)은 저장소 사유가 아니라 실패 빌드 기록(DB)에만
                detail=BuildFailure(
                    hidden_plan=out.hidden_plan or c.plan,
                    review_flags=list(out.review_flags),
                    repairs=list(out.repairs),
                    trace=build_trace(out, c.adjustments, "material" if material else "generation"),
                ),
            )
        starter = out.overlays[STARTER].files
        readme = render_readme(job.task, c.contract, c.template, starter=starter)
        # 응시용 README 누설 검사 → 승인 화면 README 경고 칸 (#232: 렌더만 하고 검사하지 않았다)
        spec = SpecView.from_task(job.task)
        flags = readme_flags(
            readme,
            spec=spec,
            seed=c.seed,
            avoid_terms=c.avoid_terms,
            forbidden_storage=c.forbidden_storage,
            check_terms=c.check_terms,
        )
        # 문제 분석 판 — 원래 판도 같은 명세에서 (손으로 고치지 않는다, analysis-stage.md §6)
        full = (
            render_readme(job.task, c.contract, c.template, starter=starter, full=True)
            if thin_readme(job.task)
            else None
        )
        return BuildResult(
            overlays=dict(out.overlays),
            plan=out.hidden_plan or c.plan,
            readme=readme.content,
            review_flags=[*out.review_flags, *flags],
            repairs=list(out.repairs),
            cost_usd=out.cost_usd,
            readme_full=full.content if full is not None else None,
            provenance=tuple(out.provenance),
            trace=build_trace(out, c.adjustments),
        )

    # ③ 이미지
    async def images(self, job: Job, c: ContractResult, b: BuildResult) -> ImagesResult:
        return await asyncio.to_thread(self._images, job, c, b)

    def _images(self, job: Job, c: ContractResult, b: BuildResult) -> ImagesResult:
        t = c.template
        ov = b.overlays
        item = TaskMaterial(
            source=job.build.build_id,
            task=job.task,
            contract=c.contract,
            plan=b.plan,
            common_id=job.build.common_id,
            revision=job.label.revision or 1,
            ontology_version=job.build.ontology_version,
            readme=b.readme,
            starter=ov[STARTER],
            hidden=ov[HIDDEN],
            public=ov.get(PUBLIC),
            refs={k: v for k, v in ov.items() if k not in (STARTER, HIDDEN, PUBLIC)},
        )
        ctx = job.work / "images"
        ctx.mkdir(parents=True, exist_ok=True)
        log = ctx / "build.log"
        pm = Podman(job.work / "tmp")
        layers = build_layers(t, ctx, pm, log)
        x = build_task_images(t, item, layers, ctx, pm, log)
        cleanup = [x.candidate, x.grader]
        try:
            verify_task_images(x, ctx, ctx / "samples", pm)
            if x.offline_test != "pass":
                raise StepFailed(
                    f"images: 응시용 이미지에서 오프라인 기존 테스트 실패 ({x.offline_test})"
                )
            starter = (x.grades.get("starter-only") or {}).get("result") or {}
            if starter.get("setup") != "ok":
                raise StepFailed(
                    f"images: 시작 코드 채점이 기동하지 않음 (setup={starter.get('setup')})"
                )
            leaks = self._scan(x, pm, log, job.work)
            if leaks:  # 경로는 숨은 테스트 파일 이름이라 사유에는 개수만
                raise StepFailed(f"images: 응시용 레이어에 금지 경로 {len(leaks)}개")
            verification = {
                name: {
                    "summary": (g["result"] or {}).get("summary"),
                    "cases": (g["result"] or {}).get("cases") or [],
                }
                for name, g in x.grades.items()
            }
            expected = summaries(verification).get("starter-only")
            if expected is None:
                raise StepFailed("images: 시작 코드만 채점한 결과(starter-only)가 없다")
            pkg = PackageInputs(
                header=PackageHeader(
                    mock=False,
                    approval="pending",
                    note=PENDING_NOTE,
                    common_id=job.build.common_id,
                    revision=job.label.revision or 1,
                    build_id=job.build.build_id,
                    template_ref=t.manifest.ref,
                    ontology_version=job.build.ontology_version,
                    tree_sha256=x.tree,
                    title=job.task.title,
                ),
                candidate=ImageRef(x.candidate, self._image_id(pm, x.candidate)),
                grader=ImageRef(x.grader, self._image_id(pm, x.grader)),
                readme=b.readme,
                workspace=t.manifest.workspace,
                commands=t.manifest.commands.model_dump(exclude_none=True)
                if t.manifest.commands
                else {},
                grader_sha256=x.grader_sha256,
                timeout_s=int(x.config["timeout_s"]),
                case_ids=[str(k["id"]) for k in x.config["cases"]],
                starter_expected=expected,
            )
        except BaseException:
            self._rmi(cleanup)
            raise
        return ImagesResult(
            template_ref=t.manifest.ref,
            manifest_sha256=t.manifest_sha256,
            tree_sha256=x.tree,
            grader_sha256=x.grader_sha256,
            verification=summaries(verification),
            cleanup=[
                *cleanup,
                *(f"localhost/task/{job.build.build_id}-{r}" for r in ("candidate", "grader")),
            ],
            package=pkg,
        )

    @staticmethod
    def _image_id(pm: Podman, name: str) -> str:
        insp = json.loads(pm("image", "inspect", name))[0]
        return "sha256:" + insp["Id"].removeprefix("sha256:")

    @staticmethod
    def _scan(x: TaskImages, pm: Podman, log: Path, work: Path) -> list[str]:
        """응시용 이미지를 저장해 레이어마다 금지 경로(숨은 테스트·하네스·manifest)를 찾는다 — 위 레이어에서 지워도 아래에 남는다."""
        tar = work / "scan" / "candidate.tar"
        tar.parent.mkdir(parents=True, exist_ok=True)
        pm("save", "--format", "docker-archive", "-o", str(tar), x.candidate, log=log)
        try:
            return scan_candidates(tar, {x.candidate: x.template}).get(x.candidate, [])
        finally:
            tar.unlink(missing_ok=True)

    # ④ 등록
    async def register(
        self, store: BuildRepo, job: Job, c: ContractResult, b: BuildResult, i: ImagesResult
    ) -> None:
        if not isinstance(i.package, PackageInputs):
            raise StepFailed("package: 묶음 재료 없음")
        await register_packages(store, self.images_root, job.build, i.package)

    async def cleanup(self, images: list[str]) -> None:
        await asyncio.to_thread(self._rmi, images)

    @staticmethod
    def _rmi(images: list[str]) -> None:
        """이 빌드가 만든 과제 이미지 이름만 지운다 — 템플릿·검증용 층은 다음 빌드가 다시 쓴다."""
        if images:
            subprocess.run(["podman", "rmi", "-f", *images], capture_output=True, check=False)
