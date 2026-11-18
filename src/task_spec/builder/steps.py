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
from collections.abc import Mapping
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
from task_spec.nodes.starter.plan_contract import PlanResult, plan_contract
from task_spec.prompting import PromptRegistry
from task_spec.schemas.build_record import BuildFailure
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
from task_spec.starter.render import render_readme, thin_readme
from task_spec.starter.repair import STARTER
from task_spec.starter.static_check import Overlay
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
            raise StepFailed(
                f"contract: 계약 생성 실패 — {contract_failure(res)}",
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
            await asyncio.to_thread(keep_overlays, out.overlays, job.work / FAILED_OVERLAYS)
            last = out.repairs[-1] if out.repairs else None
            # 수리 사유(reason)에는 숨은 테스트 단언 문구가 올 수 있다 — 관문·대상·결과만
            why = (
                f"{last.gate} {last.target} {last.result}" if last is not None else "수리 기록 없음"
            )
            raise StepFailed(
                f"build: 수리 루프 실패 (회차 {out.rounds}) — {why}",
                cost_usd=out.cost_usd,
                # 사유 문장(숨은 테스트 문구가 올 수 있음)은 저장소 사유가 아니라 실패 빌드 기록(DB)에만
                detail=BuildFailure(
                    hidden_plan=out.hidden_plan or c.plan,
                    review_flags=list(out.review_flags),
                    repairs=list(out.repairs),
                ),
            )
        starter = out.overlays[STARTER].files
        readme = render_readme(job.task, c.contract, c.template, starter=starter)
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
            review_flags=list(out.review_flags),
            repairs=list(out.repairs),
            cost_usd=out.cost_usd,
            readme_full=full.content if full is not None else None,
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
