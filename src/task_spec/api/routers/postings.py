"""새 공고(경로 2) API — 자동 추천 → 요청자 확인 → 임시 공고.

POST /postings/suggest : 공고 원문(텍스트 또는 파일) → 절 분리 + 직군·업종 후보 (저장, suggestion_id)
POST /postings         : suggestion_id + 확인한 직군·업종 + 확인자 → 매처 분석 → 임시 공고 (posting_id=prov-…)
GET  /postings?q=      : 공고 찾기 — 회사·직무명·id 로 온톨로지 공고(기본 시범만)와 임시 공고 [제안: 2026-11-04]
GET  /postings/{id}    : 공고 프로필 — 임시 공고와 온톨로지 공고 모두
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

from task_spec.api.deps import OntologyDep, PostingsDep, SettingsDep, get_postings
from task_spec.api.errors import (
    InvalidUpload,
    PostingNotFound,
    ServiceUnavailable,
    UnsupportedFileType,
)
from task_spec.api.uploads import ALLOWED_EXTENSIONS, _read_limited
from task_spec.nodes.ingest.extractors import OcrError, UnsupportedFormat, extract_raw
from task_spec.ontology.queries import posting_profile, search_positions
from task_spec.postings.search import PostingHit, search_terms
from task_spec.postings.sections import segment
from task_spec.postings.store import is_provisional, provisional_profile, rules_fingerprint
from task_spec.postings.suggest import analyze, suggest
from task_spec.schemas import Candidate, PostingLineIn, PostingProfile

router = APIRouter(prefix="/postings", tags=["postings"])


class SuggestResponse(BaseModel):
    suggestion_id: str
    segment_method: str
    lines: list[PostingLineIn]
    sub_candidates: list[Candidate]
    industry_candidates: list[Candidate]
    llm_used: bool


class ConfirmRequest(BaseModel):
    suggestion_id: str = Field(min_length=1)
    sub: str = Field(min_length=1)
    industry_id: str = Field(min_length=1)
    secondary_sub: str | None = None
    confirmed_by: str = Field(min_length=1, max_length=200)  # TODO(auth)


class ConfirmResponse(BaseModel):
    posting_id: str
    source: str = "provisional"
    competencies: int
    unmatched_lines: int


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
    return SuggestResponse(
        suggestion_id=sid,
        segment_method=seg.method,
        lines=seg.lines,
        sub_candidates=subs,
        industry_candidates=industries,
        llm_used=meta["llm_used"],
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
    pid = await postings.store.create_provisional(
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
    return ConfirmResponse(
        posting_id=pid,
        competencies=len({c for x in analyzed for c in x.comps}),
        unmatched_lines=sum(1 for x in analyzed if x.kind == "unmatched"),
    )


@router.get("")
async def search_postings(
    request: Request,
    ontology: OntologyDep,
    q: Annotated[str, Query(max_length=100)] = "",
    pilot_only: bool = True,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[PostingHit]:
    """회사·직무명·id 로 공고 찾기. 임시 공고(최근 것부터) 다음에 온톨로지 공고. 검색어가 비면 앞에서부터."""
    ctx = getattr(request.app.state, "postings", None)
    if ontology is None and ctx is None:
        raise ServiceUnavailable("공고 찾기가 준비되지 않았습니다 (온톨로지·저장소 미연결)")
    labels = {k: str(v.get("label", "")) for k, v in ctx.lists.subs.items()} if ctx else None
    terms = search_terms(q, labels)
    hits: list[PostingHit] = []
    if ctx is not None:
        for r in await ctx.store.search_provisional(terms, limit):
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
    if ontology is not None and len(hits) < limit:
        for r in await search_positions(
            ontology, terms, pilot_only=pilot_only, limit=limit - len(hits)
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
    return hits


@router.get("/{posting_id}")
async def get_posting(posting_id: str, request: Request, ontology: OntologyDep) -> PostingProfile:
    """임시 공고(prov-…)는 저장소에서, 그 밖에는 온톨로지 공고 프로필 (① posting_profile 과 같은 질의)."""
    if is_provisional(posting_id):
        postings = get_postings(request)
        row = await postings.store.get_provisional(posting_id)
        if row is not None:
            return provisional_profile(row, postings.lists)
    elif ontology is not None:
        prof = await posting_profile(ontology, posting_id)
        if prof is not None:
            return prof
    raise PostingNotFound(posting_id)
