"""회사 결정적 사건 저장소 (task_db public.company_incident) [확정: 2026-11-19 사용자].

채용담당자 인터뷰 등으로 모은 회사의 실제 장애 사례를 저장하고, 공유 승인을 받은 것만 **같은 회사** 공고의 공통 과제 생성에
공개 사례보다 먼저 쓴다 — 새 직군·새 공고를 올릴 때 쓸 수 있는 사례가 많을수록 좋다. 공개 사례(`incidents/public_v1`)는
지금처럼 파일로만 둔다 (DB 에 넣지 않는다 [2026-11-19 사용자]).

- id = `co:<직군>:<12자>`, 내용은 공개 사례와 같은 형식(PublicIncident). 등록한 뒤 내용을 바꾸지 않는다 — 고치려면 새로 등록하고
  이전 것은 보관한다. 그래서 빌더는 명세의 `source_incident` id 로 명세가 본 그 내용을 읽는다.
- 상태: pending(등록) → shared(공유 승인) | rejected(반려), pending·shared → retired(보관 — 밖으로 샜거나 더 쓰지 않음).
  공유 승인은 interrupt 그래프로만 (graph/incident_share.py, 불변식 9). 보관은 새로 고르지 않게 할 뿐, 이미 만든 과제는 그대로.
- 다른 회사의 과제·LLM 입력에는 쓰지 않는다 (회사 = 공고의 company_id, postings/company.py).
- 고르기(`pick_company_incident`): 공고마다 처음 정한 사례를 계속 쓴다 (같은 공고 지원자 비교 가능성, 불변식 3).
  공통 과제가 이미 있는 공고는 그 사례 그대로 — 공개 사례로 시작했으면 회사 사례로 바꾸지 않는다. 회사 사례는 공통 과제가 아직 없는
  공고부터 쓰고, 다른 공고가 덜 쓴 사례를 먼저 고른다 (같은 사례를 여러 공고에 쓰면 두 공고에 지원한 사람은 함정·정답을 안다).
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict

from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors
from task_spec.schemas import PublicIncident, TaskKind
from task_spec.spec.store import CommonTaskRepo
from task_spec.starter.templates import TEMPLATE_FOR_SUB

COMPANY_PREFIX = "co:"
CompanyResolver = Callable[
    [str], Awaitable[str | None]
]  # 공고 id → 회사 id (postings/company.py) — 모르면 None
CompanyIncidentStatus = Literal["pending", "shared", "rejected", "retired"]
ShareStatus = Literal["shared", "rejected"]

FLAG_COMPANY_REUSED = "회사 사례 {id} 는 다른 공고 {n}곳의 공통 과제에서도 사용함 — 두 공고에 모두 지원한 사람은 함정·정답을 파악할 수 있음"
FLAG_COMPANY_SWITCHED = "이 공고가 사용하던 회사 사례 {id} 를 더 사용할 수 없어(보관·반려 또는 과제 유형에 적합하지 않음) 다른 사례로 생성함"
FLAG_COMPANY_NO_FEATURE = "이 회사의 공유 사례에 기능형 재료가 없어 공개 사례로 생성함"

DDL = """
CREATE TABLE IF NOT EXISTS public.company_incident (
    id text PRIMARY KEY,
    company_id text NOT NULL,
    sub text NOT NULL,
    incident jsonb NOT NULL,
    content_sha256 text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'shared', 'rejected', 'retired')),
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_by text,
    decided_at timestamptz,
    decision_comment text,
    retired_by text,
    retired_at timestamptz,
    retire_reason text
);
CREATE INDEX IF NOT EXISTS company_incident_shared ON public.company_incident (company_id, sub)
    WHERE status = 'shared';
"""


# 내용 검사 — 가명화와 같은 로컬 탐지기 (외부 호출 없음). 값은 밖으로 내지 않고 칸·범주·개수만 남긴다.
# LLM 입력에 들어가는 칸 (spec/incidents.incident_block) + 제목(화면). 연락처·링크는 등록 때 막고, 나머지는 공유 승인 화면에 보인다.
# 막는 칸 = LLM 입력으로 가는 칸 — 과제 생성(spec/incidents.incident_block)과 기능형 재료(계약 생성 plan_contract 입력).
# 그 밖의 칸(원문 오류 문구·피할 이름 등, LLM 에 보내지 않음)은 표시만 한다 — 로그의 사내 주소 같은 것이 정상적으로 들어갈 수 있다.
LLM_FIELDS = (
    "deep_structure",
    "symptom",
    "root_cause",
    "naive_fix",
    "why_naive_fails",
    "key_insight",
    "surface",
    "accepted_fixes",
    "hypotheses",
    "feature",
)
OTHER_FIELDS = ("title", "notes", "tech", "avoid_terms", "hidden_terms", "source_literals")
BLOCKED_CATEGORIES = frozenset({"CONTACT", "LINK"})
_CATEGORY_LABEL = {
    "PERSON": "사람 이름",
    "CONTACT": "연락처",
    "LINK": "링크·주소",
    "SCHOOL": "학교",
    "ORG": "회사·기관",
    "PRODUCT": "제품",
    "COURSE": "교육 과정",
    "REMOVE": "나이·성별 등",
}
Detect = Callable[[str], list[str]]  # 글 → 탐지 범주 목록 (값 없이)


@dataclass(frozen=True)
class Finding:
    field: str
    category: str
    count: int

    @property
    def blocks(self) -> bool:
        """등록을 막는가 — LLM 입력으로 가는 칸의 연락처·링크."""
        return self.category in BLOCKED_CATEGORIES and self.field in LLM_FIELDS

    def text(self) -> str:
        label = _CATEGORY_LABEL.get(self.category, self.category)
        return f"{self.field}: {label}{_ro(label)} 보이는 표현 {self.count}곳"


def _ro(word: str) -> str:
    """조사 '(으)로' — 끝 글자에 받침이 없거나 ㄹ 받침이면 '로' (연락처로·제품으로)."""
    last = word.rstrip()[-1:] or "가"
    code = ord(last) - 0xAC00
    if not 0 <= code < 11172:
        return "로"
    jong = code % 28
    return "로" if jong in (0, 8) else "으로"


def _detector() -> Detect:
    detectors = Detectors(load_dictionaries(), use_kiwi=True)
    return lambda text: [s.category for s in detectors.detect(text)] if text.strip() else []


def _strings(value: Any) -> list[str]:
    # 칸 값 안의 글 전부 (목록·기능형 재료처럼 겹친 값도) — 키·수·참거짓은 빼고
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for v in value for t in _strings(v)]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _strings(v)]
    return []


def scan_incident(incident: PublicIncident, detect: Detect | None = None) -> list[Finding]:
    """사례 내용에서 개인정보·회사 고유명으로 보이는 표현을 칸·범주별로 센다 (블로킹 — 호출하는 쪽이 to_thread).
    detect 가 없으면 이 검사 한 번에 탐지기 하나를 만들어 모든 칸에 쓴다."""
    run = detect or _detector()
    data = incident.model_dump(mode="json")
    out: list[Finding] = []
    for name in (*LLM_FIELDS, *OTHER_FIELDS):
        counts = Counter(run("\n".join(_strings(data.get(name)))))
        out.extend(Finding(name, cat, n) for cat, n in sorted(counts.items()))
    return out


def is_company_incident(incident_id: str | None) -> bool:
    return bool(incident_id) and str(incident_id).startswith(COMPANY_PREFIX)


def new_incident_id(sub: str) -> str:
    return f"{COMPANY_PREFIX}{sub}:{uuid.uuid4().hex[:12]}"


def content_sha256(incident: PublicIncident) -> str:
    return hashlib.sha256(incident.model_dump_json().encode("utf-8")).hexdigest()


def feature_fit(incident: PublicIncident, sub: str) -> bool:
    """기능형 과제에 쓸 수 있는가 — 직군 템플릿이 시험할 수 있는 기능형 재료 (spec/incidents.feature_incident 와 같은 기준)."""
    template = TEMPLATE_FOR_SUB.get(sub)
    return incident.feature is not None and template is not None and template in incident.templates


class CompanyIncident(BaseModel):
    """저장된 회사 사례 한 건 (내용 + 상태·기록)."""

    model_config = ConfigDict(frozen=True)

    incident_id: str
    company_id: str
    sub: str
    incident: PublicIncident
    content_sha256: str
    status: CompanyIncidentStatus
    source: dict[str, Any]
    created_by: str
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_comment: str | None = None
    retired_by: str | None = None
    retired_at: datetime | None = None
    retire_reason: str | None = None


class CompanyIncidentReader(Protocol):
    async def get(self, incident_id: str) -> CompanyIncident | None: ...


class CompanyIncidentRepo(CompanyIncidentReader, Protocol):
    async def submit(
        self,
        company_id: str,
        incident: PublicIncident,
        *,
        source: dict[str, Any],
        created_by: str,
    ) -> CompanyIncident:
        """등록 (pending). 같은 id 가 있으면 ValueError."""
        ...

    async def find(
        self,
        *,
        company_id: str | None = None,
        status: CompanyIncidentStatus | None = None,
        limit: int = 200,
    ) -> list[CompanyIncident]:
        """최근 등록 순."""
        ...

    async def shared(self, company_id: str, sub: str) -> list[CompanyIncident]:
        """그 회사·직군의 공유된 사례 (id 순) — 과제 생성 후보."""
        ...

    async def decide(
        self,
        incident_id: str,
        content: str,
        status: ShareStatus,
        reviewer: str,
        at: datetime,
        comment: str | None,
    ) -> bool:
        """pending 이고 내용 지문이 같을 때만 shared·rejected 로 (아니면 False — 그 사이 보관·결정됨)."""
        ...

    async def retire(self, incident_id: str, reviewer: str, at: datetime, reason: str) -> bool:
        """pending·shared → retired (아니면 False)."""
        ...


def _check_new(company_id: str, incident: PublicIncident) -> None:
    if not company_id.strip():
        raise ValueError("회사 id 가 비어 있다")
    if not is_company_incident(incident.incident_id):
        raise ValueError(f"회사 사례 id 는 {COMPANY_PREFIX} 로 시작한다: {incident.incident_id}")
    if incident.incident_id.split(":")[1] != incident.sub:
        raise ValueError(
            f"사례 id 의 직군이 sub 와 다르다: {incident.incident_id} / {incident.sub}"
        )


_INSERT = (
    "INSERT INTO public.company_incident "
    "(id, company_id, sub, incident, content_sha256, source, created_by) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *"
)


def _insert_params(
    company_id: str, incident: PublicIncident, source: dict[str, Any], created_by: str
) -> tuple[Any, ...]:
    return (
        incident.incident_id,
        company_id,
        incident.sub,
        Jsonb(incident.model_dump(mode="json")),
        content_sha256(incident),
        Jsonb(source),
        created_by,
    )


async def insert_incident(
    conn: AsyncConnection[Any],
    company_id: str,
    incident: PublicIncident,
    *,
    source: dict[str, Any],
    created_by: str,
) -> None:
    """등록 (pending) — 호출하는 쪽 트랜잭션 안에서 (인터뷰 승인이 인터뷰 판본 확정·과제 승인과 함께 묶는다, #266 D2b).
    같은 id 가 있으면 ValueError (그 savepoint 만 되돌린다). 연락처·링크 검사(scan_incident)는 부르는 쪽이 먼저 한다."""
    _check_new(company_id, incident)
    try:
        async with conn.transaction():
            await conn.execute(_INSERT, _insert_params(company_id, incident, source, created_by))
    except errors.UniqueViolation:
        raise ValueError(f"이미 있는 사례 id: {incident.incident_id}") from None


class InMemoryCompanyIncidents:
    """단위 테스트·DB 없는 앱용."""

    def __init__(self) -> None:
        self.rows: dict[str, CompanyIncident] = {}

    async def submit(
        self,
        company_id: str,
        incident: PublicIncident,
        *,
        source: dict[str, Any],
        created_by: str,
    ) -> CompanyIncident:
        _check_new(company_id, incident)
        if incident.incident_id in self.rows:
            raise ValueError(f"이미 있는 사례 id: {incident.incident_id}")
        rec = CompanyIncident(
            incident_id=incident.incident_id,
            company_id=company_id,
            sub=incident.sub,
            incident=incident,
            content_sha256=content_sha256(incident),
            status="pending",
            source=dict(source),
            created_by=created_by,
            created_at=datetime.now(UTC),
        )
        self.rows[rec.incident_id] = rec
        return rec

    async def get(self, incident_id: str) -> CompanyIncident | None:
        return self.rows.get(incident_id)

    async def find(
        self,
        *,
        company_id: str | None = None,
        status: CompanyIncidentStatus | None = None,
        limit: int = 200,
    ) -> list[CompanyIncident]:
        rows = [
            r
            for r in self.rows.values()
            if (company_id is None or r.company_id == company_id)
            and (status is None or r.status == status)
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    async def shared(self, company_id: str, sub: str) -> list[CompanyIncident]:
        return sorted(
            (
                r
                for r in self.rows.values()
                if r.company_id == company_id and r.sub == sub and r.status == "shared"
            ),
            key=lambda r: r.incident_id,
        )

    async def decide(
        self,
        incident_id: str,
        content: str,
        status: ShareStatus,
        reviewer: str,
        at: datetime,
        comment: str | None,
    ) -> bool:
        r = self.rows.get(incident_id)
        if r is None or r.status != "pending" or r.content_sha256 != content:
            return False
        self.rows[incident_id] = r.model_copy(
            update={
                "status": status,
                "decided_by": reviewer,
                "decided_at": at,
                "decision_comment": comment,
            }
        )
        return True

    async def retire(self, incident_id: str, reviewer: str, at: datetime, reason: str) -> bool:
        r = self.rows.get(incident_id)
        if r is None or r.status not in ("pending", "shared"):
            return False
        self.rows[incident_id] = r.model_copy(
            update={
                "status": "retired",
                "retired_by": reviewer,
                "retired_at": at,
                "retire_reason": reason,
            }
        )
        return True


class CompanyIncidentStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, conninfo: str, *, ddl: bool = True) -> CompanyIncidentStore:
        """ddl=False — 표를 만들지 않는다 (builder 전용 DB 역할은 DDL 을 못 돌린다, 읽기만)."""
        pool = AsyncConnectionPool(
            conninfo, min_size=1, max_size=4, open=False, kwargs={"autocommit": True}
        )
        await pool.open(wait=True)
        if ddl:
            async with pool.connection() as conn:
                await conn.execute(DDL)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    @staticmethod
    def _row(r: dict[str, Any]) -> CompanyIncident:
        return CompanyIncident(
            incident_id=r["id"],
            company_id=r["company_id"],
            sub=r["sub"],
            incident=PublicIncident.model_validate(r["incident"]),
            content_sha256=r["content_sha256"],
            status=r["status"],
            source=r["source"] or {},
            created_by=r["created_by"],
            created_at=r["created_at"],
            decided_by=r["decided_by"],
            decided_at=r["decided_at"],
            decision_comment=r["decision_comment"],
            retired_by=r["retired_by"],
            retired_at=r["retired_at"],
            retire_reason=r["retire_reason"],
        )

    async def submit(
        self,
        company_id: str,
        incident: PublicIncident,
        *,
        source: dict[str, Any],
        created_by: str,
    ) -> CompanyIncident:
        _check_new(company_id, incident)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(_INSERT, _insert_params(company_id, incident, source, created_by))
            except Exception as exc:  # psycopg.errors.UniqueViolation
                if type(exc).__name__ == "UniqueViolation":
                    raise ValueError(f"이미 있는 사례 id: {incident.incident_id}") from None
                raise
            r = await cur.fetchone()
        assert r is not None
        return self._row(r)

    async def get(self, incident_id: str) -> CompanyIncident | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM public.company_incident WHERE id = %s", (incident_id,))
            r = await cur.fetchone()
        return self._row(r) if r else None

    async def find(
        self,
        *,
        company_id: str | None = None,
        status: CompanyIncidentStatus | None = None,
        limit: int = 200,
    ) -> list[CompanyIncident]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.company_incident "
                "WHERE (%(company)s::text IS NULL OR company_id = %(company)s) "
                "AND (%(status)s::text IS NULL OR status = %(status)s) "
                "ORDER BY created_at DESC, id LIMIT %(limit)s",
                {"company": company_id, "status": status, "limit": limit},
            )
            rows = await cur.fetchall()
        return [self._row(r) for r in rows]

    async def shared(self, company_id: str, sub: str) -> list[CompanyIncident]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM public.company_incident "
                "WHERE company_id = %s AND sub = %s AND status = 'shared' ORDER BY id",
                (company_id, sub),
            )
            rows = await cur.fetchall()
        return [self._row(r) for r in rows]

    async def decide(
        self,
        incident_id: str,
        content: str,
        status: ShareStatus,
        reviewer: str,
        at: datetime,
        comment: str | None,
    ) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE public.company_incident SET status = %s, decided_by = %s, decided_at = %s, "
                "decision_comment = %s WHERE id = %s AND status = 'pending' AND content_sha256 = %s",
                (status, reviewer, at, comment, incident_id, content),
            )
            return cur.rowcount == 1

    async def retire(self, incident_id: str, reviewer: str, at: datetime, reason: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE public.company_incident SET status = 'retired', retired_by = %s, "
                "retired_at = %s, retire_reason = %s "
                "WHERE id = %s AND status IN ('pending', 'shared')",
                (reviewer, at, reason, incident_id),
            )
            return cur.rowcount == 1


@dataclass(frozen=True)
class CompanyPick:
    """공고 하나의 공통 과제에 쓸 회사 사례 — incident=None 이면 공개 사례 그대로."""

    incident: PublicIncident | None
    flags: list[str] = field(default_factory=list)  # 명세 단위 review_flags


def _spread(posting_id: str, incident_id: str) -> str:
    # 덜 쓴 사례가 여럿일 때 공고마다 다른 사례가 가게 — 같은 공고는 늘 같은 순서
    return hashlib.sha256(f"{posting_id}|{incident_id}".encode()).hexdigest()


def company_open(prev: Sequence[str], shared: Sequence[CompanyIncident]) -> bool:
    """이 공고에 회사 사례를 쓸 수 있는가 — 그 공고가 쓴 사례(`prev`)를 앞에서부터 볼 때 공개 사례·사례 없음이 먼저 나오면 아니다
    (공통 과제가 이미 있는 공고는 그 사례 그대로, 불변식 3). 더 쓸 수 없는 회사 사례는 건너뛴다."""
    ids = {c.incident_id for c in shared}
    for inc in prev:
        if inc in ids:
            return True
        if not is_company_incident(inc):
            return False
    return bool(shared)


async def pick_company_incident(
    shared: Sequence[CompanyIncident],
    *,
    kind: TaskKind,
    sub: str,
    posting_id: str,
    prev: Sequence[str],
    common: CommonTaskRepo,
) -> CompanyPick:
    """공고 하나에 쓸 회사 사례 고르기.

    `prev` = 그 공고의 공통 과제가 쓴 사례 id (CommonTaskRepo.posting_incidents — 승인된 것 먼저, 최근 순, 사례 없으면 "").
    앞에서부터 본다:
    - 아직 쓸 수 있는 회사 사례(공유 중·이번 유형에 맞음) → 그 사례 그대로 (보관 뒤 고른 대체 사례도 이렇게 고정된다)
    - 공개 사례·사례 없음 → 회사 사례로 바꾸지 않는다 (None) — 같은 공고 지원자 비교 가능성 (불변식 3)
    - 더 쓸 수 없는 회사 사례(보관·반려·유형) → 건너뛰고 표시 (`FLAG_COMPANY_SWITCHED`)
    끝까지 정하지 못하면 새로 고른다: 다른 공고가 덜 쓴 사례 먼저 (승인된 공통 과제 기준), 같으면 공고마다 다른 순서.
    기능형은 직군 템플릿이 시험할 수 있는 기능형 재료가 있는 사례만. 공유 사례가 하나도 없으면 공개 사례 (None).
    """
    fit = {c.incident_id: c for c in shared if kind != "feature" or feature_fit(c.incident, sub)}
    flags: list[str] = []
    chosen: CompanyIncident | None = None
    gone: str | None = None  # 처음 만난, 더 쓸 수 없는 회사 사례
    public = False  # 공개 사례·사례 없음을 만났다 — 그 공고는 그대로 둔다
    for inc in prev:
        if inc in fit:
            chosen = fit[inc]
            break
        if not is_company_incident(inc):
            public = True
            break
        gone = gone or inc
    if gone is not None:
        flags.append(FLAG_COMPANY_SWITCHED.format(id=gone))
    if chosen is None and public:
        return CompanyPick(None, flags)
    if chosen is None and not fit:
        if shared and kind == "feature":
            flags.append(FLAG_COMPANY_NO_FEATURE)
        return CompanyPick(None, flags)
    usage = await common.incident_usage(sorted(fit), exclude_posting=posting_id)
    if chosen is None:
        chosen = min(
            fit.values(),
            key=lambda c: (usage.get(c.incident_id, 0), _spread(posting_id, c.incident_id)),
        )
    if n := usage.get(chosen.incident_id, 0):
        flags.append(FLAG_COMPANY_REUSED.format(id=chosen.incident_id, n=n))
    return CompanyPick(chosen.incident, flags)
