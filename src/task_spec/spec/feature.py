"""기능 개발형 공통 과제 서술 (draft_feature_task LLM) — 1단계 구현 → 1단계 제출 뒤 2단계 디버깅 [확정: 2026-11-06 사용자].

입력은 공고·온톨로지·공개 사례뿐 (불변식 3). 골격 값(역량·수준·기술 환경·난이도·시간)은 LLM 이 바꾸지 않는다.
생성 루프·재요청·고쳐 쓰기 정책은 이슈형과 같다 (spec/draft.py draft_task) — 검사·고쳐 쓰기·자료 섞기만 이 모듈 것.

검증 (설계 docs/design/feature-task-backend.md §6.3·§4.3)
- 이슈형 공통 규칙: 수행준거 id·역량마다 인용, 기술 환경 밖 언어, 1단계 자료 규칙, 원 사례 베끼기·고유어.
- id 참조: 숨김 수용 기준·추가 요구 reveals·허용 설계·2단계 trap_ids·catches_trap 는 traps 안에서만.
  숨은 요구마다 숨김 수용 기준 1개 이상·허용 설계 2개 이상. asks_clarification 은 clarifications 안에서만.
- 숨은 요구 노출 금지: 먼저 말할 개념(1·2단계 first_mention_of)·원 사례 원인 용어가 지원자에게 보이는 곳
  (제목·상황·공개 요구·공개 수용 기준·산출물·자료 제목·추가 요구 답·2단계 상황)에 나오지 않는다.
- 구현 방법 지시 금지 (1차, 금지어): 제목·공개 요구·공개 수용 기준에 구현 수단 이름을 쓰지 않는다.
- 2단계: 자료 id 가 1단계와 겹치지 않음, 2단계 자료 규칙(핵심 1·방해 1 이상), requests_artifact 는 2단계 자료 안에서만.
- 운영 조건 노출 금지 (재료 trigger_terms): 1단계 제목·상황·공개 요구·공개 수용 기준에 운영 조건을 쓰지 않는다 — 자료·구체 답에서만.
- 두 층 답의 매칭 낱말: 단서 추가 요구의 match_terms 중 공개 글에 이미 있는 말·두 글자 이하 일반어는 코드가 지운다 (재요청 없음).
  다 지워지면 진행자는 늘 모호한 답만 주고, 승인자 화면에 표시한다 (FLAG_HINT_ONLY).
- v5 단서 자료: 단서 추가 요구의 모호한 답은 생성 모델이 지목한 1단계 자료로 코드가 만든다 ("'<제목>' 자료에서 확인할 수 있습니다.").
  지목한 제목이 1단계 자료에 없거나 방해 자료면 재요청. 1단계 자료 제목의 운영 조건 표현도 재요청 (재료 자료 종류 이름의 말은 괜찮음).
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable
from typing import Any, cast

from task_spec.llm import LLMClient
from task_spec.prompting import PromptRegistry
from task_spec.schemas import (
    AcceptedDesign,
    ClarificationDraft,
    DebugStage,
    DraftFeatureOutput,
    DraftFeatureOutputV2,
    DraftFeatureOutputV4,
    DraftFeatureOutputV5,
    FeatureSeed,
    Observable,
    PublicIncident,
    TaskItem,
    Trap,
)
from task_spec.spec.common import (
    FEATURE_BUILD_MIN,
    FEATURE_DEBUG_MIN,
    FLAG_NO_INCIDENT,
    FLAG_PUBLIC_INCIDENT,
)
from task_spec.spec.draft import (
    _ART_ID,
    DraftContext,
    DraftResult,
    _norm,
    _phrase,
    check,
    check_artifact_list,
    draft_task,
    incident_copied,
    repair,
)

TASK = "draft_feature_task"
SEED_TRAP = "t1"  # v2: 재료의 숨은 요구 하나
# 기능형 1단계 자료는 5~8개라 이슈형(방해 3개 이상)보다 한 단계 낮춘다 [2026-11-06 — v2 직군별 점검에서 4/5 가 이 규칙에만 걸림]
FEATURE_MIN_DISTRACTOR = 2
# 1단계 자료가 2단계(배포 뒤) 자료와 같은 종류인지 볼 때 빼는 흔한 말 — 자료 종류 낱말만 남긴다
_GENERIC = frozenset(
    "로그 지표 결과 기록 자료 시계열 표본 발췌 내역 목록 현황 상태 운영 환경 시점 최근 일부 예시 표 코드".split()
)
STAGE_OVERLAP = 0.5  # 핵심 낱말 Jaccard 이 이 이상이면 같은 종류로 본다


def _kind_words(title: str) -> set[str]:
    bare = re.sub(r"\([^)]*\)", " ", title)
    words = {w for w in re.split(r"[\s·,/]+", bare.lower()) if len(w) >= 2}
    return words - _GENERIC


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _same_kind(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and _jaccard(a, b) >= STAGE_OVERLAP


def stage2_kinds_in_stage1(
    titles: list[str], evidence: list[str], discoverable: list[str] | None = None
) -> list[str]:
    """1단계 자료 중 2단계 핵심 증거(재료 stage2_evidence 첫 항목)와 같은 종류 — 기능이 아직 없으니 배포 뒤 증거가 1단계에 있으면 안 된다.

    다른 2단계 자료(서버 응답 로그 같은 방해 자료)는 배포 전에도 있으므로 보지 않는다. 재료가 1단계 단서(discoverable_by)로도
    적어 둔 종류는 1단계에 있어도 된다."""
    disc = [_kind_words(d) for d in discoverable or []]
    kinds = [
        k for k in (_kind_words(e) for e in evidence[:1]) if not any(_same_kind(k, d) for d in disc)
    ]
    out = []
    for t in titles:
        w = _kind_words(t)
        if not any(_same_kind(w, k) for k in kinds):
            continue
        # 1단계 단서(discoverable_by)에 더 가까우면 그 자료다 — 2026-11-09 알파 kakaobank_2026#005: 단서 '평시 DB 연결 사용량
        # 대시보드' 그대로인 제목이 2단계 'DB 연결 상태 로그'(일반어를 빼면 {db, 연결})와 0.5 로 겹쳐 재요청에도 못 고침
        if max((_jaccard(w, d) for d in disc), default=0.0) > max(_jaccard(w, k) for k in kinds):
            continue
        out.append(t)
    return out


FLAG_SEED_UNREVIEWED = "기능형 재료(공개 사례 feature 블록)가 사람 검토 전 (reviewed=false) — 숨은 요구·허용 설계를 승인자가 확인"
FLAG_HINT_ONLY = (
    "추가 요구 단서 답에 구체 답 조건(match_terms)이 없음 — 매칭 낱말이 모두 공개 글에 이미 있는 말이거나 두 글자 이하 일반어라 지움, "
    "진행자는 늘 모호한 답만 줌 (구체 답이 필요하면 승인자가 매칭 낱말을 적어 수정 요청): "
)
FLAG_NO_STARTER = "이 기능형 재료에 연결된 시작 코드 예제가 없음 — 명세만 (시작 코드·숨은 테스트는 빌드 단계에서 만들어야 함)"
FLAG_NO_SEED = (
    "기능형 v2 프롬프트인데 이 직군 공개 사례에 기능형 재료(feature)가 없음 — 서술 없이 골격만"
)
# 공개 요구·수용 기준에 나오면 "어떻게"를 알려 주는 말 (구현 수단). 1차 금지어 — 2차는 판정 LLM (평가 PR)
HOWTO_TERMS = (
    "트랜잭션",
    "transaction",
    "락",
    "lock",
    "잠금",
    "비관적",
    "낙관적",
    "멱등",
    "idempotent",
    "idempotency",
    "원자적",
    "atomic",
    "뮤텍스",
    "mutex",
    "세마포어",
    "semaphore",
    "아웃박스",
    "outbox",
    "재시도",
    "retry",
    "커넥션 풀",
    "connection pool",
    "requires_new",
    "for update",
    "버전 컬럼",
    "분산 락",
    "메시지 큐",
    "message queue",
)
_HANGUL = re.compile(r"[가-힣]")


def seed_block(inc: PublicIncident) -> str:
    """v2 프롬프트에 넣는 기능형 재료 — 원리와 사람이 적은 feature 블록만. 조직명·제목·URL·원문 증상·수치는 넣지 않는다."""
    f = inc.feature
    assert f is not None
    lines = [
        f"원리(유지할 심층 구조): {inc.deep_structure}",
        f"만들 기능 (1단계 상황의 재료 — 이것만으로 상황을 쓴다): {f.business_need}",
        f"흔한 구현 (AI 첫 답): {f.naive_impl}",
        f"숨은 요구 t1: {f.hidden_requirement}",
        f"숨은 요구를 깨는 운영 조건 (1단계 자료에서 보여야 함): {f.trigger}",
        f"흔한 구현이 깨지는 이유: {f.why_naive_breaks}",
        "조건이 보이는 1단계 자료 종류: " + ", ".join(f.discoverable_by),
        "허용 설계 (정답으로 인정 — 공개 요구에 쓰지 않음): " + " / ".join(f.accepted_designs),
        f"2단계 겉 증상 (원인 없이): {f.stage2_symptom}",
        "2단계 자료 종류: " + ", ".join(f.stage2_evidence),
        "2단계에서 먼저 말할 원인 개념 (지원자 화면 금지): " + ", ".join(f.stage2_cause_terms),
        "지원자 화면에 쓰면 안 되는 말: " + ", ".join([*f.leak_terms, *f.stage2_cause_terms]),
        "바꿀 수 있는 표면: " + ", ".join(inc.surface),
    ]
    if f.task_shape == "analysis":
        lines.append("과제 형태: analysis (구현 대신 재실행 가능한 분석 코드와 결과)")
    return "\n".join(lines)


def _design(i: int, text: str) -> AcceptedDesign:
    """'[설계] 요약 — 조건: … — 함정: …' → 요약 / 나머지(조건·함정)."""
    body = text.split("] ", 1)[1] if text.startswith("[") and "] " in text else text
    summary, _, rest = body.partition(" — ")
    return AcceptedDesign(
        design_id=f"d{i}",
        summary=summary.strip()[:300],
        conditions=(rest.strip() or "조건 없음")[:600],
        trap_ids=[SEED_TRAP],
    )


def fill_from_seed(out: DraftFeatureOutputV2, seed: FeatureSeed) -> DraftFeatureOutput:
    """v2 출력 + 재료 → 전체 기능형 출력. 숨은 요구·허용 설계는 재료 그대로, 2단계 원인 개념은 재료 것을 앞에 둔다."""
    trap = Trap(
        trap_id=SEED_TRAP,
        description=f"{seed.hidden_requirement} (운영 조건: {seed.trigger})",
        why_ai_fails=f"{seed.naive_impl} — {seed.why_naive_breaks}",
    )
    dbg = out.debug_stage
    seeded = [Observable(kind="first_mention_of", value=t) for t in seed.stage2_cause_terms[:2]]
    have = {o.value for o in dbg.observables if o.kind == "first_mention_of"}
    obs2 = [*(o for o in seeded if o.value not in have), *dbg.observables]
    data = out.model_dump()
    data["traps"] = [trap.model_dump()]
    data["accepted_designs"] = [
        _design(i, t).model_dump() for i, t in enumerate(seed.accepted_designs, 1)
    ]
    data["debug_stage"] = {
        **dbg.model_dump(),
        "trap_ids": [SEED_TRAP],
        "observables": [o.model_dump() for o in obs2][:6],
    }
    return DraftFeatureOutput.model_validate(data)


STAGE2_ALREADY = (
    "1단계에서 이미 대응했다면, 이 증상이 내 구현에서 생기지 않는 근거 (테스트·설계 설명)"
)
MAX_CLARIFICATIONS = 2  # 질문 한도 (README·진행자 공통 규칙과 같게)


def _limit(arts: list[Any], cap: int) -> int:
    """요청 한도 = 핵심 자료 수 + 1 (자료 수 − 1 이하, cap 이하) — 골라서 요청하게, 핵심은 다 볼 수 있게."""
    keys = sum(a.role == "key" for a in arts)
    return max(1, min(keys + 1, len(arts) - 1, cap)) if keys <= cap else cap


SCENARIO_MAX = 3000  # DraftFeatureOutput.scenario
DEBUG_SCENARIO_MAX = 2000  # DebugStageDraft.scenario
DRAFT_MATCH_TERMS_MAX = 5  # ClarificationDraft.match_terms (v5 출력은 6개까지)


def _with_note(body: str, note: str, cap: int) -> str:
    """본문 + 코드가 붙이는 안내. 합쳐 cap 을 넘으면 본문을 마지막 문장 끝에서 자른다 (안내는 늘 남긴다)."""
    body = body.rstrip()
    room = cap - len(note)
    if len(body) > room:
        cut = body[:room]
        end = cut.rfind(".")
        body = cut[: end + 1] if end > 0 else cut.rstrip()
    return body + note


def fill_from_seed_v4(out: DraftFeatureOutputV4, seed: FeatureSeed) -> DraftFeatureOutput:
    """v4 출력 → 전체 기능형 출력. 계산할 수 있는 칸은 여기서 정한다 (LLM 에 맡기지 않음):
    자료 번호(1단계 a1…, 2단계 이어서)·요청 한도·질문 한도·관찰 행동·2단계 연결·공통 문구, 숨은 요구·허용 설계(재료)."""
    s1 = [a.model_copy(update={"artifact_id": f"a{i}"}) for i, a in enumerate(out.artifacts, 1)]
    n = len(s1)
    s2 = [
        a.model_copy(update={"artifact_id": f"a{n + i}"})
        for i, a in enumerate(out.debug_stage.artifacts, 1)
    ]
    lim1, lim2 = _limit(s1, 4), _limit(s2, 3)
    clars = [
        {**c.model_dump(), "reveals": [SEED_TRAP] if c.reveals else []} for c in out.clarifications
    ]
    max_q = min(len(clars), MAX_CLARIFICATIONS)
    obs1 = [
        *({"kind": "first_mention_of", "value": c} for c in out.concepts),
        {"kind": "catches_trap", "value": SEED_TRAP},
        *({"kind": "requests_artifact", "value": a.artifact_id} for a in s1 if a.role == "key"),
        *(
            {"kind": "asks_clarification", "value": c["clarification_id"]}
            for c in clars
            if c["reveals"]
        ),
    ]
    obs2 = [
        *({"kind": "first_mention_of", "value": t} for t in seed.stage2_cause_terms[:2]),
        *({"kind": "requests_artifact", "value": a.artifact_id} for a in s2 if a.role == "key"),
        {"kind": "catches_trap", "value": SEED_TRAP},
    ][:6]
    deliv2 = list(out.debug_stage.deliverables)
    if not any("이미 대응" in d for d in deliv2):
        deliv2.append(STAGE2_ALREADY)
    scenario = _with_note(
        out.scenario,
        f"\n\n아래 자료 중 최대 {lim1}개를 요청해 볼 수 있고, 질문은 최대 {max_q}개까지 할 수 있습니다.",
        SCENARIO_MAX,
    )
    debug_scenario = _with_note(
        out.debug_stage.scenario,
        f"\n\n아래 자료 중 최대 {lim2}개를 요청해 볼 수 있습니다.",
        DEBUG_SCENARIO_MAX,
    )
    data = {
        **out.model_dump(exclude={"concepts", "debug_stage", "clarifications", "artifacts"}),
        "scenario": scenario,
        "artifacts": [a.model_dump() for a in s1],
        "max_requests": lim1,
        "clarifications": clars,
        "max_clarifications": max_q,
        "acceptance_hidden": [
            {**h.model_dump(), "trap_id": SEED_TRAP} for h in out.acceptance_hidden
        ],
        "observables": obs1[:10],
        "traps": [
            {
                "trap_id": SEED_TRAP,
                "description": f"{seed.hidden_requirement} (운영 조건: {seed.trigger})",
                "why_ai_fails": f"{seed.naive_impl} — {seed.why_naive_breaks}",
            }
        ],
        "accepted_designs": [
            _design(i, t).model_dump() for i, t in enumerate(seed.accepted_designs, 1)
        ],
        "debug_stage": {
            "scenario": debug_scenario,
            "trap_ids": [SEED_TRAP],
            "artifacts": [a.model_dump() for a in s2],
            "max_requests": lim2,
            "deliverables": deliv2[:3],
            "observables": obs2,
        },
    }
    return DraftFeatureOutput.model_validate(data)


HINT_POINTER = "'{title}' 자료에서 확인할 수 있습니다."
_POINTER_RX = re.compile(r"^'(.+)' 자료에서 확인할 수 있습니다\.$")
_QUOTES = "'\"“”‘’「」『』"


def pointer_hint(title: str) -> str:
    """v5 단서 추가 요구의 모호한 답 — 볼 곳만 (사실·수치·조건 없음). 자료 제목은 이미 누출 검사를 받는다."""
    return HINT_POINTER.format(title=title)


def _match_title(source: str, titles: list[str]) -> str:
    """지목한 자료 → 1단계 자료 제목 (공백·대소문자·따옴표 무시, 하나로 정해질 때만). 못 정하면 그대로 — check_hints 가 재요청."""
    want = _norm(source.strip().strip(_QUOTES))
    same = [t for t in titles if _norm(t) == want]
    if same:
        return same[0]
    near = [t for t in titles if want and (want in _norm(t) or _norm(t) in want)]
    return near[0] if len(near) == 1 else source.strip()


def fill_from_seed_v5(out: DraftFeatureOutputV5, seed: FeatureSeed) -> DraftFeatureOutput:
    """v5 출력 → v4 출력 → 전체 기능형 출력 [2026-11-06 사용자 (B)].

    단서 추가 요구(reveals 있음)의 모호한 답은 생성 모델이 지목한 1단계 자료(hint_source)로 코드가 만든다 —
    생성 모델은 모호한 답 문장을 쓰지 않으니 운영 조건을 흘릴 자리가 없다. 단서가 아닌 추가 요구는 생성 모델의 모호한 답 그대로.
    """
    titles = [a.title for a in out.artifacts]
    clars = [
        ClarificationDraft(
            clarification_id=c.clarification_id,
            topic=c.topic,
            match_terms=list(c.match_terms)[:DRAFT_MATCH_TERMS_MAX],
            answer=c.answer,
            reveals=list(c.reveals),
            answer_hint=pointer_hint(_match_title(c.hint_source or "", titles))
            if c.reveals
            else (c.answer_hint or ""),
        )
        for c in out.clarifications
    ]
    v4 = DraftFeatureOutputV4.model_validate(
        {
            **out.model_dump(exclude={"clarifications"}),
            "clarifications": [c.model_dump() for c in clars],
        }
    )
    return fill_from_seed_v4(v4, seed)


def extra_input() -> dict[str, Any]:
    return {"build_min": FEATURE_BUILD_MIN, "debug_min": FEATURE_DEBUG_MIN}


def _term_rx(term: str) -> re.Pattern[str]:
    """한글 말은 부분 문자열(조사가 붙음), 영문은 단어 경계 (draft._phrase)."""
    return re.compile(re.escape(term)) if _HANGUL.search(term) else _phrase(term)


def _concepts(out: DraftFeatureOutput) -> list[str]:
    obs = [*out.observables, *out.debug_stage.observables]
    vals = [o.value for o in obs if o.kind == "first_mention_of" and len(_norm(o.value)) >= 2]
    return list(dict.fromkeys(vals))


def _dup(label: str, ids: list[str]) -> list[str]:
    return [f"{label} id 가 겹침"] if len(set(ids)) != len(ids) else []


_DIGIT = re.compile(r"\d")


def _in_text(term: str, text: str) -> bool:
    """한글 말은 공백을 무시한 부분 문자열, 영문은 단어 경계 — 진행자가 질문에서 찾는 방식보다 넓게 본다."""
    if _HANGUL.search(term):
        t = _norm(term)
        return bool(t) and t in _norm(text)
    return bool(_phrase(term).search(text))


def find_terms(terms: Iterable[str], text: str) -> list[str]:
    """text 에 나온 말 (한글은 띄어쓰기 무시, 영문은 단어 경계) — 기능형 서술 검사와 같은 기준.

    다른 단계(공개 계약 검사 starter/contract.py 등)가 같은 낱말 목록·같은 기준을 쓰도록 공개한다.
    """
    return [t for t in dict.fromkeys(terms) if len(_norm(t)) >= 2 and _in_text(t, text)]


def public_text(out: DraftFeatureOutput) -> str:
    """지원자가 처음부터 보는 1단계 글 — 제목·상황·공개 요구·공개 수용 기준·제출물."""
    return "\n".join(
        [
            out.title,
            out.scenario,
            *(r.text for r in out.requirements),
            *(a.text for a in out.acceptance_public),
            *out.deliverables,
        ]
    )


def _too_general(term: str) -> bool:
    """띄어쓰기 없는 두 글자 이하 낱말 ("연결"·"설정"·"제한"·"DB") — 일반 질문에도 들어간다 [2026-11-06 사용자]."""
    t = term.strip()
    return not re.search(r"\s", t) and len(_norm(t)) <= 2


def prune_match_terms(out: DraftFeatureOutput) -> DraftFeatureOutput:
    """단서 추가 요구(reveals 있음·두 층 답)의 매칭 낱말 중 공개 글에 이미 있는 말과 두 글자 이하 일반어를 지운다
    [2026-11-06 사용자].

    공개 글의 말·일반어는 일반 질문에도 들어가므로 구체 답이 거의 늘 나간다 — 두 층 답이 갈리지 않는다.
    지우기만 하고 재요청하지 않는다 (토큰 0). 다 지워지면 진행자는 늘 모호한 답을 주고 apply_feature 가 표시를 남긴다.
    단서가 아닌 추가 요구는 구체 답이 나가도 해가 없어 그대로 둔다.
    """
    text = public_text(out)
    clars = []
    changed = False
    for c in out.clarifications:
        if c.reveals and c.answer_hint is not None and c.match_terms:
            kept = [t for t in c.match_terms if not _in_text(t, text) and not _too_general(t)]
            if kept != c.match_terms:
                c = c.model_copy(update={"match_terms": kept})
                changed = True
        clars.append(c)
    return out.model_copy(update={"clarifications": clars}) if changed else out


def hint_only(out: Any) -> list[str]:
    """구체 답 조건이 다 지워진 단서 추가 요구 id."""
    return [
        c.clarification_id
        for c in out.clarifications
        if c.reveals and c.answer_hint is not None and not c.match_terms
    ]


def _locate(term: str, text: str) -> re.Match[str] | None:
    """_in_text·_term_rx 와 같은 기준으로 원문에서 위치를 찾는다 (한글은 띄어쓰기 무시)."""
    if _HANGUL.search(term):
        body = r"\s*".join(re.escape(ch) for ch in _norm(term))
        return re.search(body, text, re.IGNORECASE) if body else None
    return _phrase(term).search(text)


def _quote(terms: list[str], text: str, limit: int = 2) -> str:
    """걸린 말이 든 문장을 재요청 사유에 붙인다 — 생성 모델이 고칠 곳을 바로 찾게 [2026-11-06 사용자].

    재요청 뒤에도 같은 표현이 남는 일이 잦았다 (gpt-4.1 v5 비교에서 상황 글 2/4). 문장이 길면 걸린 말 앞뒤만.
    """
    quotes: list[str] = []
    for t in dict.fromkeys(terms):
        m = _locate(t, text)
        if m is None:
            continue
        start = max(text.rfind(ch, 0, m.start()) for ch in ".?!\n") + 1
        ends = [i for i in (text.find(ch, m.end()) for ch in ".?!\n") if i != -1]
        end = min(ends) + 1 if ends else len(text)
        sent = text[start:end].strip()
        if len(sent) > 90:
            at = max(0, m.start() - start - 40)
            sent = ("…" if at else "") + sent[at : at + 90] + "…"
        q = f'"{sent}"'
        if q not in quotes:
            quotes.append(q)
        if len(quotes) >= limit:
            break
    return (" — 고칠 문장: " + " / ".join(quotes)) if quotes else ""


def check_hints(out: DraftFeatureOutput, ctx: DraftContext) -> list[str]:
    """두 층 답의 모호한 답(answer_hint) — 구체 답보다 엄격하다 [2026-11-06 사용자].

    - 숨은 요구를 드러내는 말(재료 leak_terms·원인 개념·먼저 말할 개념·원 사례 원인 용어)을 쓰지 않는다.
    - 단서가 되는 추가 요구(reveals 있음)의 모호한 답에는 수치를 쓰지 않는다 — 운영 조건의 값은 정확히 짚은 질문에만.
    - 단서 추가 요구의 모호한 답과 구체 답이 같으면 두 층이 아니다 (단서가 아니면 같아도 된다).
    매칭 낱말이 비는 것은 재요청 사유가 아니다 — 공개 글의 말을 지운 결과다 (prune_match_terms, 승인자 표시).
    v5 단서 자료 답("'<제목>' 자료에서 확인할 수 있습니다."): 제목이 1단계 자료에 있고 방해 자료가 아니어야 한다.
    제목 자체의 금지어·운영 조건 표현은 check_feature 가 자료 제목으로 본다.
    """
    hints = [c for c in out.clarifications if c.answer_hint is not None]
    if not hints:
        return []
    problems: list[str] = []
    inc = ctx.incident
    seed = inc.feature if inc is not None else None
    terms = [*_concepts(out)]
    if seed is not None:
        terms += [*seed.leak_terms, *seed.stage2_cause_terms]
    if inc is not None:
        terms += inc.hidden_terms
    arts = {a.title: a for a in out.artifacts}
    for c in hints:
        hint = c.answer_hint or ""
        pointed = _POINTER_RX.match(hint)
        if pointed:
            title = pointed.group(1)
            art = arts.get(title)
            if art is None:
                problems.append(
                    f"추가 요구 {c.clarification_id} 의 단서 자료 '{title}' 가 1단계 자료 제목에 없음 — "
                    "hint_source 는 1단계 artifacts 의 title 을 글자 그대로"
                )
            elif art.role == "distractor":
                problems.append(
                    f"추가 요구 {c.clarification_id} 의 단서 자료 '{title}' 가 방해(distractor) 자료 — "
                    "그 사실이 실제로 보이는 핵심·맥락 자료로"
                )
            continue
        shown = [t for t in dict.fromkeys(terms) if len(_norm(t)) >= 2 and _term_rx(t).search(hint)]
        if shown:
            problems.append(
                f"추가 요구 {c.clarification_id} 의 모호한 답에 숨은 요구를 드러내는 말: {', '.join(shown)}"
            )
        if c.reveals and _DIGIT.search(hint):
            problems.append(
                f"추가 요구 {c.clarification_id} 의 모호한 답에 수치가 있음 — 수치는 구체 답(정확히 짚은 질문)에만"
            )
        if c.reveals and _norm(hint) == _norm(c.answer):  # 단서가 아니면 같아도 해가 없다
            problems.append(f"추가 요구 {c.clarification_id} 의 모호한 답과 구체 답이 같음")
    return problems


def check_feature(out: DraftFeatureOutput, ctx: DraftContext) -> list[str]:
    """어긋난 점 목록 (비면 통과). 문장은 그대로 재요청 사유가 된다."""
    # 이슈형 공통 규칙 (제목·상황·산출물·1단계 자료 목록의 노출 검사, 1단계 관찰 행동 id 포함)
    problems = check(cast(Any, out), ctx, min_distractor=FEATURE_MIN_DISTRACTOR)
    trap_ids = [t.trap_id for t in out.traps]
    traps = set(trap_ids)
    s1 = [a.artifact_id for a in out.artifacts]
    dbg = out.debug_stage
    s2 = [a.artifact_id for a in dbg.artifacts]
    clar = [c.clarification_id for c in out.clarifications]

    problems += _dup("requirements(req_id)", [r.req_id for r in out.requirements])
    problems += _dup("acceptance_public(ac_id)", [a.ac_id for a in out.acceptance_public])
    problems += _dup("acceptance_hidden(ac_id)", [a.ac_id for a in out.acceptance_hidden])
    problems += _dup("clarifications", clar)
    problems += _dup("accepted_designs", [d.design_id for d in out.accepted_designs])

    # id 참조 — 숨은 요구(traps) 안에서만
    for h in out.acceptance_hidden:
        if h.trap_id not in traps:
            problems.append(f"숨김 수용 기준 {h.ac_id} 의 trap_id {h.trap_id} 가 traps 에 없음")
    for c in out.clarifications:
        bad = [t for t in c.reveals if t not in traps]
        if bad:
            problems.append(
                f"추가 요구 {c.clarification_id} 의 reveals 에 없는 trap_id: {', '.join(bad)}"
            )
    for d in out.accepted_designs:
        bad = [t for t in d.trap_ids if t not in traps]
        if bad:
            problems.append(
                f"허용 설계 {d.design_id} 의 trap_ids 에 없는 trap_id: {', '.join(bad)}"
            )
    for t in trap_ids:
        if not any(h.trap_id == t for h in out.acceptance_hidden):
            problems.append(f"숨은 요구 {t} 에 숨김 수용 기준이 없음")
        n = sum(t in d.trap_ids for d in out.accepted_designs)
        if n < 2:
            problems.append(
                f"숨은 요구 {t} 를 만족하는 허용 설계가 {n}개 — 2개 이상 (한 가지 정답으로 감점하지 않게)"
            )
    for o in out.observables:
        if o.kind == "asks_clarification" and o.value not in clar:
            problems.append(f"asks_clarification 값 {o.value} 가 clarifications 의 id 가 아님")
    if out.max_clarifications > len(out.clarifications):
        problems.append("max_clarifications 가 추가 요구 수보다 큼")
    problems += check_hints(out, ctx)

    # 2단계
    bad = [t for t in dbg.trap_ids if t not in traps]
    if bad:
        problems.append(f"2단계 trap_ids 에 없는 trap_id: {', '.join(bad)}")
    overlap = sorted(set(s1) & set(s2))
    if overlap:
        problems.append(f"2단계 자료 id 가 1단계 자료와 겹침: {', '.join(overlap)} (번호를 이어서)")
    problems += check_artifact_list(
        dbg.artifacts,
        scenario=dbg.scenario,
        limit=dbg.max_requests,
        min_distractor=1,
        label="2단계 ",
    )
    for o in dbg.observables:
        if o.kind == "requests_artifact" and o.value not in s2:
            problems.append(f"2단계 requests_artifact 값 {o.value} 가 2단계 자료 id 가 아님")
        if o.kind == "catches_trap" and o.value not in traps:
            problems.append(f"2단계 catches_trap 값 {o.value} 가 traps 의 trap_id 가 아님")
        if o.kind == "asks_clarification":
            problems.append(
                "2단계에는 추가 요구(asks_clarification)가 없음 — 1단계 observables 에만"
            )

    # 숨은 요구 노출 — check() 가 보지 않는 곳 + 2단계 개념
    concepts = _concepts(out)
    stage1_only = {o.value for o in out.observables if o.kind == "first_mention_of"}
    extra_visible = "\n".join(
        [
            *(r.text for r in out.requirements),
            *(a.text for a in out.acceptance_public),
            *(c.answer for c in out.clarifications),
        ]
    )
    base_visible = "\n".join(
        [out.title, out.scenario, *out.deliverables, *(a.title for a in out.artifacts)]
    )
    shown = [c for c in concepts if _norm(c) in _norm(extra_visible)]
    shown += [
        c for c in concepts if c not in stage1_only and _norm(c) in _norm(base_visible)
    ]  # 1단계 개념은 check() 가 이미 봤다
    if shown:
        problems.append(
            "먼저 말할 개념이 지원자에게 보이는 곳(공개 요구·수용 기준·추가 요구 답·상황 등)에 나옴: "
            + ", ".join(dict.fromkeys(shown))
        )
    dbg_visible = "\n".join([dbg.scenario, *dbg.deliverables, *(a.title for a in dbg.artifacts)])
    shown2 = [c for c in concepts if _norm(c) in _norm(dbg_visible)]
    if shown2:
        problems.append(
            f"먼저 말할 개념이 2단계 상황·산출물·자료 제목에 나옴 (증상만 써야 함): {', '.join(shown2)}"
        )
    howto_text = "\n".join(
        [out.title, *(r.text for r in out.requirements), *(a.text for a in out.acceptance_public)]
    )
    seed = ctx.incident.feature if ctx.incident is not None else None
    # 그 기능이 다루는 대상 자체(예: DBA 의 트랜잭션)는 구현 방법 지시가 아니다 — 재료의 업무 요구에 나온 말은 뺀다
    own = seed.business_need.lower() if seed is not None else ""
    howto = [w for w in HOWTO_TERMS if _term_rx(w).search(howto_text) and w.lower() not in own]
    if howto:
        problems.append(
            "제목·공개 요구·공개 수용 기준이 구현 방법을 지시함 ("
            + ", ".join(howto)
            + ") — '무엇을'만 쓰고 '어떻게'는 지원자 몫"
        )

    if seed is not None:
        # 사람이 적어 둔 새는 말 — 바꿔 쓴 표현까지 잡으려고 재료마다 둔다
        stage1 = "\n".join([base_visible, extra_visible])
        terms = [*seed.leak_terms, *seed.stage2_cause_terms]
        s1 = [t for t in terms if _term_rx(t).search(stage1)]
        if s1:
            problems.append(
                "숨은 요구를 드러내는 말이 1단계 지원자 화면(제목·상황·공개 요구·수용 기준·추가 요구 답·자료 제목)에 나옴: "
                + ", ".join(dict.fromkeys(s1))
                + _quote(s1, stage1)
            )
        # 운영 조건 — 단서는 자료(원시 값)·정확히 짚은 질문의 구체 답에서만. 공개된 업무 요구에 나온 말은 뺀다
        story = "\n".join(
            [
                out.title,
                out.scenario,
                *(r.text for r in out.requirements),
                *(a.text for a in out.acceptance_public),
            ]
        )
        cond = [
            t
            for t in seed.trigger_terms
            if not _in_text(t, seed.business_need) and _in_text(t, story)
        ]
        if cond:
            problems.append(
                "운영 조건을 드러내는 말이 1단계 제목·상황·공개 요구·공개 수용 기준에 나옴: "
                + ", ".join(dict.fromkeys(cond))
                + " — 상황은 만들 기능만, 운영 조건은 자료(원시 값)와 정확히 짚은 질문의 구체 답에서만"
                + _quote(cond, story)
            )
        # 1단계 자료 제목 (요청 전부터 보이고, v5 단서 자료 답이 제목을 그대로 쓴다) — 재료의 자료 종류 이름(괄호 앞)에
        # 나온 말은 그 자료의 이름이라 괜찮다 (예: DBA "DB 정리 작업 설정"). 괄호 안 설명의 말은 안 된다
        names = "\n".join(re.split(r"[(（]", d, maxsplit=1)[0] for d in seed.discoverable_by)
        allowed = "\n".join([seed.business_need, names])
        art_titles = "\n".join(a.title for a in out.artifacts)
        tcond = [
            t for t in seed.trigger_terms if not _in_text(t, allowed) and _in_text(t, art_titles)
        ]
        if tcond:
            problems.append(
                "운영 조건을 드러내는 말이 1단계 자료 제목에 나옴: "
                + ", ".join(dict.fromkeys(tcond))
                + " — 제목은 '<구성 요소> <자료 종류>'만 (재료의 자료 종류 이름은 괜찮음), 조건은 본문의 원시 값으로"
                + _quote(tcond, art_titles)
            )
        mixed = stage2_kinds_in_stage1(
            [a.title for a in out.artifacts], seed.stage2_evidence, seed.discoverable_by
        )
        if mixed:
            problems.append(
                "1단계 자료가 배포 뒤(2단계) 증거와 같은 종류: "
                + ", ".join(mixed)
                + " — 기능이 아직 없으니 1단계에는 배포 전 자료(설정·지표·기존 코드·통계)만"
            )
        s2 = [t for t in terms if _term_rx(t).search(dbg_visible)]
        if s2:
            problems.append(
                "숨은 요구·원인을 드러내는 말이 2단계 상황·산출물·자료 제목에 나옴 (증상만): "
                + ", ".join(dict.fromkeys(s2))
                + _quote(s2, dbg_visible)
            )

    inc = ctx.incident
    if inc is not None:
        raw = "\n".join([extra_visible, dbg.scenario, *dbg.deliverables])
        hidden = [w for w in inc.hidden_terms if _phrase(w).search(raw)]
        if hidden:
            problems.append(
                f"원 사례의 원인 용어가 공개 요구·추가 요구 답·2단계 상황에 나옴: {', '.join(hidden)}"
            )
        dbg_all = "\n".join([dbg.scenario, *(a.content for a in dbg.artifacts), extra_visible])
        copied = incident_copied(dbg_all, inc)
        if copied:
            problems.append(
                f"2단계·공개 요구가 원 사례 문구·수치를 그대로 씀 ({'; '.join(copied)}) — 새로 만들 것"
            )
        leaked = [
            w
            for w in inc.avoid_terms
            if _phrase(w).search(
                "\n".join(
                    [
                        dbg_all,
                        *(d.summary + " " + d.conditions for d in out.accepted_designs),
                        *(h.text for h in out.acceptance_hidden),
                    ]
                )
            )
        ]
        if leaked:
            problems.append(f"원 사례의 고유어가 나옴 (소재를 바꿔야 함): {', '.join(leaked)}")
    return problems


def repair_feature(out: DraftFeatureOutput, ctx: DraftContext) -> tuple[DraftFeatureOutput, bool]:
    """재요청 후에도 어긋나면 잘못된 id 참조만 걷어낸다 (이슈형 repair + 기능형 참조). 문장은 고치지 않는다."""
    fixed_any, anchors_ok = repair(cast(Any, out), ctx)
    fixed = cast(DraftFeatureOutput, fixed_any)
    traps = {t.trap_id for t in fixed.traps}
    clar = {c.clarification_id for c in fixed.clarifications}
    s1 = {a.artifact_id for a in fixed.artifacts}
    dbg = fixed.debug_stage
    s2 = {a.artifact_id for a in dbg.artifacts}

    def keep1(o: Observable) -> bool:
        if o.kind == "asks_clarification":
            return o.value in clar
        if o.kind == "requests_artifact":
            return o.value in s1
        return True

    def keep2(o: Observable) -> bool:
        if o.kind == "catches_trap":
            return o.value in traps
        if o.kind == "requests_artifact":
            return o.value in s2
        return o.kind != "asks_clarification"

    designs = [
        d.model_copy(update={"trap_ids": [t for t in d.trap_ids if t in traps]})
        for d in fixed.accepted_designs
    ]
    dbg_traps = [t for t in dbg.trap_ids if t in traps] or sorted(traps)[:1]
    upd: dict[str, Any] = {
        "acceptance_hidden": [h for h in fixed.acceptance_hidden if h.trap_id in traps],
        "clarifications": [
            c.model_copy(update={"reveals": [t for t in c.reveals if t in traps]})
            for c in fixed.clarifications
        ],
        "max_clarifications": max(1, min(fixed.max_clarifications, len(fixed.clarifications))),
        "accepted_designs": [d for d in designs if d.trap_ids],
        "observables": [o for o in fixed.observables if keep1(o)],
        "debug_stage": dbg.model_copy(
            update={
                "trap_ids": dbg_traps,
                "observables": [o for o in dbg.observables if keep2(o)],
            }
        ),
    }
    return fixed.model_copy(update=upd), anchors_ok


def shuffle_feature(out: DraftFeatureOutput, seed: str) -> DraftFeatureOutput:
    """1·2단계 자료를 단계 안에서 섞고 이어지는 번호로 다시 매긴다 (1단계 a1…aN, 2단계 aN+1…).
    생성 모델이 핵심 자료를 앞에 두는 경향을 지운다 (spec/draft.shuffle_artifacts 와 같은 이유).
    두 단계 id 가 겹치거나 한 단계 안에서 겹치면 섞지 않는다 — 검사가 잡아 재요청한다."""
    s1, s2 = list(out.artifacts), list(out.debug_stage.artifacts)
    ids = [a.artifact_id for a in [*s1, *s2]]
    if len(set(ids)) != len(ids):
        return out
    rnd = random.Random(seed)
    rnd.shuffle(s1)
    rnd.shuffle(s2)
    remap = {a.artifact_id: f"a{i}" for i, a in enumerate([*s1, *s2], 1)}

    def sub(text: str) -> str:
        return _ART_ID.sub(lambda m: remap.get(m.group(0), m.group(0)), text)

    def obs(items: list[Observable]) -> list[Observable]:
        return [
            o.model_copy(update={"value": remap.get(o.value, o.value)})
            if o.kind == "requests_artifact"
            else o
            for o in items
        ]

    anchors = out.behavioral_anchors
    dbg = out.debug_stage
    return out.model_copy(
        update={
            "artifacts": [a.model_copy(update={"artifact_id": remap[a.artifact_id]}) for a in s1],
            "observables": obs(out.observables),
            "scenario": sub(out.scenario),
            "deliverables": [sub(d) for d in out.deliverables],
            "invariant": sub(out.invariant),
            "traps": [
                t.model_copy(
                    update={"description": sub(t.description), "why_ai_fails": sub(t.why_ai_fails)}
                )
                for t in out.traps
            ],
            "clarifications": [
                c.model_copy(update={"answer": sub(c.answer)}) for c in out.clarifications
            ],
            "behavioral_anchors": anchors.model_copy(
                update={
                    k: getattr(anchors, k).model_copy(
                        update={"behavior": sub(getattr(anchors, k).behavior)}
                    )
                    for k in ("level_1", "level_3", "level_5")
                }
            ),
            "debug_stage": dbg.model_copy(
                update={
                    "artifacts": [
                        a.model_copy(update={"artifact_id": remap[a.artifact_id]}) for a in s2
                    ],
                    "observables": obs(dbg.observables),
                    "scenario": sub(dbg.scenario),
                    "deliverables": [sub(d) for d in dbg.deliverables],
                }
            ),
        }
    )


def apply_feature(task: TaskItem, res: DraftResult) -> TaskItem:
    """기능형 골격 TaskItem 에 서술을 붙인다. 2단계 공개 시점·시간은 결정적으로 붙인다."""
    out = cast(DraftFeatureOutput | None, res.output)
    if out is None:
        return task.model_copy(update={"review_flags": [*task.review_flags, *res.flags]})
    dbg = out.debug_stage
    stage = DebugStage(
        time_limit_min=FEATURE_DEBUG_MIN,
        scenario=dbg.scenario,
        trap_ids=list(dbg.trap_ids),
        artifacts=list(dbg.artifacts),
        max_requests=dbg.max_requests,
        deliverables=list(dbg.deliverables),
        observables=list(dbg.observables),
    )
    updated = task.model_copy(
        update={
            "title": out.title,
            "scenario": out.scenario,
            "deliverables": list(out.deliverables),
            "invariant": out.invariant,
            "variable": list(out.variable),
            "traps": list(out.traps),
            "artifacts": list(out.artifacts),
            "max_requests": out.max_requests,
            "observables": list(out.observables),
            "behavioral_anchors": out.behavioral_anchors if res.anchors_ok else None,
            "requirements": list(out.requirements),
            "acceptance_public": list(out.acceptance_public),
            "acceptance_hidden": list(out.acceptance_hidden),
            "clarifications": list(out.clarifications),
            "max_clarifications": out.max_clarifications,
            "accepted_designs": list(out.accepted_designs),
            "debug_stage": stage,
            "provenance_id": res.provenance_id,
            "source_incident": res.incident_id,
            "review_flags": [
                *(
                    FLAG_PUBLIC_INCIDENT if f == FLAG_NO_INCIDENT and res.incident_id else f
                    for f in task.review_flags
                ),
                *res.flags,
            ],
        }
    )
    if res.seed is not None:
        flags = [*updated.review_flags]
        if not res.seed.reviewed:
            flags.append(FLAG_SEED_UNREVIEWED)
        if res.seed.starter is None:
            flags.append(FLAG_NO_STARTER)
        only = hint_only(out)
        if only:
            flags.append(FLAG_HINT_ONLY + ", ".join(only))
        updated = updated.model_copy(update={"review_flags": flags})
    # model_copy 는 검사를 건너뛴다 — 저장·승인 화면에 올리기 전에 TaskItem 규칙(유형·필드 범위)을 다시 본다
    return TaskItem.model_validate(updated.model_dump())


def uses_seed(prompts: Any) -> bool:
    """CURRENT(또는 고정한 버전) 템플릿이 기능형 재료(v2)를 입력으로 받는지."""
    return "feature_seed" in prompts.get(TASK).input_model.model_fields


async def draft_feature_task(
    ctx: DraftContext,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    reviewer_note: str | None = None,
) -> DraftResult:
    """생성 → 검증 → 1회 재요청 → 고쳐 쓰기 (이슈형과 같은 루프, 기능형 검사).

    v2(입력에 feature_seed): 공개 사례의 기능형 재료가 있어야 한다. LLM 출력(겉모습)에 재료의 숨은 요구·허용 설계를
    채운 뒤 섞고 검사한다. v1: 공개 사례 원문(incident)을 넣고 LLM 이 숨은 요구까지 쓴다.
    """
    if not uses_seed(prompts):
        return await draft_task(
            ctx,
            llm=llm,
            prompts=prompts,
            reviewer_note=reviewer_note,
            task=TASK,
            extra_input=extra_input(),
            checker=cast(Any, check_feature),
            repairer=cast(Any, repair_feature),
            shuffler=cast(Any, shuffle_feature),
        )
    inc = ctx.incident
    if inc is None or inc.feature is None:
        res = DraftResult()
        res.flags.append(FLAG_NO_SEED)
        return res
    seed = inc.feature

    def fill_then_shuffle(out: Any, key: str) -> DraftFeatureOutput:
        if isinstance(out, DraftFeatureOutputV5):  # v4 의 하위 모델이라 먼저
            full = fill_from_seed_v5(out, seed)
        elif isinstance(out, DraftFeatureOutputV4):
            full = fill_from_seed_v4(out, seed)
        elif isinstance(out, DraftFeatureOutputV2):  # v3 (두 층 답) 은 v2 의 하위 모델
            full = fill_from_seed(out, seed)
        else:
            full = out
        return prune_match_terms(shuffle_feature(full, key))

    res = await draft_task(
        ctx,
        llm=llm,
        prompts=prompts,
        reviewer_note=reviewer_note,
        task=TASK,
        extra_input={**extra_input(), "feature_seed": seed_block(inc)},
        checker=cast(Any, check_feature),
        repairer=cast(Any, repair_feature),
        shuffler=fill_then_shuffle,
    )
    res.incident_id = inc.incident_id
    res.seed = seed
    return res
