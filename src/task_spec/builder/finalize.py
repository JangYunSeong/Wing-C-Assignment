"""묶음 확정 (#130 ③ A) — 승인된 빌드의 묶음을 builder 가 마무리한다 [확정: 2026-11-12 사용자].

승인 그래프(API)는 DB 에 승인만 기록한다. builder 는 "approved 인데 묶음이 아직 확정 전"인 빌드를 찾아
1. 응시용·채점용 descriptor 를 `approval=approved` 로 다시 쓰고 SHA256SUMS 를 갱신 (이미지 tar·README 는 그대로 — 저장소가 검사)
2. 평가 키트(`kit.json` — 숨은 케이스·숨김 수용 기준·과정 기준·승인자 판단)를 만들어 `eval_kit/` 에 쓰고
3. 파일 목록 갱신·키트 등록·확정 시각을 한 번에 저장한다 (`store.finalize_packages`). 전달은 확정된 빌드만.

파일 주인(builder 전용 사용자)·템플릿(실패 사유 설명)이 builder 쪽에 있어서 여기서 한다 — API 는 묶음 폴더를 읽기만.
다시 해도 같은 결과다 (이미 approved 인 descriptor 는 그대로). 실패하면 상태는 두고 사유만 남긴다 (`note_error`, 사람이 확인).
고정 온톨로지 검사(`+dirty` 금지)는 approved descriptor 를 만들 때 묶음 머리 규칙이 건다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from task_spec.schemas import TaskItem
from task_spec.schemas.build_record import BuildArtifact, BuildRecord
from task_spec.schemas.package import ArtifactFile, CandidateDescriptor, GraderDescriptor
from task_spec.starter.artifacts import write_role
from task_spec.starter.kit import build_kit
from task_spec.starter.store import BuildRepo, role_dir
from task_spec.starter.templates import load_template

logger = logging.getLogger(__name__)

APPROVED_NOTE = "시작 코드 승인을 받은 묶음 — 전달 가능"


class FinalizeError(Exception):
    """확정할 수 없음 — 메시지는 저장소 사유로 (짧게, 원문 없이)."""


def _template_reasons(template_ref: str) -> dict[str, str]:
    """템플릿 실패 사유 코드 → 설명 (평가 키트의 사유 설명)."""
    tid, _, ver = template_ref.rpartition("@v")
    t = load_template(tid, int(ver))
    return {r.code: r.description for r in t.manifest.failure_reasons}


def _approved(model: Any, cls: type[Any]) -> Any:
    try:
        return cls.model_validate(
            {**model.model_dump(), "approval": "approved", "note": APPROVED_NOTE}
        )
    except ValidationError as exc:
        why = "; ".join(str(e["msg"]) for e in exc.errors())[:200]
        raise FinalizeError(f"finalize: approved 묶음 머리 규칙 — {why}") from exc


def write_final_packages(
    root: Path, build: BuildRecord, task: TaskItem
) -> tuple[list[ArtifactFile], list[ArtifactFile], list[ArtifactFile]]:
    """파일 쓰기 (동기 — 스레드에서). (응시용·채점용·평가 키트 파일 목록)."""
    o = build.outcome
    if o is None:
        raise FinalizeError("finalize: 결과(outcome)가 없다")
    cdir = role_dir(root, build.build_id, "candidate")
    gdir = role_dir(root, build.build_id, "grader")
    try:
        cand = CandidateDescriptor.model_validate_json((cdir / "candidate.json").read_text("utf-8"))
        grad = GraderDescriptor.model_validate_json((gdir / "grader.json").read_text("utf-8"))
    except (OSError, ValidationError) as exc:
        raise FinalizeError(
            f"finalize: 묶음 descriptor 를 읽지 못함 ({type(exc).__name__})"
        ) from exc
    if cand.mock or grad.mock:
        raise FinalizeError("finalize: 목 판 묶음은 확정하지 않는다")
    if cand.build_id != build.build_id or cand.tree_sha256 != o.tree_sha256:
        raise FinalizeError("finalize: descriptor 가 이 빌드의 지금 결과가 아니다")
    cand2 = _approved(cand, CandidateDescriptor)
    grad2 = _approved(grad, GraderDescriptor)
    cfiles = write_role(cdir, "candidate.json", cand2.model_dump_json(indent=2))
    gfiles = write_role(gdir, "grader.json", grad2.model_dump_json(indent=2))
    cases = [
        {
            "id": c.case_id,
            "kind": "hidden",
            "trap_id": c.trap_id,
            "intended_reason": c.intended_reason,
        }
        for c in o.hidden_plan.cases
    ]
    try:
        kit = build_kit(
            task,
            cand2,
            grad2,
            cases,
            o.verification,
            _template_reasons(o.template_ref),
            o.review_flags,
            review_decisions=build.review_decisions,
        )
    except ValidationError as exc:
        why = "; ".join(str(e["msg"]) for e in exc.errors())[:200]
        raise FinalizeError(f"finalize: 평가 키트 규칙 — {why}") from exc
    kdir = role_dir(root, build.build_id, "eval_kit")
    kdir.mkdir(parents=True, exist_ok=True)
    kfiles = write_role(kdir, "kit.json", kit.model_dump_json(indent=2))
    return cfiles, gfiles, kfiles


class PackageFinalizer:
    def __init__(self, store: BuildRepo, images_root: Path) -> None:
        self.store, self.images_root = store, images_root

    async def finalize(self, build: BuildRecord) -> BuildRecord:
        """빌드 하나 확정. 실패하면 사유만 남기고 그 빌드를 돌려준다 (상태는 approved 그대로)."""
        try:
            task = await self.store.task(build.common_id)
            if task is None:
                raise FinalizeError("finalize: 공통 과제 명세 없음")
            cfiles, gfiles, kfiles = await asyncio.to_thread(
                write_final_packages, self.images_root, build, task
            )
            o = build.outcome
            assert o is not None
            kit = BuildArtifact(
                build_id=build.build_id,
                role="eval_kit",
                files=kfiles,
                image_id=None,
                tree_sha256=o.tree_sha256,
                grader_sha256=o.grader_sha256,
                created_at=datetime.now(UTC),
            )
            done = await self.store.finalize_packages(build.build_id, cfiles, gfiles, kit)
            logger.info("묶음 확정: %s", build.build_id, extra={"build_id": build.build_id})
            return done
        except FinalizeError as e:
            reason = str(e)
        except Exception as e:  # 저장소 규칙·파일 오류 — 사유는 종류만
            reason = f"finalize: {type(e).__name__}"
        logger.error("묶음 확정 실패: %s (%s)", build.build_id, reason.split(" — ")[0])
        return await self.store.note_error(build.build_id, reason)

    async def run_pending(self) -> int:
        """확정 전 빌드를 모두 — 확정한 수."""
        done = 0
        for b in await self.store.pending_finalize():
            if (await self.finalize(b)).packages_final_at is not None:
                done += 1
        return done
