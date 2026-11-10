"""공통 과제 빌드 저장소 — 빌드·오버레이 파일·전달 묶음·전달 기록 (#52·#85) [제안].

테이블: `common_task_build` → `starter_file` · `build_artifact` → `artifact_delivery`. 묶음 파일은 `data/images/<build_id>/<role>/`.
규칙 (두 구현이 같은 함수로 확인한다):
- 상태: pending → generating → verifying → review → approved → published (실패 failed). 수리는 generating 으로 되돌린다.
- 오버레이 파일은 승인 전(pending·generating·verifying)에만 바꾼다. 승인된 빌드의 원본은 고정.
- 승인은 review 에서만, 검토 표시마다 승인자 판단이 있어야 한다. 승인 자체는 그래프 interrupt 로 받은 결과를 적는 것 (불변식 9).
- 묶음(역할 폴더)은 한 번 쓰면 바꾸지 않는다. 전달은 approved·published 빌드만, 첫 전달이 끝나면 published.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
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
    StarterFile,
)
from task_spec.schemas.package import (
    BUILD_ID,
    ArtifactFile,
    ArtifactRole,
    ReviewDecision,
    VerifySummary,
)
from task_spec.spec.store import DDL as COMMON_TASK_DDL

IMAGES_DIR = "images"

# 바꿀 수 있는 다음 상태. approved 는 approve(), published 는 finish_delivery() 로만.
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"generating", "failed"},
    "generating": {"verifying", "failed"},
    "verifying": {"review", "generating", "failed"},
    "review": {"generating", "failed"},
    "approved": set(),
    "published": set(),
    "failed": set(),
}
_FILES_OPEN = ("pending", "generating", "verifying")
_OUTCOME_OPEN = ("generating", "verifying", "review")
_ARTIFACT_OPEN = ("verifying", "review", "approved")
_DELIVERABLE = ("approved", "published")

DDL = """
CREATE TABLE IF NOT EXISTS public.common_task_build (
    id text PRIMARY KEY,
    common_id text NOT NULL REFERENCES public.common_task (id),
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
    build_id text NOT NULL REFERENCES public.common_task_build (id),
    kind text NOT NULL CHECK (kind IN ('starter', 'hidden', 'public_test', 'ref_naive', 'ref_accepted')),
    variant text NOT NULL DEFAULT '',
    path text NOT NULL,
    content text NOT NULL,
    sha256 text NOT NULL,
    size integer NOT NULL CHECK (size >= 0),
    PRIMARY KEY (build_id, kind, variant, path)
);
CREATE TABLE IF NOT EXISTS public.build_artifact (
    build_id text NOT NULL REFERENCES public.common_task_build (id),
    role text NOT NULL CHECK (role IN ('candidate', 'grader', 'eval_kit')),
    files jsonb NOT NULL,
    image_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (build_id, role),
    CHECK ((role = 'eval_kit') = (image_id IS NULL))
);
CREATE TABLE IF NOT EXISTS public.artifact_delivery (
    id text PRIMARY KEY,
    build_id text NOT NULL,
    role text NOT NULL,
    target text NOT NULL,
    requested_by text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'sent', 'failed')),
    files jsonb NOT NULL,
    sent_at timestamptz,
    error text,
    FOREIGN KEY (build_id, role) REFERENCES public.build_artifact (build_id, role)
);
CREATE INDEX IF NOT EXISTS artifact_delivery_build ON public.artifact_delivery (build_id);
"""


class BuildRuleError(ValueError):
    """저장소 규칙 위반 (상태·승인·불변)."""


# ── 경로 ──────────────────────────────────────────────────────────────


def build_dir(data_dir: Path, build_id: str) -> Path:
    """`<data_dir>/images/<build_id>` — build_id 패턴을 확인한 뒤에만 경로를 만든다."""
    if not re.match(BUILD_ID, build_id):
        raise BuildRuleError(f"build_id 형식이 아니다: {build_id!r}")
    return data_dir / IMAGES_DIR / build_id


def role_dir(data_dir: Path, build_id: str, role: ArtifactRole) -> Path:
    return build_dir(data_dir, build_id) / role


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
        raise BuildRuleError(f"{build.build_id}: {build.status} → {new} 은 허용되지 않는다")


def check_approval(build: BuildRecord, decisions: list[ReviewDecision]) -> None:
    if build.status != "review":
        raise BuildRuleError(f"{build.build_id}: review 상태에서만 승인 (지금 {build.status})")
    if build.outcome is None:
        raise BuildRuleError(f"{build.build_id}: 생성·검증 결과(outcome)가 없다")
    decided = {d.flag for d in decisions}
    missing = [f for f in build.outcome.review_flags if f not in decided]
    if missing:
        raise BuildRuleError(f"{build.build_id}: 승인자 판단이 없는 검토 표시 {len(missing)}건")
    unknown = sorted(decided - set(build.outcome.review_flags))
    if unknown:
        raise BuildRuleError(f"{build.build_id}: 없는 검토 표시에 대한 판단 {len(unknown)}건")


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

    async def set_status(self, build_id: str, status: BuildStatus) -> BuildRecord: ...

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
        self._builds: dict[str, BuildRecord] = {}
        self._files: dict[tuple[str, str, str, str], StarterFile] = {}
        self._artifacts: dict[tuple[str, str], BuildArtifact] = {}
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

    async def set_status(self, build_id: str, status: BuildStatus) -> BuildRecord:
        build = _require(self._builds.get(build_id), build_id)
        check_transition(build, status)
        return self._put(build, status=status)

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
        check_approval(build, decisions)
        return self._put(
            build,
            status="approved",
            approved_by=reviewer,
            approved_at=at,
            review_decisions=[d.model_dump() for d in decisions],
        )

    async def put_artifact(self, artifact: BuildArtifact) -> None:
        build = _require(self._builds.get(artifact.build_id), artifact.build_id)
        _require_status(build, _ARTIFACT_OPEN, "묶음 등록")
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
        artifact = self._artifacts.get((build_id, role))
        if artifact is None:
            raise BuildRuleError(f"{build_id}/{role}: 등록된 묶음이 없다")
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

    async def _locked(self, cur: Any, build_id: str) -> BuildRecord:
        await cur.execute(
            "SELECT * FROM public.common_task_build WHERE id = %s FOR UPDATE", (build_id,)
        )
        r = await cur.fetchone()
        return _require(self._build(r) if r else None, build_id)

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
            check_approval(await self._locked(cur, build_id), decisions)
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
            build = await self._locked(cur, artifact.build_id)
            _require_status(build, _ARTIFACT_OPEN, "묶음 등록")
            await cur.execute(
                "INSERT INTO public.build_artifact (build_id, role, files, image_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (build_id, role) DO NOTHING RETURNING role",
                (
                    artifact.build_id,
                    artifact.role,
                    Jsonb([f.model_dump(mode="json") for f in artifact.files]),
                    artifact.image_id,
                    artifact.created_at,
                ),
            )
            if await cur.fetchone() is None:
                raise BuildRuleError(
                    f"{artifact.build_id}/{artifact.role}: 이미 등록된 묶음은 바꾸지 않는다"
                )

    async def artifacts(self, build_id: str) -> list[BuildArtifact]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.build_artifact WHERE build_id = %s ORDER BY role", (build_id,)
            )
            rows = await cur.fetchall()
        return [self._artifact(r) for r in rows]

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
            _require_status(await self._locked(cur, build_id), _DELIVERABLE, "전달")
            await cur.execute(
                "SELECT files FROM public.build_artifact WHERE build_id = %s AND role = %s",
                (build_id, role),
            )
            a = await cur.fetchone()
            if a is None:
                raise BuildRuleError(f"{build_id}/{role}: 등록된 묶음이 없다")
            await cur.execute(
                "INSERT INTO public.artifact_delivery (id, build_id, role, target, requested_by, "
                "requested_at, files) VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()), %s) RETURNING *",
                (_new_delivery_id(), build_id, role, target, requested_by, at, Jsonb(a["files"])),
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
