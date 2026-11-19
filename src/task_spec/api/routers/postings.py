"""새 공고(경로 2) API — 자동 추천 → 요청자 확인 → 임시 공고.

POST /postings/suggest : 공고 원문(텍스트 또는 파일) → 절 분리 + 직군·업종 후보 (저장, suggestion_id)
POST /postings         : suggestion_id + 확인한 직군·업종 + 확인자 → 매처 분석 → 임시 공고 (posting_id=prov-…)
GET  /postings?q=      : 공고 찾기 — 회사·직무명·id 로 온톨로지 공고(기본 시범만)와 임시 공고 [제안: 2026-11-04]
GET  /postings/{id}    : 공고 프로필 — 임시 공고와 온톨로지 공고 모두
PUT  /postings/{id}/career-level : 공고의 신입·경력을 사람이 정함 (postings/career.py) — 찾기·프로필·빌드·실행 목록이 같은 값을 보인다
공고 원문은 지원자 개인정보가 아니다. 확인자(confirmed_by)는 인증 전까지 요청 본문에서 받는다 [TODO(auth)].
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, Field

from task_spec.api.deps import OntologyDep, PostingsContext, PostingsDep, SettingsDep, get_postings
from task_spec.api.errors import (
    InvalidUpload,
    PostingCompanyInvalid,
    PostingDuplicate,
    PostingNotFound,
    ServiceUnavailable,
    UnsupportedFileType,
)
from task_spec.api.uploads import ALLOWED_EXTENSIONS, _read_limited
from task_spec.nodes.ingest.extractors import OcrError, UnsupportedFormat, extract_raw
from task_spec.ontology.queries import posting_exists, posting_profile, search_positions
from task_spec.postings.career import PostingCareer, estimate_career, posting_careers
from task_spec.postings.company import CompanySource, company_id, posting_companies
from task_spec.postings.search import CompanyFilter, PostingHit, search_terms
from task_spec.postings.sections import segment
from task_spec.postings.store import (
    SamePostingExists,
    is_provisional,
    provisional_profile,
    rules_fingerprint,
    text_sha256,
)
from task_spec.postings.suggest import analyze, suggest
from task_spec.schemas import (
    Candidate,
    CareerLevel,
    CareerSource,
    PostingLineIn,
    PostingProfile,
)

router = APIRouter(prefix="/postings", tags=["postings"])


class SuggestResponse(BaseModel):
    suggestion_id: str
    segment_method: str
    lines: list[PostingLineIn]
    sub_candidates: list[Candidate]
    industry_candidates: list[Candidate]
    llm_used: bool
    # 공고 문구(직무명·자격요건)로 추정한 신입·경력 — 확인 단계 선택 칸의 기본값. 단서가 없거나 둘 다면 null
    career_level_candidate: CareerLevel | None = None
    # 같은 회사·같은 원문으로 이미 확정된 공고 — 확정은 409, 일부러 따로 등록하려면 allow_duplicate (POST /postings)
    existing_posting_ids: list[str] = Field(default_factory=list)
    # 같은 회사·같은 제목인데 원문이 다른 공고 — 막지 않는다. 같은 공고를 고쳐 올린 것인지 확인 화면에서 본다
    similar_posting_ids: list[str] = Field(default_factory=list)


class ConfirmRequest(BaseModel):
    suggestion_id: str = Field(min_length=1)
    sub: str = Field(min_length=1)
    industry_id: str = Field(min_length=1)
    secondary_sub: str | None = None
    confirmed_by: str = Field(min_length=1, max_length=200)  # TODO(auth)
    career_level: CareerLevel | None = (
        None  # 확인자가 고른 신입·경력 — 없으면 공고 문구 추정을 보인다
    )
    # 같은 회사·같은 원문의 공고가 있어도 새로 만든다 — 같은 공고로 다른 문제를 만들 때 (없으면 409 posting_duplicate)
    allow_duplicate: bool = False


class ConfirmResponse(BaseModel):
    posting_id: str
    source: str = "provisional"
    competencies: int
    unmatched_lines: int


class PostingProfileView(PostingProfile):
    """공고 프로필 + 신입·경력 — API 응답에만. 그래프 State 의 PostingProfile 에는 넣지 않는다 (체크포인트 모델)."""

    career_level: CareerLevel | None = None
    career_level_source: CareerSource | None = None


class CareerLevelIn(BaseModel):
    career_level: CareerLevel | None  # null = 정한 값 지우기 (다시 공고 문구 추정)
    set_by: str = Field(min_length=1, max_length=200)  # TODO(auth)


class CompanyIn(BaseModel):
    company: str | None = Field(
        min_length=1, max_length=200
    )  # null = 정한 값 지우기 (다시 공고의 회사 칸)
    set_by: str = Field(
        min_length=1, max_length=200
    )  # TODO(auth) — 인증 전에는 입력한 회사를 그대로 믿는다


class CompanyOut(BaseModel):
    """정한 뒤의 회사 — 지웠으면 공고 데이터의 회사, 그것도 없으면 null."""

    posting_id: str
    company_id: str | None = None
    company: str | None = None
    company_known: bool | None = None  # 온톨로지 company 표에 있는 회사인지
    company_source: CompanySource | None = None


class CareerLevelOut(BaseModel):
    """정한 뒤의 값 — 지웠으면 공고 문구 추정, 그것도 없으면 null."""

    posting_id: str
    career_level: CareerLevel | None = None
    career_level_source: CareerSource | None = None


def _career_fields(c: PostingCareer | None) -> dict[str, Any]:
    return {"career_level": c.level if c else None, "career_level_source": c.source if c else None}


async def _file_text(upload: UploadFile, settings: Any) -> str:
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileType(f"허용 형식은 {', '.join(sorted(ALLOWED_EXTENSIONS))} 입니다")
    data = await _read_limited(upload, settings.upload_max_bytes, "posting")
    work = Path(settings.data_dir) / "tmp" / f"posting-{uuid.uuid4().hex[:8]}"

    def run() -> str:
        work.mkdir(parents=True, exist_ok=True)
        src = work / f"posting{ext}"
        src.write_bytes(data)
        try:
            return "\n".join(extract_raw(src, work / "ocr").lines)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    try:
        return await asyncio.to_thread(run)
    except (UnsupportedFormat, OcrError) as exc:
        raise InvalidUpload(
            f"공고 파일에서 텍스트를 뽑지 못했습니다 ({type(exc).__name__})"
        ) from None


@router.post("/suggest", status_code=201)
async def suggest_posting(
    settings: SettingsDep,
    postings: PostingsDep,
    company: Annotated[str, Form(max_length=200)] = "",
    title: Annotated[str, Form(max_length=300)] = "",
    text: Annotated[str | None, Form(max_length=40000)] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> SuggestResponse:
    if (text is None) == (file is None):
        raise InvalidUpload("공고 원문은 text 또는 file 중 하나로 보내 주세요")
    body = text if text is not None else await _file_text(file, settings)  # type: ignore[arg-type]
    seg = await segment(body, postings.llm, postings.prompts)
    if not seg.lines:
        raise InvalidUpload("공고에서 담당업무·자격요건 문장을 찾지 못했습니다")
    subs, industries, meta = await suggest(
        postings.lists,
        company=company,
        title=title,
        text=body,
        lines=seg.lines,
        llm=postings.llm,
        prompts=postings.prompts,
    )
    sid = await postings.store.create_suggestion(
        text=body,
        company=company,
        title=title,
        method=seg.method,
        lines=seg.lines,
        subs=subs,
        industries=industries,
        meta=meta,
        ontology_version=postings.lists.version,
    )
    key, digest = company_id(company) or "", text_sha256(body)
    return SuggestResponse(
        suggestion_id=sid,
        segment_method=seg.method,
        lines=seg.lines,
        sub_candidates=subs,
        industry_candidates=industries,
        llm_used=meta["llm_used"],
        career_level_candidate=estimate_career(
            [title, *(x.text for x in seg.lines if x.section == "required")]
        ),
        existing_posting_ids=await postings.store.same_postings(key, digest),
        similar_posting_ids=await postings.store.similar_postings(key, title, digest),
    )


@router.post("", status_code=201)
async def confirm_posting(req: ConfirmRequest, postings: PostingsDep) -> ConfirmResponse:
    lists = postings.lists
    sug = await postings.store.get_suggestion(req.suggestion_id)
    if sug is None:
        raise PostingNotFound(req.suggestion_id)
    if sug["ontology_version"] != lists.version:
        raise InvalidUpload("추천 이후 온톨로지 버전이 바뀌었습니다 — 다시 추천받아 주세요")
    bad = [
        f"{k}={v}"
        for k, v, ok in (
            ("sub", req.sub, req.sub in lists.subs),
            (
                "secondary_sub",
                req.secondary_sub,
                req.secondary_sub is None or req.secondary_sub in lists.subs,
            ),
            ("industry_id", req.industry_id, req.industry_id in lists.industries),
        )
        if not ok
    ]
    if bad:
        raise InvalidUpload(f"온톨로지 목록에 없는 값: {', '.join(bad)}")
    lines = [PostingLineIn.model_validate(x) for x in sug["lines"]]
    pid_prefix = f"sug:{req.suggestion_id}"
    analyzed = analyze(lists, lines, req.sub, id_prefix=pid_prefix)
    # 같은 회사·같은 원문의 공고가 있으면 알린다 — 실수로 두 번 올리면 같은 공고 목록에 두 줄이 생긴다 (2026-11-20 운영).
    # 검사와 저장은 저장소가 한 트랜잭션으로 (확정을 두 번 눌러도 하나만)
    unique = None if req.allow_duplicate else (company_id(sug["company"]) or "", sug["text_sha256"])
    try:
        pid = await postings.store.create_provisional(
            unique=unique,
            suggestion_id=req.suggestion_id,
            company=sug["company"],
            title=sug["title"],
            sub=req.sub,
            secondary_sub=req.secondary_sub,
            industry_id=req.industry_id,
            confirmed_by=req.confirmed_by,
            ontology_version=lists.version,
            rules_sha256=rules_fingerprint(lists),
            lines=analyzed,
        )
    except SamePostingExists as e:
        raise PostingDuplicate(e.posting_ids) from None
    if req.career_level is not None:
        await postings.store.set_career_level(pid, req.career_level, req.confirmed_by)
    return ConfirmResponse(
        posting_id=pid,
        competencies=len({c for x in analyzed for c in x.comps}),
        unmatched_lines=sum(1 for x in analyzed if x.kind == "unmatched"),
    )


DEFAULT_SEARCH_LIMIT = 20


async def _company_filter(company: str | None, ctx: PostingsContext | None) -> CompanyFilter | None:
    key = company_id(company)
    if key is None:
        return None
    entered = await ctx.store.all_entered_companies() if ctx is not None else {}
    mine = tuple(sorted(p for p, c in entered.items() if company_id(c) == key))
    others = tuple(sorted(p for p, c in entered.items() if company_id(c) != key))
    return CompanyFilter(key=key, include=mine, exclude=others)


@router.get("")
async def search_postings(
    request: Request,
    ontology: OntologyDep,
    q: Annotated[str, Query(max_length=100)] = "",
    pilot_only: bool = True,
    limit: Annotated[int | None, Query(ge=1, le=50)] = None,
    company: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> list[PostingHit]:
    """회사·직무명·id 로 공고 찾기. 임시 공고(최근 것부터) 다음에 온톨로지 공고. 검색어가 비면 앞에서부터.
    `company`(회사 id 또는 표기, GET /runs 와 같은 규칙)를 주면 그 회사 공고만 — 사람이 정한 회사가 먼저다. 이때 limit 이
    없으면 전부 (채용담당자 인터뷰가 자기 회사 공고를 모두 보고 고른다 [2026-11-20 사용자]). company 없이는 limit 기본 20."""
    ctx = getattr(request.app.state, "postings", None)
    if ontology is None and ctx is None:
        raise ServiceUnavailable("공고 찾기가 준비되지 않았습니다 (온톨로지·저장소 미연결)")
    labels = {k: str(v.get("label", "")) for k, v in ctx.lists.subs.items()} if ctx else None
    terms = search_terms(q, labels)
    only = await _company_filter(company, ctx)
    if company is not None and only is None:
        return []
    cap = limit if limit is not None or company is not None else DEFAULT_SEARCH_LIMIT
    hits: list[PostingHit] = []
    if ctx is not None:
        for r in await ctx.store.search_provisional(terms, cap, only):
            hits.append(
                PostingHit(
                    posting_id=r["id"],
                    source="provisional",
                    company=r["company"],
                    title=r["title"],
                    sub=r["sub"],
                    industry_id=r["industry_id"],
                )
            )
    if ontology is not None and (cap is None or len(hits) < cap):
        for r in await search_positions(
            ontology,
            terms,
            pilot_only=pilot_only,
            limit=None if cap is None else cap - len(hits),
            company=only,
        ):
            hits.append(
                PostingHit(
                    posting_id=str(r["id"]),
                    source="ontology",
                    company=r["company"],  # type: ignore[arg-type]
                    title=r["job"],  # type: ignore[arg-type]
                    position_name=r["position_name"],  # type: ignore[arg-type]
                    sub=r["sub"],  # type: ignore[arg-type]
                    industry_id=r["industry_id"],  # type: ignore[arg-type]
                    in_pilot=bool(r["in_pilot"]),
                )
            )
    careers = await posting_careers(
        [h.posting_id for h in hits], ontology=ontology, store=ctx.store if ctx else None
    )
    # 사람이 정한 회사가 있으면 그 표기를 보인다 (회사 사례를 고르는 값과 같게)
    entered = await ctx.store.entered_companies([h.posting_id for h in hits]) if ctx else {}
    return [
        h.model_copy(
            update={
                **_career_fields(careers.get(h.posting_id)),
                **({"company": entered[h.posting_id]} if h.posting_id in entered else {}),
            }
        )
        for h in hits
    ]


class SubItem(BaseModel):
    id: str
    label: str
    in_pilot: bool


@router.get("/subs")
async def list_subs(postings: PostingsDep) -> list[SubItem]:
    """직군 목록 (온톨로지, 버전 고정) — 공고 확인·회사 결정적 사건 등록 화면의 직군 선택 칸.
    `/{posting_id}` 보다 먼저 선언한다 (subs 를 공고 id 로 잡지 않게)."""
    return [
        SubItem(id=k, label=str(v.get("label", "")), in_pilot=bool(v.get("in_pilot")))
        for k, v in sorted(postings.lists.subs.items())
    ]


@router.get("/{posting_id}")
async def get_posting(
    posting_id: str, request: Request, ontology: OntologyDep
) -> PostingProfileView:
    """임시 공고(prov-…)는 저장소에서, 그 밖에는 온톨로지 공고 프로필 (① posting_profile 과 같은 질의)."""
    prof: PostingProfile | None = None
    if is_provisional(posting_id):
        postings = get_postings(request)
        row = await postings.store.get_provisional(posting_id)
        if row is not None:
            prof = provisional_profile(row, postings.lists)
    elif ontology is not None:
        prof = await posting_profile(ontology, posting_id)
    if prof is None:
        raise PostingNotFound(posting_id)
    ctx = getattr(request.app.state, "postings", None)
    careers = await posting_careers(
        [posting_id], ontology=ontology, store=ctx.store if ctx else None
    )
    return PostingProfileView(**prof.model_dump(), **_career_fields(careers.get(posting_id)))


@router.put("/{posting_id}/career-level")
async def set_career_level(
    posting_id: str, req: CareerLevelIn, postings: PostingsDep, ontology: OntologyDep
) -> CareerLevelOut:
    """공고의 신입·경력을 정한다 (온톨로지 공고도 — 값은 task_db posting_meta 에. 온톨로지는 읽기만, 불변식 4)."""
    if is_provisional(posting_id):
        found = await postings.store.get_provisional(posting_id) is not None
    else:
        found = ontology is not None and await posting_exists(ontology, posting_id)
    if not found:
        raise PostingNotFound(posting_id)
    await postings.store.set_career_level(posting_id, req.career_level, req.set_by)
    careers = await posting_careers([posting_id], ontology=ontology, store=postings.store)
    return CareerLevelOut(posting_id=posting_id, **_career_fields(careers.get(posting_id)))


@router.put("/{posting_id}/company")
async def set_company(
    posting_id: str, req: CompanyIn, postings: PostingsDep, ontology: OntologyDep
) -> CompanyOut:
    """공고의 회사를 정한다 (온톨로지 공고도 — 값은 task_db posting_company 에) [확정: 2026-11-19 사용자 — 인증 전에는 입력한
    회사를 믿는다]. 회사 결정적 사건은 이 회사의 공유 사례에서 고른다 (spec/company_incidents.py) — 이미 회사 사례로 공통 과제를
    만든 공고의 회사를 바꾸면 다음 명세부터 다른 사례로 바뀌고 명세에 표시된다."""
    if is_provisional(posting_id):
        found = await postings.store.get_provisional(posting_id) is not None
    else:
        found = ontology is not None and await posting_exists(ontology, posting_id)
    if not found:
        raise PostingNotFound(posting_id)
    if req.company is not None and company_id(req.company) is None:
        raise PostingCompanyInvalid()
    await postings.store.set_company(
        posting_id, req.company.strip() if req.company else None, req.set_by
    )
    found_company = (
        await posting_companies([posting_id], ontology=ontology, store=postings.store)
    ).get(posting_id)
    if found_company is None:
        return CompanyOut(posting_id=posting_id)
    return CompanyOut(
        posting_id=posting_id,
        company_id=found_company.company_id,
        company=found_company.name,
        company_known=found_company.known,
        company_source=found_company.source,
    )
