"""공통 과제 빌드 저장소 — 빌드·오버레이 파일·전달 묶음·전달 대상·전달 기록 (#52·#85) [제안].

테이블: `common_task_build` → `starter_file` · `build_artifact` → `artifact_delivery` ← `delivery_target`.
묶음 파일은 `<images_root>/<build_id>/<role>/` (`Settings.images_root` — 기본 `data/images`, 운영은 지원자 데이터와 다른 볼륨).

규칙 (두 구현이 같은 함수로 확인한다):
- 상태: pending → generating → verifying → review → approved → published (실패 failed).
  수리는 `reopen()` 으로만 verifying·review → generating — 그 빌드의 승인 전 묶음을 지우고 돌려준다 (파일 정리용).
- 오버레이 파일은 pending·generating 에서만 바꾼다. 생성·검증 결과(outcome)는 generating·verifying 에서만 쓴다.
- 묶음: 응시용·채점용은 review 에서, 평가 키트는 approved 에서(승인자 판단이 들어가므로) 등록한다.
  묶음의 tree_sha256·grader_sha256 은 빌드의 현재 outcome 과 같아야 하고, 한 번 등록하면 바꾸지 않는다.
- 승인은 review 에서만, 응시용·채점용 묶음이 현재 outcome 과 맞고 검토 표시마다 승인자 판단(flag_id)이 있어야 한다.
  승인 자체는 그래프 interrupt 로 받은 결과를 적는 것 (불변식 9).
- 전달은 approved·published 빌드만, 등록된 대상이 그 역할을 받을 때만. 첫 전달이 성공하면 published.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from task_spec.schemas.build import BuildStatus, FileKind, HiddenPlan, RepairRecord, TaskContract
from task_spec.schemas.build_record import (
    BuildArtifact,
    BuildOutcome,
    BuildRecord,
    Delivery,
    DeliveryTarget,
    StarterFile,
)
from task_spec.schemas.package import (
    BUILD_ID,
    ArtifactFile,
    ArtifactRole,
    ReviewDecision,
    VerifySummary,
    flag_id,
)
from task_spec.spec.store import DDL as COMMON_TASK_DDL

# set_status 로 바꿀 수 있는 다음 상태. 수리(→ generating)는 reopen(), approved 는 approve(), published 는 finish_delivery().
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"generating", "failed"},
    "generating": {"verifying", "failed"},
    "verifying": {"review", "failed"},
    "review": {"failed"},
    "approved": set(),
    "published": set(),
    "failed": set(),
}
_FILES_OPEN = ("pending", "generating")
_OUTCOME_OPEN = ("generating", "verifying")
_REOPENABLE = ("verifying", "review")
_ARTIFACT_OPEN: dict[str, tuple[str, ...]] = {
    "candidate": ("review",),
    "grader": ("review",),
    "eval_kit": ("approved",),
}
_APPROVAL_NEEDS: tuple[ArtifactRole, ...] = ("candidate", "grader")
_DELIVERABLE = ("approved", "published")

DDL = """
CREATE TABLE IF NOT EXISTS public.common_task_build (
    id text PRIMARY KEY,
    common_id text NOT NULL REFERENCES public.common_task (id) ON DELETE RESTRICT,
    seq integer NOT NULL CHECK (seq >= 1),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending', 'generating', 'verifying', 'review', 'failed', 'approved', 'published')),
    ontology_version text NOT NULL,
    template_ref text,
    template_sha256 text,
    tree_sha256 text,
    grader_sha256 text,
    contract jsonb,
    hidden_plan jsonb,
    verification jsonb NOT NULL DEFAULT '{}'::jsonb,
    review_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    review_decisions jsonb NOT NULL DEFAULT '[]'::jsonb,
    repairs jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    approved_by text,
    approved_at timestamptz,
    retain_until timestamptz,
    UNIQUE (common_id, seq)
);
CREATE TABLE IF NOT EXISTS public.starter_file (
    build_id text NOT NULL REFERENCES public.common_task_build (id) ON DELETE RESTRICT,
    kind text NOT NULL CHECK (kind IN ('starter', 'hidden', 'public_test', 'ref_naive', 'ref_accepted')),
    variant text NOT NULL DEFAULT '',
    path text NOT NULL,
    content text NOT NULL,
    sha256 text NOT NULL,
    size integer NOT NULL CHECK (size >= 0),
    PRIMARY KEY (build_id, kind, variant, path)
);
CREATE TABLE IF NOT EXISTS public.build_artifact (
    build_id text NOT NULL REFERENCES public.common_task_build (id) ON DELETE RESTRICT,
    role text NOT NULL CHECK (role IN ('candidate', 'grader', 'eval_kit')),
    files jsonb NOT NULL,
    image_id text,
    tree_sha256 text NOT NULL,
    grader_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (build_id, role),
    CHECK ((role = 'eval_kit') = (image_id IS NULL))
);
CREATE TABLE IF NOT EXISTS public.delivery_target (
    name text PRIMARY KEY,
    role text NOT NULL CHECK (role IN ('candidate', 'grader', 'eval_kit')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.artifact_delivery (
    id text PRIMARY KEY,
    build_id text NOT NULL,
    role text NOT NULL,
    target text NOT NULL REFERENCES public.delivery_target (name) ON DELETE RESTRICT,
    requested_by text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'sent', 'failed')),
    files jsonb NOT NULL,
    sent_at timestamptz,
    error text,
    FOREIGN KEY (build_id, role) REFERENCES public.build_artifact (build_id, role) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS artifact_delivery_build ON public.artifact_delivery (build_id);
"""


@dataclass(frozen=True)
class TaskLabel:
    """화면 목록용 공통 과제 이름표 — 제목·공고 id 만 (명세 본문은 넣지 않는다)."""

    title: str | None
    posting_id: str


class BuildRuleError(ValueError):
    """저장소 규칙 위반 (상태·승인·불변·전달 대상)."""


# ── 경로 ──────────────────────────────────────────────────────────────


def build_dir(images_root: Path, build_id: str) -> Path:
    """`<images_root>/<build_id>` — build_id 패턴을 확인한 뒤에만 경로를 만든다."""
    if not re.match(BUILD_ID, build_id):
        raise BuildRuleError(f"build_id 형식이 아니다: {build_id!r}")
    return images_root / build_id


def role_dir(images_root: Path, build_id: str, role: ArtifactRole) -> Path:
    return build_dir(images_root, build_id) / role


def archive_name(build_id: str, role: ArtifactRole) -> str:
    if role == "eval_kit":
        raise BuildRuleError("eval_kit 에는 이미지 파일이 없다")
    return f"{build_id}-{role}.tar.zst"


def sha256sums(files: Iterable[ArtifactFile]) -> str:
    """SHA256SUMS 본문 (`sha256sum -c` 형식, 이름순)."""
    return "".join(f"{f.sha256}  {f.name}\n" for f in sorted(files, key=lambda f: f.name))


# ── 규칙 (두 구현 공통) ──────────────────────────────────────────────


def check_transition(build: BuildRecord, new: BuildStatus) -> None:
    if new not in TRANSITIONS[build.status]:
        hint = " (수리는 reopen)" if new == "generating" else ""
        raise BuildRuleError(f"{build.build_id}: {build.status} → {new} 은 허용되지 않는다{hint}")


def check_artifact_current(build: BuildRecord, artifact: BuildArtifact) -> None:
    """묶음이 빌드의 현재 생성·검증 결과에서 나온 것인지 — 수리 뒤 낡은 묶음을 승인·전달하지 않게."""
    o = build.outcome
    if o is None:
        raise BuildRuleError(f"{build.build_id}: 생성·검증 결과(outcome)가 없다")
    if (artifact.tree_sha256, artifact.grader_sha256) != (o.tree_sha256, o.grader_sha256):
        raise BuildRuleError(
            f"{build.build_id}/{artifact.role}: 묶음이 현재 결과와 다르다 (tree·grader 해시)"
        )


def check_artifact_open(build: BuildRecord, artifact: BuildArtifact) -> None:
    _require_status(build, _ARTIFACT_OPEN[artifact.role], f"{artifact.role} 묶음 등록")
    check_artifact_current(build, artifact)


def check_approval(
    build: BuildRecord, decisions: list[ReviewDecision], artifacts: list[BuildArtifact]
) -> None:
    if build.status != "review":
        raise BuildRuleError(f"{build.build_id}: review 상태에서만 승인 (지금 {build.status})")
    if build.outcome is None:
        raise BuildRuleError(f"{build.build_id}: 생성·검증 결과(outcome)가 없다")
    by_role = {a.role: a for a in artifacts}
    for role in _APPROVAL_NEEDS:
        if role not in by_role:
            raise BuildRuleError(
                f"{build.build_id}: {role} 묶음이 없다 — 승인할 이미지를 먼저 등록"
            )
        check_artifact_current(build, by_role[role])
    flags = {flag_id(f) for f in build.outcome.review_flags}
    decided = {d.flag_id for d in decisions}
    if len(flags - decided):
        raise BuildRuleError(
            f"{build.build_id}: 승인자 판단이 없는 검토 표시 {len(flags - decided)}건"
        )
    if len(decided - flags):
        raise BuildRuleError(
            f"{build.build_id}: 없는 검토 표시에 대한 판단 {len(decided - flags)}건"
        )


def check_target(target: DeliveryTarget | None, name: str, role: ArtifactRole) -> None:
    if target is None:
        raise BuildRuleError(f"등록되지 않은 전달 대상: {name}")
    if target.role != role:
        raise BuildRuleError(f"전달 대상 {name} 은 {target.role} 만 받는다 ({role} 요청)")


def _require(build: BuildRecord | None, build_id: str) -> BuildRecord:
    if build is None:
        raise KeyError(build_id)
    return build


def _require_status(build: BuildRecord, allowed: tuple[str, ...], what: str) -> None:
    if build.status not in allowed:
        raise BuildRuleError(
            f"{build.build_id}: {what} 는 {'/'.join(allowed)} 상태에서만 (지금 {build.status})"
        )


def _new_delivery_id() -> str:
    return f"dl-{uuid.uuid4().hex[:12]}"


# ── 저장소 ───────────────────────────────────────────────────────────


class BuildRepo(Protocol):
    async def new_build(
        self, common_id: str, ontology_version: str, at: datetime | None = None
    ) -> BuildRecord: ...

    async def get(self, build_id: str) -> BuildRecord | None: ...

    async def builds(self, common_id: str) -> list[BuildRecord]: ...

    async def recent(self, limit: int) -> list[BuildRecord]: ...

    async def task_labels(self, common_ids: Iterable[str]) -> dict[str, TaskLabel]: ...

    async def set_status(self, build_id: str, status: BuildStatus) -> BuildRecord: ...

    async def reopen(self, build_id: str) -> list[BuildArtifact]: ...

    async def save_outcome(self, build_id: str, outcome: BuildOutcome) -> BuildRecord: ...

    async def put_files(self, build_id: str, files: list[StarterFile]) -> None: ...

    async def files(
        self, build_id: str, kinds: Iterable[FileKind] | None = None
    ) -> list[StarterFile]: ...

    async def approve(
        self, build_id: str, reviewer: str, at: datetime, decisions: list[ReviewDecision]
    ) -> BuildRecord: ...

    async def put_artifact(self, artifact: BuildArtifact) -> None: ...

    async def artifacts(self, build_id: str) -> list[BuildArtifact]: ...

    async def add_target(
        self, name: str, role: ArtifactRole, at: datetime | None = None
    ) -> DeliveryTarget: ...

    async def targets(self) -> list[DeliveryTarget]: ...

    async def request_delivery(
        self,
        build_id: str,
        role: ArtifactRole,
        target: str,
        requested_by: str,
        at: datetime | None = None,
    ) -> Delivery: ...

    async def finish_delivery(
        self, delivery_id: str, ok: bool, at: datetime, error: str | None = None
    ) -> Delivery: ...

    async def deliveries(self, build_id: str) -> list[Delivery]: ...


class InMemoryBuildStore:
    """단위 테스트·DB 없는 실행용. 공통 과제 행 존재는 확인하지 않는다 (FK 는 Postgres 에서)."""

    def __init__(self) -> None:
        self.labels: dict[str, TaskLabel] = {}  # 공통 과제 행 대신 (테스트가 채움)
        self._builds: dict[str, BuildRecord] = {}
        self._files: dict[tuple[str, str, str, str], StarterFile] = {}
        self._artifacts: dict[tuple[str, str], BuildArtifact] = {}
        self._targets: dict[str, DeliveryTarget] = {}
        self._deliveries: dict[str, Delivery] = {}

    def _put(self, build: BuildRecord, **update: Any) -> BuildRecord:
        new = BuildRecord.model_validate({**build.model_dump(), **update})
        self._builds[new.build_id] = new
        return new

    async def new_build(
        self, common_id: str, ontology_version: str, at: datetime | None = None
    ) -> BuildRecord:
        seq = 1 + max((b.seq for b in self._builds.values() if b.common_id == common_id), default=0)
        build = BuildRecord(
            build_id=f"{common_id}-b{seq}",
            common_id=common_id,
            seq=seq,
            status="pending",
            ontology_version=ontology_version,
            created_at=at or datetime.now(UTC),
        )
        self._builds[build.build_id] = build
        return build

    async def get(self, build_id: str) -> BuildRecord | None:
        return self._builds.get(build_id)

    async def builds(self, common_id: str) -> list[BuildRecord]:
        return sorted(
            (b for b in self._builds.values() if b.common_id == common_id), key=lambda b: b.seq
        )

    async def recent(self, limit: int) -> list[BuildRecord]:
        rows = sorted(self._builds.values(), key=lambda b: (b.created_at, b.build_id), reverse=True)
        return rows[:limit]

    async def task_labels(self, common_ids: Iterable[str]) -> dict[str, TaskLabel]:
        return {c: self.labels[c] for c in common_ids if c in self.labels}

    async def set_status(self, build_id: str, status: BuildStatus) -> BuildRecord:
        build = _require(self._builds.get(build_id), build_id)
        check_transition(build, status)
        return self._put(build, status=status)

    async def reopen(self, build_id: str) -> list[BuildArtifact]:
        build = _require(self._builds.get(build_id), build_id)
        _require_status(build, _REOPENABLE, "수리(reopen)")
        removed = [a for (b, _), a in self._artifacts.items() if b == build_id]
        for a in removed:
            del self._artifacts[(build_id, a.role)]
        self._put(build, status="generating")
        return sorted(removed, key=lambda a: a.role)

    async def save_outcome(self, build_id: str, outcome: BuildOutcome) -> BuildRecord:
        build = _require(self._builds.get(build_id), build_id)
        _require_status(build, _OUTCOME_OPEN, "결과 저장")
        return self._put(build, outcome=outcome)

    async def put_files(self, build_id: str, files: list[StarterFile]) -> None:
        build = _require(self._builds.get(build_id), build_id)
        _require_status(build, _FILES_OPEN, "오버레이 파일 저장")
        for f in files:
            if f.build_id != build_id:
                raise BuildRuleError(f"다른 빌드의 파일: {f.build_id}")
            self._files[(build_id, f.kind, f.variant, f.path)] = f

    async def files(
        self, build_id: str, kinds: Iterable[FileKind] | None = None
    ) -> list[StarterFile]:
        want = set(kinds) if kinds is not None else None
        got = [
            f
            for (b, *_), f in self._files.items()
            if b == build_id and (want is None or f.kind in want)
        ]
        return sorted(got, key=lambda f: (f.kind, f.variant, f.path))

    async def approve(
        self, build_id: str, reviewer: str, at: datetime, decisions: list[ReviewDecision]
    ) -> BuildRecord:
        build = _require(self._builds.get(build_id), build_id)
        check_approval(build, decisions, await self.artifacts(build_id))
        return self._put(
            build,
            status="approved",
            approved_by=reviewer,
            approved_at=at,
            review_decisions=[d.model_dump() for d in decisions],
        )

    async def put_artifact(self, artifact: BuildArtifact) -> None:
        build = _require(self._builds.get(artifact.build_id), artifact.build_id)
        check_artifact_open(build, artifact)
        key = (artifact.build_id, artifact.role)
        if key in self._artifacts:
            raise BuildRuleError(
                f"{artifact.build_id}/{artifact.role}: 이미 등록된 묶음은 바꾸지 않는다"
            )
        self._artifacts[key] = artifact

    async def artifacts(self, build_id: str) -> list[BuildArtifact]:
        return sorted(
            (a for (b, _), a in self._artifacts.items() if b == build_id), key=lambda a: a.role
        )

    async def add_target(
        self, name: str, role: ArtifactRole, at: datetime | None = None
    ) -> DeliveryTarget:
        got = self._targets.get(name)
        if got is not None:
            if got.role != role:
                raise BuildRuleError(f"전달 대상 {name} 은 이미 {got.role} 용이다")
            return got
        t = DeliveryTarget(name=name, role=role, created_at=at or datetime.now(UTC))
        self._targets[name] = t
        return t

    async def targets(self) -> list[DeliveryTarget]:
        return sorted(self._targets.values(), key=lambda t: t.name)

    async def request_delivery(
        self,
        build_id: str,
        role: ArtifactRole,
        target: str,
        requested_by: str,
        at: datetime | None = None,
    ) -> Delivery:
        build = _require(self._builds.get(build_id), build_id)
        _require_status(build, _DELIVERABLE, "전달")
        check_target(self._targets.get(target), target, role)
        artifact = self._artifacts.get((build_id, role))
        if artifact is None:
            raise BuildRuleError(f"{build_id}/{role}: 등록된 묶음이 없다")
        check_artifact_current(build, artifact)
        d = Delivery(
            delivery_id=_new_delivery_id(),
            build_id=build_id,
            role=role,
            target=target,
            requested_by=requested_by,
            requested_at=at or datetime.now(UTC),
            files=artifact.files,
        )
        self._deliveries[d.delivery_id] = d
        return d

    async def finish_delivery(
        self, delivery_id: str, ok: bool, at: datetime, error: str | None = None
    ) -> Delivery:
        d = self._deliveries.get(delivery_id)
        if d is None:
            raise KeyError(delivery_id)
        if d.status != "requested":
            raise BuildRuleError(f"{delivery_id}: 이미 끝난 전달 ({d.status})")
        new = d.model_copy(
            update={
                "status": "sent" if ok else "failed",
                "sent_at": at if ok else None,
                "error": error,
            }
        )
        self._deliveries[delivery_id] = new
        build = self._builds[d.build_id]
        if ok and build.status == "approved":
            self._put(build, status="published")
        return new

    async def deliveries(self, build_id: str) -> list[Delivery]:
        return sorted(
            (d for d in self._deliveries.values() if d.build_id == build_id),
            key=lambda d: d.requested_at,
        )


class PgBuildStore:
    """Postgres 구현. `open()` 이 common_task·빌드 DDL 을 적용한다."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, conninfo: str) -> PgBuildStore:
        pool = AsyncConnectionPool(
            conninfo, min_size=1, max_size=4, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True)
        async with pool.connection() as conn:
            await conn.execute(COMMON_TASK_DDL)
            await conn.execute(DDL)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # ── 행 변환 ──
    @staticmethod
    def _build(r: dict[str, Any]) -> BuildRecord:
        outcome = None
        if r["template_ref"] is not None:
            outcome = BuildOutcome(
                template_ref=r["template_ref"],
                template_sha256=r["template_sha256"],
                tree_sha256=r["tree_sha256"],
                grader_sha256=r["grader_sha256"],
                contract=TaskContract.model_validate(r["contract"]),
                hidden_plan=HiddenPlan.model_validate(r["hidden_plan"]),
                verification={
                    k: VerifySummary.model_validate(v) for k, v in r["verification"].items()
                },
                review_flags=list(r["review_flags"]),
                repairs=[RepairRecord.model_validate(x) for x in r["repairs"]],
            )
        return BuildRecord(
            build_id=r["id"],
            common_id=r["common_id"],
            seq=r["seq"],
            status=r["status"],
            ontology_version=r["ontology_version"],
            outcome=outcome,
            review_decisions=[ReviewDecision.model_validate(x) for x in r["review_decisions"]],
            created_at=r["created_at"],
            approved_by=r["approved_by"],
            approved_at=r["approved_at"],
            retain_until=r["retain_until"],
        )

    @staticmethod
    def _artifact(r: dict[str, Any]) -> BuildArtifact:
        return BuildArtifact(
            build_id=r["build_id"],
            role=r["role"],
            files=[ArtifactFile.model_validate(x) for x in r["files"]],
            image_id=r["image_id"],
            tree_sha256=r["tree_sha256"],
            grader_sha256=r["grader_sha256"],
            created_at=r["created_at"],
        )

    @staticmethod
    def _delivery(r: dict[str, Any]) -> Delivery:
        return Delivery(
            delivery_id=r["id"],
            build_id=r["build_id"],
            role=r["role"],
            target=r["target"],
            requested_by=r["requested_by"],
            requested_at=r["requested_at"],
            status=r["status"],
            files=[ArtifactFile.model_validate(x) for x in r["files"]],
            sent_at=r["sent_at"],
            error=r["error"],
        )

    @staticmethod
    def _target(r: dict[str, Any]) -> DeliveryTarget:
        return DeliveryTarget(name=r["name"], role=r["role"], created_at=r["created_at"])

    async def _locked(self, cur: Any, build_id: str) -> BuildRecord:
        await cur.execute(
            "SELECT * FROM public.common_task_build WHERE id = %s FOR UPDATE", (build_id,)
        )
        r = await cur.fetchone()
        return _require(self._build(r) if r else None, build_id)

    async def _artifacts_of(self, cur: Any, build_id: str) -> list[BuildArtifact]:
        await cur.execute(
            "SELECT * FROM public.build_artifact WHERE build_id = %s ORDER BY role", (build_id,)
        )
        return [self._artifact(r) for r in await cur.fetchall()]

    # ── 빌드 ──
    async def new_build(
        self, common_id: str, ontology_version: str, at: datetime | None = None
    ) -> BuildRecord:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            for _ in range(3):  # 같은 판 동시 빌드: UNIQUE 위반이면 다시 번호를 매긴다
                try:
                    await cur.execute(
                        "INSERT INTO public.common_task_build (id, common_id, seq, ontology_version, created_at) "
                        "SELECT %(c)s || '-b' || n, %(c)s, n, %(o)s, COALESCE(%(at)s, now()) FROM "
                        "(SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM public.common_task_build "
                        " WHERE common_id = %(c)s) s RETURNING *",
                        {"c": common_id, "o": ontology_version, "at": at},
                    )
                    r = await cur.fetchone()
                    assert r is not None
                    return self._build(r)
                except Exception as exc:  # psycopg.errors.UniqueViolation
                    if type(exc).__name__ != "UniqueViolation":
                        raise
        raise RuntimeError("common_task_build: 빌드 번호 충돌이 계속됩니다")

    async def get(self, build_id: str) -> BuildRecord | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM public.common_task_build WHERE id = %s", (build_id,))
            r = await cur.fetchone()
        return self._build(r) if r else None

    async def builds(self, common_id: str) -> list[BuildRecord]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.common_task_build WHERE common_id = %s ORDER BY seq",
                (common_id,),
            )
            rows = await cur.fetchall()
        return [self._build(r) for r in rows]

    async def recent(self, limit: int) -> list[BuildRecord]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.common_task_build ORDER BY created_at DESC, id DESC LIMIT %s",
                (limit,),
            )
            rows = await cur.fetchall()
        return [self._build(r) for r in rows]

    async def task_labels(self, common_ids: Iterable[str]) -> dict[str, TaskLabel]:
        ids = sorted(set(common_ids))
        if not ids:
            return {}
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, posting_id, task->>'title' AS title FROM public.common_task "
                "WHERE id = ANY(%s)",
                (ids,),
            )
            rows = await cur.fetchall()
        return {r["id"]: TaskLabel(title=r["title"], posting_id=r["posting_id"]) for r in rows}

    async def set_status(self, build_id: str, status: BuildStatus) -> BuildRecord:
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            check_transition(await self._locked(cur, build_id), status)
            await cur.execute(
                "UPDATE public.common_task_build SET status = %s WHERE id = %s RETURNING *",
                (status, build_id),
            )
            r = await cur.fetchone()
        return self._build(r)  # type: ignore[arg-type]

    async def reopen(self, build_id: str) -> list[BuildArtifact]:
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            _require_status(await self._locked(cur, build_id), _REOPENABLE, "수리(reopen)")
            removed = await self._artifacts_of(cur, build_id)
            # 승인 전이라 전달 기록이 없다 — 있으면 FK(RESTRICT)가 막는다
            await cur.execute("DELETE FROM public.build_artifact WHERE build_id = %s", (build_id,))
            await cur.execute(
                "UPDATE public.common_task_build SET status = 'generating' WHERE id = %s",
                (build_id,),
            )
        return removed

    async def save_outcome(self, build_id: str, outcome: BuildOutcome) -> BuildRecord:
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            _require_status(await self._locked(cur, build_id), _OUTCOME_OPEN, "결과 저장")
            o = outcome.model_dump(mode="json")
            await cur.execute(
                "UPDATE public.common_task_build SET template_ref = %s, template_sha256 = %s, "
                "tree_sha256 = %s, grader_sha256 = %s, contract = %s, hidden_plan = %s, "
                "verification = %s, review_flags = %s, repairs = %s WHERE id = %s RETURNING *",
                (
                    o["template_ref"],
                    o["template_sha256"],
                    o["tree_sha256"],
                    o["grader_sha256"],
                    Jsonb(o["contract"]),
                    Jsonb(o["hidden_plan"]),
                    Jsonb(o["verification"]),
                    Jsonb(o["review_flags"]),
                    Jsonb(o["repairs"]),
                    build_id,
                ),
            )
            r = await cur.fetchone()
        return self._build(r)  # type: ignore[arg-type]

    # ── 오버레이 파일 ──
    async def put_files(self, build_id: str, files: list[StarterFile]) -> None:
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            _require_status(await self._locked(cur, build_id), _FILES_OPEN, "오버레이 파일 저장")
            for f in files:
                if f.build_id != build_id:
                    raise BuildRuleError(f"다른 빌드의 파일: {f.build_id}")
            await cur.executemany(
                "INSERT INTO public.starter_file (build_id, kind, variant, path, content, sha256, size) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (build_id, kind, variant, path) "
                "DO UPDATE SET content = EXCLUDED.content, sha256 = EXCLUDED.sha256, size = EXCLUDED.size",
                [(build_id, f.kind, f.variant, f.path, f.content, f.sha256, f.size) for f in files],
            )

    async def files(
        self, build_id: str, kinds: Iterable[FileKind] | None = None
    ) -> list[StarterFile]:
        want = list(kinds) if kinds is not None else None
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.starter_file WHERE build_id = %s "
                "AND (%s::text[] IS NULL OR kind = ANY(%s::text[])) ORDER BY kind, variant, path",
                (build_id, want, want),
            )
            rows = await cur.fetchall()
        return [StarterFile.model_validate(r) for r in rows]

    # ── 승인 ──
    async def approve(
        self, build_id: str, reviewer: str, at: datetime, decisions: list[ReviewDecision]
    ) -> BuildRecord:
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            build = await self._locked(cur, build_id)
            check_approval(build, decisions, await self._artifacts_of(cur, build_id))
            await cur.execute(
                "UPDATE public.common_task_build SET status = 'approved', approved_by = %s, "
                "approved_at = %s, review_decisions = %s WHERE id = %s RETURNING *",
                (reviewer, at, Jsonb([d.model_dump(mode="json") for d in decisions]), build_id),
            )
            r = await cur.fetchone()
        return self._build(r)  # type: ignore[arg-type]

    # ── 묶음 ──
    async def put_artifact(self, artifact: BuildArtifact) -> None:
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            check_artifact_open(await self._locked(cur, artifact.build_id), artifact)
            await cur.execute(
                "INSERT INTO public.build_artifact (build_id, role, files, image_id, tree_sha256, "
                "grader_sha256, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (build_id, role) DO NOTHING RETURNING role",
                (
                    artifact.build_id,
                    artifact.role,
                    Jsonb([f.model_dump(mode="json") for f in artifact.files]),
                    artifact.image_id,
                    artifact.tree_sha256,
                    artifact.grader_sha256,
                    artifact.created_at,
                ),
            )
            if await cur.fetchone() is None:
                raise BuildRuleError(
                    f"{artifact.build_id}/{artifact.role}: 이미 등록된 묶음은 바꾸지 않는다"
                )

    async def artifacts(self, build_id: str) -> list[BuildArtifact]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return await self._artifacts_of(cur, build_id)

    # ── 전달 대상 ──
    async def add_target(
        self, name: str, role: ArtifactRole, at: datetime | None = None
    ) -> DeliveryTarget:
        t = DeliveryTarget(name=name, role=role, created_at=at or datetime.now(UTC))  # 이름 검증
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO public.delivery_target (name, role, created_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (t.name, t.role, t.created_at),
            )
            await cur.execute("SELECT * FROM public.delivery_target WHERE name = %s", (name,))
            got = self._target(await cur.fetchone())  # type: ignore[arg-type]
        if got.role != role:
            raise BuildRuleError(f"전달 대상 {name} 은 이미 {got.role} 용이다")
        return got

    async def targets(self) -> list[DeliveryTarget]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM public.delivery_target ORDER BY name")
            rows = await cur.fetchall()
        return [self._target(r) for r in rows]

    # ── 전달 ──
    async def request_delivery(
        self,
        build_id: str,
        role: ArtifactRole,
        target: str,
        requested_by: str,
        at: datetime | None = None,
    ) -> Delivery:
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            build = await self._locked(cur, build_id)
            _require_status(build, _DELIVERABLE, "전달")
            await cur.execute("SELECT * FROM public.delivery_target WHERE name = %s", (target,))
            t = await cur.fetchone()
            check_target(self._target(t) if t else None, target, role)
            artifact = next(
                (a for a in await self._artifacts_of(cur, build_id) if a.role == role), None
            )
            if artifact is None:
                raise BuildRuleError(f"{build_id}/{role}: 등록된 묶음이 없다")
            check_artifact_current(build, artifact)
            await cur.execute(
                "INSERT INTO public.artifact_delivery (id, build_id, role, target, requested_by, "
                "requested_at, files) VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()), %s) RETURNING *",
                (
                    _new_delivery_id(),
                    build_id,
                    role,
                    target,
                    requested_by,
                    at,
                    Jsonb([f.model_dump(mode="json") for f in artifact.files]),
                ),
            )
            r = await cur.fetchone()
        return self._delivery(r)  # type: ignore[arg-type]

    async def finish_delivery(
        self, delivery_id: str, ok: bool, at: datetime, error: str | None = None
    ) -> Delivery:
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "UPDATE public.artifact_delivery SET status = %s, sent_at = %s, error = %s "
                "WHERE id = %s AND status = 'requested' RETURNING *",
                ("sent" if ok else "failed", at if ok else None, error, delivery_id),
            )
            r = await cur.fetchone()
            if r is None:
                await cur.execute(
                    "SELECT status FROM public.artifact_delivery WHERE id = %s", (delivery_id,)
                )
                prev = await cur.fetchone()
                if prev is None:
                    raise KeyError(delivery_id)
                raise BuildRuleError(f"{delivery_id}: 이미 끝난 전달 ({prev['status']})")
            if ok:
                await cur.execute(
                    "UPDATE public.common_task_build SET status = 'published' "
                    "WHERE id = %s AND status = 'approved'",
                    (r["build_id"],),
                )
        return self._delivery(r)

    async def deliveries(self, build_id: str) -> list[Delivery]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.artifact_delivery WHERE build_id = %s ORDER BY requested_at",
                (build_id,),
            )
            rows = await cur.fetchall()
        return [self._delivery(r) for r in rows]
