"""명세 초안 v13 의 틀 점검 (#318) — 틀이 정한 '엔티티로 두지 않을 것'을 초안이 저장 대상으로 그리는가 (v12 와 견줌).

표본: 시범 공고(백엔드, 기능형 강제) 앞에서 N건 × 가상 세션 유지 재료 하나 (2026-11-24 세션 유지 과제의 꼴 — 지원자·회사 원문 없음).
틀은 운영 순서로 한 번 만든다: 재료 분석(analyze_material CURRENT) → 함정 틀(trap_frame CURRENT) → apply_frame. 같은 틀 적용 재료와
틀로 공고마다 v12·v13 을 한 번씩 만든다 (v12 는 틀 입력 칸이 없다 — 틀 적용 재료만 받는다, 지금 운영과 같음).

지표 (실행 전 고정, 2026-11-26):
- drawn1: 1단계에서 보이는 곳(제목·상황·요구·공개 수용 기준·제출물·자료 제목·본문·추가 요구 주제·답)에 금지 엔티티를 저장 대상으로 그린
  초안 수 — 테이블 정의·조회 SQL·'세션 테이블'·Session 엔티티 클래스·SessionRepository 같은 꼴 (정규식 STORAGE)
- drawn2: 2단계(배포 뒤 증상·자료·제출물)에서 같은 것 — 참고 (2단계 자료는 지원자 구현의 로그라 흔한 구현을 비출 수 있다)
- ok_final: 초안 검사 통과 (재요청·고쳐 쓰기 뒤, run_feature 와 같은 정의)
- 판단: v13 의 drawn1 이 v12 보다 적고 ok_final 이 v12 보다 나쁘지 않으면 v13 이 맞게 동작한 것으로 본다. 걸린 줄은 읽어서 오탐을 뺀다
출력: data/eval/spec/<날짜>-feature-frame[-tag]/ (report.json, samples.md — 걸린 줄과 1단계 자료) — git 밖.

사용 (레포 루트에서, 모듈로):
  uv run python -m eval.spec.run_feature_frame --n 3
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import re
import tempfile
import uuid
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from eval.interview.run_material_analysis import DEADLOCK, JAVA, _pseudonymized, _virtual
from eval.spec.run_feature import ROOT, SUB, ModelOverride, make_client, pick
from task_spec.config import Settings
from task_spec.interview.material_analysis import analyze_material, material_block
from task_spec.interview.trap_frame import (
    apply_frame,
    frame_block,
    frame_problems,
    frame_texts,
    make_frame,
)
from task_spec.llm import LLMClient
from task_spec.nodes.pseudonymize import InterviewPseudonymizer
from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry
from task_spec.schemas import (
    DraftFeatureOutput,
    FeatureSeed,
    MaterialAnalysis,
    PublicIncident,
    TrapFrame,
)
from task_spec.schemas.incident import ContractNeed
from task_spec.spec.draft import FLAG_DRAFT_INVALID, DraftContext
from task_spec.spec.feature import draft_feature_task
from task_spec.spec.incidents import incident_by_id, load_incidents
from task_spec.starter.templates import Template, load_template

STORAGE = re.compile(
    r"create\s+table\s+[`\"']?\w*session"
    r"|\b(?:from|join|update|into)\s+[`\"']?\w*sessions?\b"
    r"|\b\w*sessions?\w*\s*(?:테이블|table)"
    r"|세션\s*(?:테이블|엔티티|저장\s*테이블|레코드|행)"
    r"|class\s+\w*Session\w*"
    r"|\b\w*Session\w*Repository\b"
    r"|@Table\s*\(\s*name\s*=\s*[\"']\w*session",
    re.IGNORECASE,
)


# 알파용 세 번째 허용 설계 (#318 5단계) — ⑧ 설계 질문에 담당자가 '뺀다 — <대신 인정할 설계>' 로 줄 법한 것. 하네스의 피크 버스트는
# 요청마다 다른 행이라 세션 확인을 요청마다 DB 없이 하는 설계만 통과한다 (2026-11-24 세션 과제 알파 1~3 이 JWT 설계에 메모리 맵을 더해 수렴한 꼴)
SIGNED_WITH_MEMORY = (
    "[설계] 로그인 때 서명한 세션 값을 발급하고, 마지막 접근 시각은 서버 메모리 맵에서만 갱신해 만료를 판정 — 조건: 서명 검사로 위조를 "
    "막고 메모리 맵은 서버끼리 공유 — 함정: 접근 시각을 요청마다 DB 에 쓰면 흔한 구현과 같다"
)


def session_material(
    root: Path, *, extra_designs: Sequence[str] = (), count: bool = False
) -> PublicIncident:
    """가상 세션 유지 재료 — 교착 공개 재료의 칸 이름만 빌리고 내용은 모두 이 재료의 것으로 (2단계·금지 낱말 포함).
    extra_designs: 허용 설계를 더한다 (알파 — 사람이 JWT 설계를 빼도 허용 설계가 둘 남게).
    count: 사유를 요청당 DB 사용 세기(db_per_request, 템플릿 Java v7 — #339)로, 필수 조절값 없이. 아니면 대기 시간 사유(보정)."""
    base = incident_by_id(load_incidents(root), DEADLOCK)
    assert base is not None
    return _virtual(
        base,
        "co:sw.backend:virtual-session-draft",
        symptom="피크 시간대 로그인 유지 요청이 늘면 일부 요청이 DB 연결 획득 시간 초과로 실패",
        root_cause="요청마다 세션 테이블을 조회하고 마지막 접근 시각을 갱신해 요청 하나가 DB 문장을 여러 개 실행한다 — 연결을 쥐는 "
        "시간이 길어 대기가 쌓인다",
        naive_fix="커넥션 풀 크기를 늘린다",
        why_naive_fails="부하가 바뀌면 다시 대기가 쌓인다 — 요청당 DB 일을 줄이지 않았다",
        notes="커넥션 풀 수치·동시 접속 규모는 확인되지 않음",
        key_insight="연결 사용 중이 최대치이고 대기 수가 늘면, 요청 하나가 DB 를 몇 번 오가는지(세션 확인 포함)를 센다.",
        deep_structure="요청마다 공유 자원(DB 연결)을 쥐는 시간이 늘면, 동시 요청 수가 풀 크기를 넘는 피크에 대기가 한도를 넘어 "
        "실패한다 — 요청당 공유 자원 사용을 줄이는 설계가 필요하다.",
        surface=[
            "세션 확인 → API 키 확인·권한 조회",
            "DB 커넥션 풀 → 외부 API 연결 풀",
            "로그인 유지 → 장바구니·최근 본 상품",
        ],
        tech=["Java", "Spring Boot", "JPA/Hibernate"],
        hypotheses=[],
        avoid_terms=[],
        hidden_terms=[],
        accepted_fixes=[],
        source_literals=[],
        known_incident_cases=[],
        business_need="로그인 API 를 추가하고, 로그인으로 받은 세션 값으로 기존 조회 API(상품·주문)를 쓰게 한다. 세션은 마지막 접근 뒤 "
        "30분 동안 유효하고 접근할 때마다 만료가 연장된다. 만료되었거나 없는 세션이면 401 을 준다.",
        naive_impl="세션을 DB 테이블에 저장하고, 요청마다 세션 테이블에서 세션을 조회해 마지막 접근 시각을 갱신한 뒤 본 처리를 한다",
        # 요청당 DB 사용 세기면 채점 기준이 결과에서 설계로 (#339 D4) — 숨은 요구에 세는 문장을 둔다. 틀이 이 글을 그대로 담지 않으면
        # 재료 것이 남는다(keeps_hidden)
        hidden_requirement="피크 시간대에도 로그인 유지 확인이 DB 연결 대기로 실패하지 않는다"
        + (". 세션 확인 경로는 요청마다 DB 를 쓰지 않는다" if count else ""),
        trigger="피크 시간대 동시 요청 수가 커넥션 풀 크기보다 크다. 운영 설정과 피크 시간대 지표를 봐야만 보인다.",
        why_naive_breaks="요청 하나가 세션 조회·갱신까지 DB 문장 약 4개를 실행해 연결을 오래 쥐고, 동시 요청이 풀보다 많으면 뒤 요청의 연결 "
        "대기가 대기 한도를 넘는다",
        discoverable_by=[
            "운영 환경 설정 (DB 연결 최대값·요청 처리 스레드 수)",
            "피크 시간대 요청 지표 (동시 처리 수)",
            "조회 API 요청당 DB 문장 수 지표",
        ],
        accepted_designs=[
            "[설계] 서명된 토큰(JWT)으로 세션을 확인해 요청마다 DB 를 보지 않음 — 조건: 만료 시각은 발급할 때 토큰에 담는다 — "
            "함정: 폐기를 확인하려고 요청마다 DB 를 보면 흔한 구현과 같다",
            "[설계] 세션을 메모리 저장소에 두고 접근할 때 만료를 연장, DB 에는 로그인 때만 씀 — 조건: 저장소를 서버끼리 공유 — "
            "함정: 메모리 저장소를 쓰면서 요청마다 DB 에도 반영하면 흔한 구현과 같다",
            *extra_designs,
        ],
        stage2_symptom="배포 뒤 피크 시간대에만 상품·주문 조회 요청 일부가 일정한 간격으로 시간 초과되며 실패한다. 그 시간대에도 DB "
        "서버 자원과 쿼리 시간은 평소와 비슷하다.",
        stage2_evidence=[
            "DB 연결 상태 로그 (사용 중·유휴·대기 수)",
            "애플리케이션 오류 로그 (커넥션 획득 시간 초과)",
            "요청당 DB 문장 수 지표",
        ],
        stage2_cause_terms=["요청마다 세션 조회·갱신", "연결 보유 시간", "커넥션 대기"],
        leak_terms=[
            "커넥션 풀",
            "connection pool",
            "풀 크기",
            "세션 테이블",
            "연결 보유 시간",
            "커넥션 고갈",
        ],
        trigger_terms=[
            "동시 요청",
            "동시에 요청",
            "동시 처리",
            "요청이 몰",
            "피크",
            "스레드",
            "연결 수",
            "DB 연결",
        ],
        fix_markers=[],
        starter=None,
        reviewed=False,
        contract_needs=[
            ContractNeed.model_validate(
                {
                    "kind": "stateful_target",
                    "note": "세션(로그인 상태) 엔티티 — 이미 있는 엔티티, 시드 행, 숨은 케이스가 바꿀 행",
                }
            )
        ],
        reasons=["db_per_request"] if count else ["pool_acquire_timeout"],
        harness=[] if count else ["pool_size", "concurrency"],
        harness_notes={},
        harness_rules={},
        fault_by=None,
    )


class FrameRun(NamedTuple):
    frame: TrapFrame
    framed: FeatureSeed  # 틀 적용 재료 (사람이 뺀 설계 반영)
    analysis: MaterialAnalysis
    notes: list[str]  # apply_frame 이 바꾼 것
    cost_usd: float


async def frame_run(
    llm: LLMClient, inc: PublicIncident, template: Template, removed: Sequence[str] = ()
) -> FrameRun:
    """운영 순서로 틀을 한 번 만든다 — 재료 분석 → 틀 (레포 CURRENT), 저장본·derived 가명화 검사.
    removed: ⑧ 설계 질문에 '뺀다'라고 답한 설계 (재료 원문 앞부분) — 서비스의 다음 과제 만들기처럼 apply_frame 이 뺀다."""
    assert inc.feature is not None
    prompts = PromptRegistry(ROOT / "prompts")
    with tempfile.TemporaryDirectory(prefix="feature-frame-") as tmp:
        pseudo = InterviewPseudonymizer(
            Detectors(load_dictionaries(), use_kiwi=False), Path(tmp) / "mappings"
        )
        mapping = pseudo.new_mapping(f"int-{uuid.uuid4().hex}", "가상회사")
        analysis, pa = await analyze_material(
            pseudo.stored_json(_pseudonymized(material_block(inc), pseudo, mapping), mapping),
            template,
            llm=llm,
            prompts=prompts,
            ontology_version="onto-eval",
        )
        frame, pf = await make_frame(
            pseudo.stored_json(
                _pseudonymized(frame_block(inc, analysis), pseudo, mapping), mapping
            ),
            template,
            llm=llm,
            prompts=prompts,
            ontology_version="onto-eval",
        )
        for text in frame_texts(frame):
            pseudo.derived(text, mapping)
    problems = frame_problems(frame, template)
    if problems or not frame.forbidden_storage:
        raise SystemExit(f"틀이 점검에 맞지 않음 (금지 엔티티 없음 또는 틀 검사): {problems}")
    framed, notes = apply_frame(inc.feature, frame, removed)
    return FrameRun(
        frame, framed, analysis, notes, float(pa.cost_usd or 0.0) + float(pf.cost_usd or 0.0)
    )


def _texts(out: DraftFeatureOutput) -> tuple[list[str], list[str]]:
    """(1단계 보이는 글, 2단계 글) — 숨은 칸(숨은 요구·허용 설계·행동 기준·관찰 개념)과 매칭 낱말은 넣지 않는다."""
    one = [out.title, out.scenario, *out.deliverables]
    one += [r.model_dump_json() for r in out.requirements]
    one += [a.model_dump_json() for a in out.acceptance_public]
    one += [f"{a.title}\n{a.content}" for a in out.artifacts]
    for c in out.clarifications:
        one += [c.topic, c.answer or "", getattr(c, "answer_hint", "") or ""]
    two: list[str] = []
    if out.debug_stage is not None:
        d = out.debug_stage
        two = [d.scenario, *d.deliverables, *(f"{a.title}\n{a.content}" for a in d.artifacts)]
    return one, two


def _hits(texts: list[str]) -> list[str]:
    out: list[str] = []
    for t in texts:
        for m in STORAGE.finditer(t):
            s, e = max(0, m.start() - 40), min(len(t), m.end() + 40)
            out.append(re.sub(r"\s+", " ", t[s:e]))
    return out


async def run_one(
    llm: LLMClient,
    version: int,
    pid: str,
    ctx: DraftContext,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    async with sem:
        reg: Any = ModelOverride(PromptRegistry(ROOT / "prompts"), None, version)  # 판 고정 래퍼
        res = await draft_feature_task(ctx, llm=llm, prompts=reg)
    row: dict[str, Any] = {
        "posting": pid,
        "version": version,
        "ok_final": res.output is not None
        and not [f for f in res.flags if f.startswith(FLAG_DRAFT_INVALID)],
        "flags": res.flags,
        "cost_usd": sum(p.cost_usd or 0.0 for p in res.provenance),
    }
    if isinstance(res.output, DraftFeatureOutput):
        one, two = _texts(res.output)
        row |= {
            "drawn1": _hits(one),
            "drawn2": _hits(two),
            "artifacts": [f"{a.title}\n{a.content}" for a in res.output.artifacts],
        }
    else:
        row |= {"drawn1": [], "drawn2": [], "artifacts": []}
    print(
        f"{pid} v{version}: 통과 {row['ok_final']} · 1단계 저장 대상 {len(row['drawn1'])} · 2단계 {len(row['drawn2'])}"
    )
    return row


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for v in sorted({r["version"] for r in rows}):
        rs = [r for r in rows if r["version"] == v]
        out[f"v{v}"] = {
            "n": len(rs),
            "ok_final": sum(r["ok_final"] for r in rs),
            "drawn1": sum(bool(r["drawn1"]) for r in rs),
            "drawn2": sum(bool(r["drawn2"]) for r in rs),
            "cost_usd": round(sum(r["cost_usd"] for r in rs), 4),
        }
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--n", type=int, default=3, help="시범 공고 수 (공고마다 v12·v13 한 번씩)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    s = Settings()  # type: ignore[call-arg]
    llm = make_client(None)
    template = load_template(JAVA)
    inc = session_material(s.incidents_dir)
    run = await frame_run(llm, inc, template)
    frame, framed, frame_cost = run.frame, run.framed, run.cost_usd
    print(
        f"틀: 금지 엔티티 {[x.entity for x in frame.forbidden_storage]} · 공개할 것 {frame.public_rules}"
    )
    repo = await OntologyRepository.open(s.database_url.get_secret_value(), "dev")
    picked, _ = await pick(
        repo, repo.version.version, load_incidents(s.incidents_dir), SUB, force=True, limit=a.n
    )
    seeded = inc.model_copy(update={"feature": framed})
    sem = asyncio.Semaphore(a.concurrency)
    jobs = []
    for pid, built in picked:
        assert built.ctx is not None
        ctx = dataclasses.replace(built.ctx, incident=seeded, trap_frame=frame)
        jobs += [run_one(llm, v, pid, ctx, sem) for v in (12, 13)]
    rows = list(await asyncio.gather(*jobs))
    report = {
        "summary": summary(rows),
        "frame_cost_usd": round(frame_cost, 4),
        "frame": frame.model_dump(),
        "rows": rows,
    }
    print(json.dumps(report["summary"], ensure_ascii=False))
    out = (
        ROOT
        / "data"
        / "eval"
        / "spec"
        / f"{date.today()}-feature-frame{('-' + a.tag) if a.tag else ''}"
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    md = [
        f"# 명세 초안 틀 점검 — {date.today()}",
        "",
        "```json",
        json.dumps(report["summary"], ensure_ascii=False),
        "```",
    ]
    for r in rows:
        md += [
            "",
            f"## {r['posting']} · v{r['version']} — 통과 {r['ok_final']}",
            "",
            "1단계 걸린 줄:",
        ]
        md += [f"- {h}" for h in r["drawn1"]] or ["- 없음"]
        md += ["", "2단계 걸린 줄:"]
        md += [f"- {h}" for h in r["drawn2"]] or ["- 없음"]
        md += ["", "1단계 자료:"]
        md += [f"```\n{x}\n```" for x in r["artifacts"]]
    (out / "samples.md").write_text("\n".join(md), "utf-8")
    print(f"결과: {out}")


if __name__ == "__main__":
    asyncio.run(main())
