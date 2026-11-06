"""공통 과제 서술 (draft_task LLM) — 결정적 골격 위에 제목·상황·산출물·함정·행동 기준을 채운다.

입력은 공고·온톨로지 텍스트만 (불변식 3). 골격 값(역량·수준·기술 환경·난이도·시간)은 LLM 이 바꾸지 않는다.

검증 [확정: 2026-11-02]
- 행동 기준(1·3·5)이 인용한 수행준거 id 는 입력 목록 안에서만. 고른 역량마다 적어도 하나는 인용.
- observables 의 catches_trap 값은 traps 의 trap_id 중 하나.
- 과제가 요구하는 언어(tech_used 중 온톨로지 언어)·본문에 나오는 언어는 기술 환경(공고 언어) 안에서만.
어긋나면 사유를 붙여 한 번 다시 요청. 그래도 어긋나면 문제가 적은 쪽을 고쳐 쓰고(잘못된 id 제거)
review_flags 에 남긴다. LLM 이 실패하면 골격만 두고 review_flags 에 남긴다.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from task_spec.llm import LLMClient
from task_spec.llm.client import EmptyResponse, SchemaViolation, TransientLLMError
from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import (
    DraftTaskInput,
    DraftTaskOutput,
    NcsCriterion,
    NcsKsa,
    NodeError,
    Provenance,
    TargetCompetency,
    TaskItem,
    TechEnv,
)
from task_spec.schemas.domain import Anchor, BehavioralAnchors
from task_spec.schemas.incident import FeatureSeed, PublicIncident
from task_spec.spec.common import FLAG_NO_INCIDENT, FLAG_PUBLIC_INCIDENT
from task_spec.spec.incidents import incident_block

logger = logging.getLogger(__name__)
TASK = "draft_task"
NONE = "(없음)"
DIFFICULTY_KO = {
    "high": "high — 공고가 '수행' 수준을 요구. 실무자가 혼자 원인을 찾고 설계까지 하는 수준",
    "medium": "medium — 공고가 '활용' 수준을 요구. 주어진 구조 안에서 도구·방법을 골라 적용하는 수준",
    "low": "low — 공고가 '이해' 수준 이하. 개념을 설명하고 간단히 적용하는 수준",
}
FLAG_DRAFT_FAILED = "과제 서술 생성 실패 — 결정적 골격만 (제목·상황·행동 기준 없음)"
FLAG_DRAFT_INVALID = "과제 서술 자동 검증 실패 (재요청 후에도) — 고쳐 쓴 항목 확인 필요: "
FLAG_NOTE_DROPPED = (
    "승인자 수정 요청에 이름·학교·회사·연락처 같은 개인정보 후보가 있어 LLM 에 보내지 않음 — "
    "개인정보 없이 다시 적어 주세요"
)
FLAG_NOTE_TOO_LONG = "승인자 수정 요청이 너무 길어(1000자 초과) LLM 에 보내지 않음"
MAX_NOTE = 1000


@dataclass
class DraftContext:
    sub_label: str
    industry_label: str
    targets: list[TargetCompetency]
    criteria: list[NcsCriterion]
    ksa: list[NcsKsa]
    env: TechEnv
    difficulty: str
    time_limit_min: int
    ontology_version: str
    language_labels: list[str] = field(default_factory=list)  # 온톨로지의 언어 전체 (본문 검사용)
    incident: PublicIncident | None = None  # 공고 직군의 공개 사례 (draft_task v2 부터 입력)

    @property
    def criterion_ids(self) -> set[str]:
        return {c.criterion_id for c in self.criteria}


@dataclass
class DraftResult:
    output: Any = None  # DraftTaskOutput (이슈형·슬롯) 또는 DraftFeatureOutput (기능형)
    anchors_ok: bool = True  # False = 재요청 후에도 근거 id 가 비어 행동 기준을 비운다
    provenance_id: str | None = None  # 채택한 답의 provenance
    incident_id: str | None = None  # 프롬프트에 실제로 넣은 공개 사례
    first_problems: list[str] = field(default_factory=list)  # 첫 답의 검증 실패 사유 (평가용)
    fail_reasons: list[str] = field(
        default_factory=list
    )  # LLM 실패 사유 (필드 위치·오류 종류만, 원문 없음)
    provenance: list[Provenance] = field(default_factory=list)
    errors: list[NodeError] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    seed: FeatureSeed | None = None  # 기능형 v2 가 쓴 재료 (검토·시작 코드 표시용)


def build_input(
    ctx: DraftContext,
    note: str | None = None,
    model: type[DraftTaskInput] = DraftTaskInput,
    extra_input: dict[str, Any] | None = None,
) -> DraftTaskInput:
    """템플릿의 입력 스키마(v1 DraftTaskInput / v2 DraftTaskInputV2 / 슬롯 DraftSlotInput)에 맞춰 만든다."""
    comps = "\n".join(
        f"- {t.competency_id} {t.label} (공고 요구 수준: {t.level or '미상'})" for t in ctx.targets
    )
    crit = "\n".join(
        f"[{c.criterion_id}] {c.unit_name} > {c.element_name}: {c.text}" for c in ctx.criteria
    )
    ksa = "\n".join(f"- {k.kind}: {k.text}" for k in ctx.ksa) or NONE
    env = (
        ", ".join(ctx.env.languages + ctx.env.frameworks)
        or "(공고에 언어 없음 — 특정 언어에 묶이지 않게)"
    )
    extra = {"incident": incident_block(ctx.incident)} if "incident" in model.model_fields else {}
    extra |= {k: v for k, v in (extra_input or {}).items() if k in model.model_fields}
    return model(
        **extra,
        sub=ctx.sub_label,
        industry=ctx.industry_label,
        difficulty=DIFFICULTY_KO.get(ctx.difficulty, ctx.difficulty),
        time_limit_min=ctx.time_limit_min,
        tech_env=env,
        competencies=comps,
        ncs_criteria=crit,
        ksa=ksa,
        revision_note=note or NONE,
    )


def _lang_patterns(labels: list[str]) -> dict[str, re.Pattern[str]]:
    """언어 이름 → 본문 검색 정규식. 한 글자(C·R)는 로그 식별자와 겹쳐 '… 언어' 일 때만 잡는다."""
    out: dict[str, re.Pattern[str]] = {}
    for label in labels:
        for part in re.sub(r"\(.*?\)", "", label).split("/"):
            part = part.strip()
            if not part:
                continue
            if len(part) == 1:
                out[part] = re.compile(rf"(?<![A-Za-z0-9+#&_-]){re.escape(part)}\s?언어")
            else:
                out[part] = re.compile(
                    rf"(?<![A-Za-z0-9+#&]){re.escape(part)}(?![A-Za-z0-9+#&])",
                    0 if len(part) <= 2 else re.IGNORECASE,
                )
    return out


def _names(labels: list[str]) -> set[str]:
    """'Shell/Bash' → {'shell/bash', 'shell', 'bash'} (소문자)."""
    out: set[str] = set()
    for x in labels:
        out.add(x.strip().lower())
        out |= {p.strip().lower() for p in re.sub(r"\(.*?\)", "", x).split("/") if p.strip()}
    return out


def _allowed_tech(env: TechEnv) -> set[str]:
    return _names(env.languages + env.frameworks)


def _phrase(term: str) -> re.Pattern[str]:
    """'SYN cookie' → 단어 사이 공백 1개 이상, 영문 단어 경계 (TcpExtSyncookiesSent 는 안 잡음)."""
    body = r"\s+".join(re.escape(w) for w in term.split())
    return re.compile(rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])", re.IGNORECASE)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def shuffle_artifacts(out: DraftTaskOutput, seed: str) -> DraftTaskOutput:
    """자료 순서를 섞고 a1… 로 다시 번호를 매긴다 — 생성 모델이 핵심 자료를 앞에 두는 경향이 정답을 흘린다
    (2026-11-03 AI 단독 풀이 테스트). requests_artifact 관찰 행동의 id 도 함께 바꾼다. 같은 seed → 같은 순서."""
    arts = list(getattr(out, "artifacts", []) or [])
    if not arts:
        return out
    order = list(arts)
    random.Random(seed).shuffle(order)
    remap = {a.artifact_id: f"a{i}" for i, a in enumerate(order, 1)}
    new_arts = [a.model_copy(update={"artifact_id": remap[a.artifact_id]}) for a in order]
    obs = [
        o.model_copy(update={"value": remap.get(o.value, o.value)})
        if o.kind == "requests_artifact"
        else o
        for o in out.observables
    ]

    def ids(
        text: str,
    ) -> str:  # 본문에 적힌 자료 번호도 한 번에 바꾼다 (a1→a7, a7→a2 가 서로 덮어쓰지 않게)
        return _ART_ID.sub(lambda m: remap.get(m.group(0), m.group(0)), text)

    anchors = out.behavioral_anchors
    new_anchors = anchors.model_copy(
        update={
            k: getattr(anchors, k).model_copy(
                update={"behavior": ids(getattr(anchors, k).behavior)}
            )
            for k in ("level_1", "level_3", "level_5")
        }
    )
    return out.model_copy(
        update={
            "artifacts": new_arts,
            "observables": obs,
            "scenario": ids(out.scenario),
            "deliverables": [ids(d) for d in out.deliverables],
            "invariant": ids(out.invariant),
            "traps": [
                t.model_copy(
                    update={"description": ids(t.description), "why_ai_fails": ids(t.why_ai_fails)}
                )
                for t in out.traps
            ],
            "behavioral_anchors": new_anchors,
        }
    )


_ART_ID = re.compile(r"(?<![A-Za-z0-9_])a\d{1,2}(?![A-Za-z0-9_])")
# 자료 제목에 결론을 암시하는 말 / 핵심 자료 본문의 판정 문구 (2026-11-03: 대조표가 정답을 그대로 보여 nano 도 풀었음)
_TITLE_TELLS = ("대조", "비교", "차이", "불일치", "vs", "누락", "원인", "요약", "분석 결과", "결론")
_CONTENT_TELLS = (
    "불일치",
    "누락",
    "원인",
    "mismatch",
    "missing",
    "root cause",
    "→ 문제",
    "O / X",
    "✓",
    "✗",
)


# 핵심·보조 자료 안의 판정·요약 줄 (2026-11-03 웹 예시: 핵심 자료 끝 'Pattern: workers that hold 1st and wait for 2nd fail').
# 줄머리 이름표 + 콜론 뒤 네 단어 이상 문장만 — YAML 'pattern: "^(GET|…"' 같은 설정 키는 따옴표·괄호로 시작해 빠진다.
_CONCLUSION_LINE = re.compile(
    r"(?im)^[ \t#/*>|-]*(?:pattern|conclusion|summary|findings?|insight|analysis|diagnosis|"
    r"root cause|explanation|takeaway|observation|결론|요약|분석|패턴|진단|해석|시사점|관찰)"
    r"[ \t]*[:：][ \t]*(?=[^\s'\"`\[\](){}^0-9])\S+(?:[ \t]+\S+){3,}"
)
# 원 사례 문구·수치 복사 (2026-11-03: sw.backend 공통 과제 5판 모두 원 블로그의 오류 문구·풀 지표를 그대로 옮김 → 검색하면 원 사례)
_KV = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\s*[=:]\s*-?\d+(?:\.\d+)?")
_BIG_NUM = re.compile(r"(?<![\d.])\d{3,}(?:\.\d+)?\s*(?:ms|s|초|분|%|MB|GB|KB)?")
_QUOTED = re.compile(r"['\"‘“`]([^'\"’”`]{10,})['\"’”`]")
INCIDENT_SPAN = 20  # 원 사례 증상·원인과 이만큼(공백 제외) 같은 글이면 복사
INCIDENT_NUMS = (
    2  # 원문 표지(증상의 키=값·세 자리 이상 수 + source_literals) 중 이만큼 함께 나오면 복사
)


def incident_copied(text: str, inc: PublicIncident) -> list[str]:
    """과제(자료 본문 포함)가 원 사례의 문구·수치를 그대로 쓴 곳. 검색 한 번이면 원 사례·정답이 나온다."""
    found: list[str] = []
    flat = _norm(text)
    # 표지 하나(라이브러리 기본 오류 문구, idle=0 같은 흔한 값)는 우연히도 나온다 — 여럿이 함께 나와야 원 사례로 특정된다
    marks = {_norm(m.group(0)) for rx in (_KV, _BIG_NUM) for m in rx.finditer(inc.symptom)}
    marks |= {_norm(x) for x in inc.source_literals}
    hits = sorted(m for m in marks if m in flat)
    if len(hits) >= INCIDENT_NUMS:
        found.append("원 사례 문구·수치 " + ", ".join(hits))
    src = "\n".join([inc.symptom, inc.root_cause, inc.key_insight])
    ns = _norm(src)
    quotes = [q for q in _QUOTED.findall(src) if _norm(q) in flat]
    if quotes:
        found.append("원 사례 문구 " + ", ".join(f"'{q}'" for q in quotes))
    elif any(
        ns[i : i + INCIDENT_SPAN] in flat for i in range(0, max(0, len(ns) - INCIDENT_SPAN + 1))
    ):
        found.append(f"원 사례 문장과 {INCIDENT_SPAN}자 이상 같음")
    return found


def _check_artifacts(out: DraftTaskOutput, arts: list[Any]) -> list[str]:
    """자료 점진 공개(v3): id 고유, 핵심 1개 이상·방해 3개 이상, 요청 한도 ≤ max(핵심+1, 자료/3), 상황에 자료 본문을 미리 싣지 않음."""
    return check_artifact_list(
        arts, scenario=out.scenario, limit=getattr(out, "max_requests", None)
    )


def check_artifact_list(
    arts: list[Any],
    *,
    scenario: str,
    limit: int | None,
    min_distractor: int = 3,
    label: str = "",
) -> list[str]:
    """자료 목록 하나의 규칙 (공통 과제 v3·슬롯·기능형 1·2단계가 같이 쓴다). label 은 사유 앞에 붙는 단계 이름."""
    problems: list[str] = []
    ids = [a.artifact_id for a in arts]
    if len(set(ids)) != len(ids):
        problems.append(f"{label}artifact_id 가 겹침")
    roles = [a.role for a in arts]
    if roles.count("key") < 1 or roles.count("distractor") < min_distractor:
        problems.append(
            f"{label}자료는 핵심(key) 1개 이상, 방해(distractor) {min_distractor}개 이상이어야 함"
        )
    if limit is not None and limit > max(roles.count("key") + 1, len(arts) // 3):
        problems.append(
            f"{label}max_requests 는 핵심 자료 수 + 1 또는 자료 수의 3분의 1 이하여야 함 (골라서 요청하게)"
        )
    if limit is not None and limit < roles.count("key"):
        problems.append(
            f"{label}max_requests 가 핵심 자료 수보다 작음 (정답에 필요한 자료를 다 볼 수 없음)"
        )
    for a in arts:
        if (
            a.role == "key"
            and len(_norm(a.content)) >= 40
            and _norm(a.content)[:40] in _norm(scenario)
        ):
            problems.append(
                f"핵심 자료 {a.artifact_id} 본문이 상황에 미리 실림 (요청해야만 보이게)"
            )
        tells = [w for w in _TITLE_TELLS if w.lower() in a.title.lower()]
        if tells:
            problems.append(
                f"자료 {a.artifact_id} 제목이 결론을 암시함 ({', '.join(tells)}) — 자료 종류만 쓸 것"
            )
        if a.role == "key":
            said = [w for w in _CONTENT_TELLS if w.lower() in a.content.lower()]
            if said:
                problems.append(
                    f"핵심 자료 {a.artifact_id} 가 결론을 직접 말함 ({', '.join(said)}) — 원시 로그·지표·설정만"
                )
        if a.role in ("key", "support"):
            line = _CONCLUSION_LINE.search(a.content)
            if line:
                problems.append(
                    f"자료 {a.artifact_id} 에 판정·요약 줄이 있음 ('{line.group(0).strip()[:60]}') — "
                    "해석은 지원자 몫, 원시 로그·지표·설정만"
                )
    return problems


def check(out: DraftTaskOutput, ctx: DraftContext, *, min_distractor: int = 3) -> list[str]:
    """어긋난 점 목록 (비면 통과). 문장은 그대로 재요청 사유가 된다 — 온톨로지·공고 값만 담는다.

    min_distractor: 자료 중 방해 자료 최소 수 (이슈형·슬롯 3, 기능형 1단계 2 — 자료가 5~8개라)."""
    problems: list[str] = []
    arts = list(getattr(out, "artifacts", []) or [])
    allowed = ctx.criterion_ids
    anchors = out.behavioral_anchors
    cited: set[str] = set()
    for name in ("level_1", "level_3", "level_5"):
        a: Anchor = getattr(anchors, name)
        bad = [i for i in a.criterion_ids if i not in allowed]
        if bad:
            problems.append(f"{name} 가 목록에 없는 수행준거 id 를 인용: {', '.join(bad)}")
        cited |= set(a.criterion_ids) & allowed
    for t in ctx.targets:
        if t.ncs_criterion_ids and not cited & set(t.ncs_criterion_ids):
            problems.append(
                f"행동 기준이 역량 {t.competency_id} 의 수행준거를 하나도 인용하지 않음"
            )
    trap_ids = [t.trap_id for t in out.traps]
    if len(set(trap_ids)) != len(trap_ids):
        problems.append("trap_id 가 겹침")
    if arts:
        problems += check_artifact_list(
            arts,
            scenario=out.scenario,
            limit=getattr(out, "max_requests", None),
            min_distractor=min_distractor,
        )
    for o in out.observables:
        if o.kind == "catches_trap" and o.value not in trap_ids:
            problems.append(f"catches_trap 값 {o.value} 가 traps 의 trap_id 가 아님")
        if o.kind == "requests_artifact" and o.value not in {a.artifact_id for a in arts}:
            problems.append(f"requests_artifact 값 {o.value} 가 artifacts 의 artifact_id 가 아님")
    tech_ok = _allowed_tech(ctx.env)
    langs = _names(ctx.language_labels)
    # 언어만 검사한다 (도구·개념 이름은 온톨로지 언어 목록에 없으므로 통과)
    extra = [x for x in out.tech_used if x.strip().lower() in langs - tech_ok]
    if extra:
        problems.append(f"기술 환경 밖 언어 요구: {', '.join(extra)}")
    # 지원자에게 처음 보이는 것: 제목·상황·제출물 + 자료 목록(이름만)
    text = "\n".join([out.title, out.scenario, *out.deliverables, *(a.title for a in arts)])
    env_langs = {p.lower() for p in _lang_patterns(ctx.env.languages)}
    mentioned = [
        name
        for name, rx in _lang_patterns(ctx.language_labels).items()
        if name.lower() not in env_langs and name.lower() not in tech_ok and rx.search(text)
    ]
    if mentioned:
        problems.append(f"본문에 기술 환경 밖 언어가 나옴: {', '.join(sorted(mentioned))}")
    # 지원자에게 보이는 곳(제목·상황·제출물)에 핵심 원인 개념이 나오면 원인 찾기 과제가 성립하지 않는다
    visible = _norm(text)
    revealed = [
        o.value
        for o in out.observables
        if o.kind == "first_mention_of" and len(_norm(o.value)) >= 2 and _norm(o.value) in visible
    ]
    if revealed:
        problems.append(
            f"핵심 원인 개념이 제목·상황·제출물에 드러남 (증상만 써야 함): {', '.join(revealed)}"
        )
    if ctx.incident is not None and ctx.incident.hidden_terms:
        # 문장 속 표현만 잡는다 — 띄어쓰기를 지운 비교는 카운터·옵션 이름(도구 출력 = 단서)까지 잡는다
        raw = "\n".join([out.title, out.scenario, *out.deliverables])
        shown = [w for w in ctx.incident.hidden_terms if _phrase(w).search(raw)]
        if shown:
            problems.append(
                f"원 사례의 원인 용어가 제목·상황·제출물에 드러남 (증상만 써야 함): {', '.join(shown)}"
            )
    if ctx.incident is not None:
        copied = incident_copied("\n".join([text, *(a.content for a in arts)]), ctx.incident)
        if copied:
            problems.append(
                f"원 사례 문구·수치를 그대로 씀 ({'; '.join(copied)}) — "
                "검색하면 원 사례가 나옴, 오류 문구·이름·수치를 새로 만들 것 (비율·구조는 유지)"
            )
    if ctx.incident is not None and ctx.incident.avoid_terms:
        # 자료 본문까지 (2026-11-03: sw.backend 3판이 원 사례 테이블 이름을 로그에 씀). 단어 경계로만 —
        # 부분 문자열이면 'ExP' 가 experiment·expected 에 걸린다
        full = "\n".join(
            [
                text,
                out.invariant,
                *(f"{t.description} {t.why_ai_fails}" for t in out.traps),
                *(a.content for a in arts),
            ]
        )
        leaked = [w for w in ctx.incident.avoid_terms if _phrase(w).search(full)]
        if leaked:
            problems.append(f"원 사례의 고유어가 나옴 (소재를 바꿔야 함): {', '.join(leaked)}")
    return problems


def repair(out: DraftTaskOutput, ctx: DraftContext) -> tuple[DraftTaskOutput, bool]:
    """재요청 후에도 어긋나면 잘못된 id·값만 걷어낸다. 행동 기준 근거가 비면 (출력, False)."""
    allowed = ctx.criterion_ids
    trap_ids = {t.trap_id for t in out.traps}
    tech_ok = _allowed_tech(ctx.env)
    upd: dict[str, object] = {
        "observables": [
            o for o in out.observables if o.kind != "catches_trap" or o.value in trap_ids
        ],
        "tech_used": [
            x
            for x in out.tech_used
            if x.strip().lower() not in _names(ctx.language_labels) - tech_ok
        ],
    }
    levels: dict[str, Anchor] = {}
    for name in ("level_1", "level_3", "level_5"):
        a: Anchor = getattr(out.behavioral_anchors, name)
        ids = [i for i in a.criterion_ids if i in allowed]
        if ids:
            levels[name] = Anchor(behavior=a.behavior, criterion_ids=ids)
    anchors_ok = len(levels) == 3
    if anchors_ok:
        upd["behavioral_anchors"] = BehavioralAnchors(**levels)
    return out.model_copy(update=upd), anchors_ok


def apply(task: TaskItem, res: DraftResult) -> TaskItem:
    """골격 TaskItem 에 서술을 붙인다. 서술이 없으면 flags 만 붙인다."""
    out = res.output
    if out is None:
        return task.model_copy(update={"review_flags": [*task.review_flags, *res.flags]})
    return task.model_copy(
        update={
            "title": out.title,
            "scenario": out.scenario,
            "deliverables": list(out.deliverables),
            "invariant": out.invariant,
            "variable": list(out.variable),
            "traps": list(out.traps),
            "artifacts": list(getattr(out, "artifacts", []) or []),
            "max_requests": getattr(out, "max_requests", None),
            "observables": list(out.observables),
            "behavioral_anchors": out.behavioral_anchors if res.anchors_ok else None,
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


@lru_cache(maxsize=1)
def _detectors() -> Detectors:
    return Detectors(load_dictionaries(), use_kiwi=True)


def safe_reviewer_note(comment: str | None) -> tuple[str | None, list[str]]:
    """승인자 수정 요청은 지원자 문서가 아니지만 지원자를 보고 쓰므로, 가명화와 같은 로컬 탐지기
    (이름·학교·회사·연락처·주소 등)로 검사해 하나라도 걸리면 보내지 않는다 (privacy-auditor W1)."""
    if not comment or not comment.strip():
        return None, []
    text = comment.strip()
    if len(text) > MAX_NOTE:
        return None, [FLAG_NOTE_TOO_LONG]
    if _detectors().detect(text):
        return None, [FLAG_NOTE_DROPPED]
    return text, []


async def draft_task(
    ctx: DraftContext,
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    reviewer_note: str | None = None,
    task: str = TASK,
    extra_input: dict[str, Any] | None = None,
    extra_check: Callable[[DraftTaskOutput], list[str]] | None = None,
    checker: Callable[[Any, DraftContext], list[str]] = check,
    repairer: Callable[[Any, DraftContext], tuple[Any, bool]] = repair,
    shuffler: Callable[[Any, str], Any] = shuffle_artifacts,
) -> DraftResult:
    """생성 → 검증 → 1회 재요청 → 고쳐 쓰기. 공통 과제(draft_task)·확장 슬롯(draft_slot)·기능형(draft_feature_task,
    checker/repairer/shuffler 를 spec/feature.py 것으로)이 같이 쓴다."""
    template = prompts.get(task)
    res = DraftResult()
    if "incident" in template.input_model.model_fields and ctx.incident is not None:
        res.incident_id = ctx.incident.incident_id
    note = f"승인자 수정 요청: {reviewer_note}" if reviewer_note else None
    best: tuple[DraftTaskOutput, list[str], str] | None = None
    for attempt in range(2):
        inp = build_input(ctx, note, template.input_model, extra_input)  # type: ignore[arg-type]
        prompt = assemble(template, inp, ontology_version=ctx.ontology_version)
        try:
            r = await llm.call(prompt, template.output_model)  # type: ignore[arg-type]
        except (SchemaViolation, EmptyResponse, TransientLLMError) as exc:
            res.errors.append(
                NodeError(node=task, code=type(exc).__name__, message="과제 서술 생성 실패")
            )
            res.fail_reasons.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            continue
        res.provenance.append(r.provenance)
        parsed = shuffler(r.parsed, f"{ctx.sub_label}|{ctx.industry_label}|{attempt}")
        r = r.model_copy(update={"parsed": parsed})
        problems = checker(r.parsed, ctx) + (extra_check(r.parsed) if extra_check else [])
        logger.info(
            "%s attempt=%d problems=%d latency_ms=%d",
            task,
            attempt + 1,
            len(problems),
            r.provenance.latency_ms,
        )
        if best is None or len(problems) < len(best[1]):
            best = (r.parsed, problems, r.provenance.provenance_id)
        if not problems:
            break
        if attempt == 0:
            res.first_problems = list(problems)
            res.errors.append(
                NodeError(
                    node=task,
                    code="draft_invalid",
                    message=f"자동 검증 실패 {len(problems)}건 — 재요청",
                )
            )
            lines = "\n".join(f"- {p}" for p in problems)
            note = "\n".join(
                x for x in (note, f"이전 답의 문제 (고쳐서 다시 쓰세요):\n{lines}") if x
            )
    if best is None:
        res.flags.append(FLAG_DRAFT_FAILED)
        return res
    out, problems, res.provenance_id = best
    if problems:
        out, res.anchors_ok = repairer(out, ctx)
        res.flags.append(FLAG_DRAFT_INVALID + "; ".join(problems))
    res.output = out
    return res
