"""새 공고 직군·업종 추천 (자동 추천 → 요청자 확인) 과 확정 후 문장 분석.

업종: 회사가 온톨로지 company 에 있으면 그 업종 (규칙). 없으면 LLM(prompts/suggest_field).
직군: ① 직무명 유사도 — 온톨로지 포지션 623개의 직무명과 비교, 가장 비슷한 포지션들의 직군
      ② 역량 범위 — 시범 직군마다 매처로 담당업무·자격요건 문장을 매칭해 역량 문장 비율
      점수 = 0.5·①+0.5·②. 1위가 약하거나(<0.35) 2위와 붙어 있으면(차 <0.05) LLM 추천을 섞는다.
평가 때는 exclude_positions 로 평가 대상 포지션을 ① 후보에서 뺀다 (정답 누설 방지).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from task_spec.llm import LLMClient, SchemaViolation, TransientLLMError
from task_spec.llm.client import EmptyResponse
from task_spec.ontology.repository import OntologyRepository
from task_spec.postings.matcher import Matcher
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    AnalyzedLine,
    Candidate,
    PostingLineIn,
    SuggestFieldInput,
    SuggestFieldOutput,
)

WEAK = 0.35
TIE = 0.05
_NORM_COMPANY = re.compile(r"\s+|\(주\)|㈜|주식회사")
_NORM_TITLE = re.compile(r"[\s/·()\[\]\-_,]+")


def _company_key(name: str) -> str:
    return _NORM_COMPANY.sub("", name or "").casefold()


def _title_key(title: str) -> str:
    return _NORM_TITLE.sub(" ", (title or "").casefold()).strip()


@dataclass
class OntologyLists:
    """추천에 쓰는 온톨로지 목록 (버전 고정, 앱 기동 때 한 번 읽는다)."""

    version: str
    subs: dict[str, dict[str, Any]]  # id → {label, in_pilot}
    industries: dict[str, str]
    company_industry: dict[str, str]  # 정규화 회사명 → 업종
    positions: list[dict[str, Any]]  # id, title, sub
    competency_label: dict[str, str]
    tool_info: dict[str, dict[str, Any]]
    rules_sha: dict[str, str] = field(default_factory=dict)
    cells_with_ncs: frozenset[str] = (
        frozenset()
    )  # NCS 허용 목록이 있는 셀 (온톨로지에 공고가 있던 조합)
    matcher: Matcher | None = None


async def load_lists(repo: OntologyRepository) -> OntologyLists:
    subs = {
        r["id"]: {"label": r["label"], "in_pilot": bool(r["in_pilot"])}
        for r in await repo.fetch("SELECT id, label, in_pilot FROM sub_family")
    }
    industries = {r["id"]: r["label"] for r in await repo.fetch("SELECT id, label FROM industry")}
    company = {
        _company_key(r["name"]): r["industry_id"]
        for r in await repo.fetch(
            "SELECT name, industry_id FROM company WHERE industry_id IS NOT NULL"
        )
    }
    positions = [
        {
            "id": r["id"],
            "title": _title_key(f"{r['job'] or ''} {r['position_name'] or ''}"),
            "sub": r["sub"],
        }
        for r in await repo.fetch(
            "SELECT id, job, position_name, sub FROM position WHERE sub IS NOT NULL"
        )
    ]
    comps = {r["id"]: r["label"] for r in await repo.fetch("SELECT id, label FROM competency")}
    tools = {
        r["id"]: {"label": r["label"], "kind": r["kind"], "family_id": r["family_id"]}
        for r in await repo.fetch("SELECT id, label, kind, family_id FROM tool")
    }
    rules = await repo.fetch(
        "SELECT path, sha256, content FROM public.ontology_rules WHERE version = %s",
        (repo.version.version,),
    )
    matcher = Matcher.from_rules({r["path"]: r["content"] for r in rules}) if rules else None
    cells = frozenset(
        r["cell_id"] for r in await repo.fetch("SELECT DISTINCT cell_id FROM cell_ncs")
    )
    return OntologyLists(
        version=repo.version.version,
        subs=subs,
        industries=industries,
        company_industry=company,
        positions=positions,
        competency_label=comps,
        tool_info=tools,
        rules_sha={r["path"]: r["sha256"] for r in rules},
        matcher=matcher,
        cells_with_ncs=cells,
    )


def _title_scores(
    lists: OntologyLists, title: str, exclude: frozenset[str]
) -> dict[str, tuple[float, str]]:
    key = _title_key(title)
    best: dict[str, tuple[float, str]] = {}
    if not key:
        return best
    for p in lists.positions:
        if p["id"] in exclude or not p["title"]:
            continue
        r = SequenceMatcher(None, key, p["title"]).ratio()
        if r > best.get(p["sub"], (0.0, ""))[0]:
            best[p["sub"]] = (r, p["id"])
    return best


def _scope_scores(lists: OntologyLists, lines: list[PostingLineIn]) -> dict[str, tuple[float, int]]:
    body = [x.text for x in lines if x.section in ("duties", "required")]
    if not body or lists.matcher is None:
        return {}
    out: dict[str, tuple[float, int]] = {}
    for sub, info in lists.subs.items():
        if not info["in_pilot"]:
            continue
        n = sum(1 for t in body if lists.matcher.classify(t, sub)["kind"] == "comp")
        out[sub] = (n / len(body), n)
    return out


async def suggest(
    lists: OntologyLists,
    *,
    company: str,
    title: str,
    text: str,
    lines: list[PostingLineIn],
    llm: LLMClient | None,
    prompts: PromptRegistry | None,
    exclude_positions: frozenset[str] = frozenset(),
    prompt_version: int | None = None,
) -> tuple[list[Candidate], list[Candidate], dict[str, Any]]:
    """(직군 후보 top3, 업종 후보, 메타). 메타에 어떤 신호를 썼는지 남긴다."""
    meta: dict[str, Any] = {"llm_used": False}
    titles = _title_scores(lists, title, exclude_positions)
    scopes = _scope_scores(lists, lines)
    sub_score: dict[str, float] = defaultdict(float)
    reasons: dict[str, list[str]] = defaultdict(list)
    for sub, (r, pid) in titles.items():
        sub_score[sub] += 0.5 * r
        if r >= 0.4:
            reasons[sub].append(f"직무명이 {pid} 와 유사({r:.2f})")
    for sub, (share, n) in scopes.items():
        sub_score[sub] += 0.5 * share
        if n:
            reasons[sub].append(f"역량 문장 {n}개가 이 직군 범위")

    industry: list[Candidate] = []
    ind = lists.company_industry.get(_company_key(company))
    if ind:
        industry.append(
            Candidate(
                id=ind,
                label=lists.industries.get(ind, ind),
                score=1.0,
                reasons=["온톨로지에 등록된 회사"],
            )
        )

    ranked = sorted(sub_score.items(), key=lambda x: -x[1])
    weak = (
        not ranked or ranked[0][1] < WEAK or (len(ranked) > 1 and ranked[0][1] - ranked[1][1] < TIE)
    )
    if (weak or not industry) and llm is not None and prompts is not None:
        try:
            out = await _ask_llm(lists, company, title, text, llm, prompts, prompt_version)
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            # LLM 은 보조 — 실패하면 규칙 후보만으로 응답한다 (추천 API 가 500 이 되지 않게)
            meta["llm_error"] = type(exc).__name__
            out = SuggestFieldOutput()
        meta["llm_used"] = True
        for rank, sid in enumerate(out.sub_ids):
            if sid in lists.subs:
                sub_score[sid] += 0.3 - 0.1 * rank
                reasons[sid].append(
                    "LLM 추천" + (f": {out.reason}" if rank == 0 and out.reason else "")
                )
        if not industry:
            picks = out.industry_ids or ([out.industry_id] if out.industry_id else [])
            for rank, iid in enumerate(dict.fromkeys(picks)):
                if iid in lists.industries:
                    industry.append(
                        Candidate(
                            id=iid,
                            label=lists.industries[iid],
                            score=round(0.6 - 0.15 * rank, 2),
                            reasons=["LLM 추천"],
                        )
                    )
        ranked = sorted(sub_score.items(), key=lambda x: -x[1])
    subs = [
        Candidate(id=s, label=lists.subs[s]["label"], score=round(v, 3), reasons=reasons[s])
        for s, v in ranked[:3]
        if s in lists.subs
    ]
    return subs, industry, meta


async def _ask_llm(
    lists: OntologyLists,
    company: str,
    title: str,
    text: str,
    llm: LLMClient,
    prompts: PromptRegistry,
    version: int | None = None,
) -> SuggestFieldOutput:
    payload = SuggestFieldInput(
        company=company or "(미상)",
        title=title or "(미상)",
        text=text[:40000],
        subs="\n".join(f"{k}: {v['label']}" for k, v in sorted(lists.subs.items())),
        industries="\n".join(f"{k}: {v}" for k, v in sorted(lists.industries.items())),
    )
    prompt = assemble(
        prompts.get("suggest_field", version), payload, ontology_version=lists.version
    )
    return (await llm.call(prompt, SuggestFieldOutput)).parsed


def analyze(
    lists: OntologyLists, lines: list[PostingLineIn], sub: str, *, id_prefix: str
) -> list[AnalyzedLine]:
    """확정된 직군으로 문장마다 매처 적용 (결정적). other 절은 분석하지 않는다."""
    if lists.matcher is None:
        raise RuntimeError("매칭 규칙이 적재되지 않았습니다 (load_pg --rules-root)")
    out = []
    for i, x in enumerate(lines, 1):
        if x.section == "other":
            continue
        c = lists.matcher.classify(x.text, sub)
        out.append(
            AnalyzedLine(
                line_id=f"{id_prefix}#{i:03d}",
                section=x.section,
                text=x.text,
                kind=c["kind"],
                comps=c["comps"],
                level=c["level"],
                non_type=c["non_type"],
                tools=lists.matcher.tool_ids(x.text),
            )
        )
    return out
