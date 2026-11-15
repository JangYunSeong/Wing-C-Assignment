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
import subprocess
from pathlib import Path

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
from task_spec.nodes.starter.plan_contract import plan_contract
from task_spec.prompting import PromptRegistry
from task_spec.schemas.package import PackageHeader
from task_spec.spec.incidents import feature_incident, incident_by_id, load_incidents
from task_spec.starter.artifacts import summaries
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
from task_spec.starter.render import render_readme
from task_spec.starter.repair import STARTER
from task_spec.starter.store import BuildRepo
from task_spec.starter.templates import select_template
from task_spec.starter.verify import verifier_for

PENDING_NOTE = "승인 전 묶음 — 시작 코드 승인(#130) 뒤에만 전달한다"


def _cost(provenance: list[object]) -> float:
    return round(sum(getattr(p, "cost_usd", None) or 0.0 for p in provenance), 4)


def _kinds(items: list[str], n: int = 2) -> str:
    """문제 문장의 종류만 (첫 ' — '·':' 앞) — 뒤에는 명세의 숨은 요구·테스트 문구가 올 수 있어 사유에 넣지 않는다
    (2026-11-12 스모크: 계약 실패 사유에 숨은 요구 문장이 그대로 들어갔다)."""
    heads = dict.fromkeys(" ".join(x.split(" — ")[0].split(":")[0].split())[:60] for x in items)
    return " | ".join(list(heads)[:n])


class RealSteps:
    def __init__(
        self,
        llm: LLMClient,
        prompts: PromptRegistry,
        incidents_dir: Path,
        images_root: Path,
        designs: int | None = None,  # None = 허용 설계 전부 (#171)
    ) -> None:
        self.llm, self.prompts = llm, prompts
        self.incidents = load_incidents(incidents_dir)
        self.images_root = images_root
        self.designs = designs

    # ① 계약
    async def contract(self, job: Job) -> ContractResult:
        task = job.task
        sub = (task.source_incident or "").split(":")[1] if task.source_incident else ""
        template = select_template(sub) if sub else None
        if template is None:
            raise StepFailed(f"contract: 템플릿이 없는 직군 ({sub or '재료 없음'})")
        inc = incident_by_id(self.incidents, task.source_incident) or feature_incident(
            self.incidents, sub, job.label.posting_id
        )
        if inc is None or inc.feature is None:
            raise StepFailed(f"contract: 기능형 재료가 없는 직군 ({sub})")
        res = await plan_contract(
            task,
            template,
            llm=self.llm,
            prompts=self.prompts,
            ontology_version=job.build.ontology_version,
            seed=inc.feature,
        )
        if not res.ok or res.contract is None or res.hidden_plan is None:
            why = res.fail_reasons or res.spec_gaps or res.problems
            raise StepFailed(
                f"contract: 계약 생성 실패 — {_kinds(why) or '사유 없음'}",
                cost_usd=_cost(list(res.provenance)),
            )
        return ContractResult(
            template=template,
            contract=res.contract,
            plan=res.hidden_plan,
            cost_usd=_cost(list(res.provenance)),
            seed=inc.feature,
            avoid_terms=tuple(inc.avoid_terms),
            review_flags=tuple(res.flags),
        )

    # ② 빌드
    async def build(self, job: Job, c: ContractResult) -> BuildResult:
        b = BuildInputs(
            task=job.task,
            contract=c.contract,
            plan=c.plan,
            template=c.template,
            seed=c.seed,
            avoid_terms=c.avoid_terms,
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
            last = out.repairs[-1] if out.repairs else None
            # 수리 사유(reason)에는 숨은 테스트 단언 문구가 올 수 있다 — 관문·대상·결과만
            why = (
                f"{last.gate} {last.target} {last.result}" if last is not None else "수리 기록 없음"
            )
            raise StepFailed(
                f"build: 수리 루프 실패 (회차 {out.rounds}) — {why}", cost_usd=out.cost_usd
            )
        readme = render_readme(
            job.task, c.contract, c.template, starter=out.overlays[STARTER].files
        )
        return BuildResult(
            overlays=dict(out.overlays),
            plan=out.hidden_plan or c.plan,
            readme=readme.content,
            review_flags=list(out.review_flags),
            repairs=list(out.repairs),
            cost_usd=out.cost_usd,
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
