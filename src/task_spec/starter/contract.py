"""공개 계약 `TaskContract` · `HiddenPlan` 결정적 검사 (docs/design/spec-to-code.md §3.3) — LLM·네트워크 없음.

결과는 둘로 나눈다.
- `problems`: 계약·HiddenPlan 을 다시 만들면 고칠 수 있는 것 → `plan_contract` 재요청 사유 (문장 그대로 붙인다).
- `spec_gaps`: 승인된 명세에 없는 것 → 재요청으로 못 고친다 (명세는 이미 승인됐고 `plan_contract` 는 명세를 바꾸지 않음).
  빌드를 `failed` 로 두고 승인자가 명세 수정 요청(새 판)을 고른다.

검사
1. 양방향 대응 — 모든 r* 가 covers 에, 모든 p* 에 공개 테스트, 모든 h* 에 숨은 케이스. 거꾸로 계약의 모든 엔드포인트·엔티티가
   covers 에 연결되고, 모든 필드가 명세의 r*·p* 를 가리킨다 (요구에 없는 필드는 낱말 목록에 안 걸려도 구조로 해법을 흘린다).
2. 템플릿 이름 규칙·허용 인터페이스.
3. 숨은 요구·운영 조건 핵심어(재료 leak_terms·trigger_terms·stage2_cause_terms·먼저 말할 개념)가 계약에 없음 —
   기능형 서술 검사와 같은 낱말 기준(`spec.feature.find_terms`). 식별자는 낱말로 풀어서 본다 (connectionPoolSize → connection pool size).
4. HiddenPlan 운영 수치 = 명세에 보이는 수치 — 템플릿 `harness_params` 의 표기 패턴으로 1단계 자료 본문·추가 요구 구체 답에서
   값을 뽑아 대조하고, 패턴이 못 읽으면 계약이 낸 근거 인용과 그 안의 값 글자(`evidence_value`)로 확인한다 (`value_check`).
   자유 글의 숫자를 그대로 찾지 않는다 ("8" 은 아무 데나 나온다) — 인용 문장에서도 어느 수가 값인지 추측하지 않는다.
5. HiddenPlan 키는 템플릿 조절값 안, 케이스의 의도한 실패 사유는 템플릿 `failure_reasons` 코드 안.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from task_spec.schemas.build import (
    AppSetting,
    ContractField,
    Endpoint,
    Entity,
    HarnessValue,
    HiddenPlan,
    Screen,
    TaskContract,
    UiElement,
)
from task_spec.schemas.domain import HiddenFault, TaskItem
from task_spec.schemas.incident import FeatureSeed, HarnessRule, MaterialInterface
from task_spec.schemas.template import HarnessParam, TemplateManifest
from task_spec.schemas.trap_frame import ForbiddenStorage
from task_spec.spec.feature import ORDINAL_FAULT, find_terms
from task_spec.starter.auth_rule import auth_flags, auth_problems, unreachable_targets
from task_spec.starter.feasibility import feasibility_problems
from task_spec.starter.field_types import non_null
from task_spec.starter.field_types import parse as parse_field
from task_spec.starter.forbidden_storage import forbidden_for
from task_spec.starter.header_values import header_problems
from task_spec.starter.hygiene import hygiene_hits
from task_spec.starter.interface import interface_problems, without_fixed
from task_spec.starter.seed_rows import (
    case_rows_problems,
    row_target_problems,
    row_use_problems,
    seed_problems,
)
from task_spec.starter.state_rule import legacy_rule, state_problems
from task_spec.starter.templates import glob_match, supports_settings


@dataclass(frozen=True)
class SpecView:
    """승인된 기능형 명세에서 계약 검사가 보는 부분."""

    req_ids: tuple[str, ...]
    public_ac_ids: tuple[str, ...]
    hidden_acs: dict[str, str]  # h* → trap_id
    trap_ids: tuple[str, ...]
    # 지원자가 운영 수치를 발견하는 곳 — 1단계 자료 본문 + 추가 요구 구체 답 (상황 글에는 운영 조건을 쓰지 않는다)
    visible_texts: tuple[str, ...]
    concepts: tuple[
        str, ...
    ] = ()  # 먼저 말할 개념 (1·2단계 first_mention_of) — 계약에 나오면 안 됨
    # 지원자에게 처음부터 보이는 명세 글 — 제목·상황·공개 요구·공개 수용 기준 (여기 나온 API 는 계약에 있어야 함)
    public_texts: tuple[str, ...] = ()
    # 숨김 수용 기준 ac_id → 승인된 실패 지정 (HiddenAcceptance.faults) — 숨은 케이스가 같은 값으로 옮겼는지 대조
    hidden_faults: dict[str, tuple[HiddenFault, ...]] = field(default_factory=dict)
    req_texts: dict[str, str] = field(
        default_factory=dict
    )  # r* → 공식 요구 문장 (새로 추가하는 API 인지 — _added_marked_existing)
    # 1단계 자료 제목 — 지원자 README '진행 방식' 의 자료 목록 줄 (render.py). README 검사가 명세 검사와 같은 허용으로 본다
    artifact_titles: tuple[str, ...] = ()

    @classmethod
    def from_task(cls, task: TaskItem) -> SpecView:
        obs = [*task.observables, *(task.debug_stage.observables if task.debug_stage else [])]
        return cls(
            req_ids=tuple(r.req_id for r in task.requirements),
            public_ac_ids=tuple(a.ac_id for a in task.acceptance_public),
            hidden_acs={h.ac_id: h.trap_id for h in task.acceptance_hidden},
            trap_ids=tuple(t.trap_id for t in task.traps),
            visible_texts=(
                *(a.content for a in task.artifacts),
                *(c.answer for c in task.clarifications),
            ),
            concepts=tuple(dict.fromkeys(o.value for o in obs if o.kind == "first_mention_of")),
            public_texts=(
                task.title or "",
                task.scenario or "",
                *(r.text for r in task.requirements),
                *(a.text for a in task.acceptance_public),
            ),
            hidden_faults={h.ac_id: tuple(h.faults) for h in task.acceptance_hidden if h.faults},
            req_texts={r.req_id: r.text for r in task.requirements},
            artifact_titles=tuple(a.title for a in task.artifacts),
        )


@dataclass(frozen=True)
class ContractCheck:
    problems: list[str] = field(default_factory=list)  # 재요청으로 고칠 수 있음
    spec_gaps: list[str] = field(default_factory=list)  # 명세 수정이 필요함 — 재요청하지 않음
    review_flags: list[str] = field(
        default_factory=list
    )  # 통과지만 승인자 확인 (운영 값 후보 여럿 등)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.spec_gaps


# ── 운영 수치 ──────────────────────────────────────────────


def _number(raw: str) -> float | None:
    s = re.sub(r"[\s,_]", "", raw)
    try:
        return float(s)
    except ValueError:
        return None


_NUMERIC = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def _cells(line: str) -> list[str]:
    """표 한 줄 → 칸. `|` 가 있으면 그것으로, 없으면 공백으로 나눈다."""
    if "|" in line:
        return [c.strip() for c in line.strip().strip("|").split("|")]
    return line.split()


def _column_values(text: str, columns: list[re.Pattern[str]]) -> list[str]:
    """머리 줄에 열 이름이 맞는 표에서 그 열의 숫자 값을 뽑는다 (예: 요청 지표 표의 inflight_requests 열)."""
    found: list[str] = []
    lines = text.splitlines()
    for i, head in enumerate(lines):
        cells = _cells(head)
        idx = next((j for j, c in enumerate(cells) if any(rx.fullmatch(c) for rx in columns)), None)
        if idx is None:
            continue
        for row in lines[i + 1 :]:
            if not row.strip():
                break
            vals = _cells(row)
            if all(set(v) <= set("-: ") for v in vals):  # 마크다운 구분 줄
                continue
            if len(vals) <= idx:
                break
            if _NUMERIC.match(vals[idx]):
                found.append(vals[idx])
    return found


def extract_values(param: HarnessParam, texts: Iterable[str]) -> list[str]:
    """표기 패턴(그룹 1)·표 열 이름으로 명세 글에서 이 조절값의 값을 뽑는다. 못 뽑으면 빈 목록 = "명세에 없음"."""
    texts = list(texts)
    found: list[str] = []
    for pattern in param.spec_patterns:
        rx = re.compile(pattern)
        for text in texts:
            found += [m.group(1).strip() for m in rx.finditer(text) if m.group(1)]
    if param.spec_columns:
        cols = [re.compile(c, re.IGNORECASE) for c in param.spec_columns]
        for text in texts:
            found += _column_values(text, cols)
    return list(dict.fromkeys(found))


def _same(value: HarnessValue, raw: str) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        n = _number(raw)
        return n is not None and n == float(value)
    if isinstance(value, str):
        return value.strip().casefold() == raw.strip().casefold()
    return False  # 목록 값은 명세 대조 대상이 아니다 (주입 대상 id 등)


def picked_values(param: HarnessParam, seen: Sequence[str]) -> list[str]:
    """표기 패턴으로 뽑은 값 중 정답 후보 (`pick`). unique 인데 서로 다른 값이 여럿이면 모두 돌려준다 (모호 — 승인자 확인)."""
    nums = [(raw, _number(raw)) for raw in seen]
    if param.pick != "unique" and nums and all(n is not None for _, n in nums):
        target = (max if param.pick == "max" else min)(n for _, n in nums)  # type: ignore[type-var]
        return [next(raw for raw, n in nums if n == target)]
    distinct: dict[object, str] = {}
    for raw, n in nums:
        distinct.setdefault(n if n is not None else raw.strip().casefold(), raw)
    return list(distinct.values())


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 근거 인용 안에서 값 후보가 아닌 숫자 — 백분율(38%)·시각(19:00)·백분위 이름(p50·p95·p99 — 2026-11-11 N+1: 'p50: 8.1' 의 50)
_NOT_VALUE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*%|\d{1,2}:\d{2}|\b[pP]\d{2,3}(?:\.\d)?\b")
NO_EVIDENCE = "없음"
MIN_QUOTE = 6
MAX_QUOTE = 160  # 분포·표 전체를 통째로 인용하면 그 안의 아무 숫자나 근거가 된다


def _norm(text: str) -> str:
    return " ".join(text.split())


# 단위가 붙은 수 — 조절값 단위(이름 끝 _ms·_s)로 환산해 맞춰 본다. 단위 뒤에 한글이 이어지면 조사·어미일 때만 단위다 (_timed)
_TIMED = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(ms|밀리초|초|sec|s|분|min)(?![A-Za-z])", re.I)
_UNIT_MS = {
    "ms": 1.0,
    "밀리초": 1.0,
    "초": 1000.0,
    "s": 1000.0,
    "sec": 1000.0,
    "분": 60000.0,
    "min": 60000.0,
}


def _timed(text: str) -> list[tuple[str, str]]:
    """글 속 시간 값 (수, 단위) — 단위 바로 뒤가 조사·어미가 아닌 한글이면 다른 낱말이라 뺀다 (_time_word 와 같은 기준).
    #238: 뒤 막기가 영문만 막아 '50 초과'를 50초, '3분기'를 3분, '초기'를 초로 읽어 근거 후보(timed_hints)·환산(_converted)에 넣었다."""
    return [
        (m.group(1), m.group(2))
        for m in _TIMED.finditer(text)
        if _time_word(text[m.start(2) : m.end(2) + 1]) is not None
    ]


def timed_matches(name: str, value: HarnessValue, texts: Iterable[str]) -> list[str]:
    """시간 조절값 값과 단위를 환산해 맞는 자료 줄 — 근거를 '없음'으로 쓴 값이 자료에 있는지 볼 때 보정(unevidenced)과
    검사(_harness)가 같은 후보(TIMED_CANDIDATES)로 본다 (#179: 같은 계산이 두 벌이었다)."""
    unit = param_unit(name)
    if unit is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return []
    return [h for h in timed_hints(name, texts) if float(value) in _converted(h, unit)]


def unevidenced(param: HarnessParam, value: HarnessValue, texts: Iterable[str]) -> bool:
    """근거를 '없음'으로 쓴 조절값이 자료 어디에도 없는가 — 템플릿 표기로 뽑히는 값도, 단위를 환산해 맞는 시간 값도 없다."""
    texts = list(texts)
    return not extract_values(param, texts) and not timed_matches(param.name, value, texts)


EVIDENCE_FRAGMENT = "근거 인용 일부만 자료와 일치 — "


def _common_spans(a: str, b: str) -> list[str]:
    """a 와 b 에 함께 있는 연속 조각들 (각 끝 위치의 가장 긴 것, 긴 순)."""
    best: dict[str, None] = {}
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] >= MIN_QUOTE:
                    best[a[i - cur[j] : i]] = None
        prev = cur
    return sorted(best, key=len, reverse=True)


def evidence_fragment(
    value: HarnessValue,
    quote: str,
    value_text: str | None,
    texts: Iterable[str],
    unit: str | None = None,
    rule: HarnessRule | None = None,
) -> str | None:
    """인용이 자료에 그대로 없을 때, 자료에 그대로 있는 조각 중 값 글자가 든 **가장 긴** 것 — 값 글자 검사(`value_check`)가 받을 때만.
    그 조각이 걸리면 더 짧은 조각으로 내려가지 않는다: 짧게 자르면 같은 종류의 다른 값(이름표 p50 이 붙은 값 등)이 잘려 나가
    이름표·여럿 판정을 피해 간다. 2026-11-12 백엔드 갱신 손실: 자료의 '2024-11-15 10:00:01 …' 을 '2024-11-15T10:00:01 …' 로
    인용해 계약이 세 번 다 막혔다."""
    texts = [_norm(t) for t in texts]
    q = _norm(quote).strip(" \"'“”‘’…")
    if any(q in t for t in texts):
        return None  # 그대로 있음 — 이 보정의 대상이 아니다
    spans = sorted({s for t in texts for s in _common_spans(q, t)}, key=len, reverse=True)
    for span in spans:
        frag = span.strip()
        if MIN_QUOTE <= len(frag) <= MAX_QUOTE and _value_spans(frag, value_text or ""):
            ok = value_check(value, frag, value_text, texts, unit, rule).verdict == "ok"
            return frag if ok else None
    return None


def param_unit(name: str) -> str | None:
    """조절값 이름의 시간 단위 — `…_ms` 는 ms, `…_s` 는 s, 그 밖은 없음."""
    return "ms" if name.endswith("_ms") else "s" if name.endswith("_s") else None


def _converted(quote: str, unit: str) -> set[float]:
    """인용 속 단위가 붙은 수를 조절값 단위로 — 정수 조절값이 받을 수 있게 반올림·올림 후보까지.
    2026-11-10 nplus1-e: 'p50 은 11.4ms' 로 query_delay_ms=12, '목표는 500ms' 로 request_timeout_s=1 을 냈는데
    인용 안에 12·1 이 그대로 없다고 거절했다 (정수 조절값이라 반올림·초 단위 올림이 맞다)."""
    out: set[float] = set()
    for raw, u in _timed(quote):
        n = _number(raw)
        if n is None:
            continue
        x = n * _UNIT_MS[u.lower()] / (1.0 if unit == "ms" else 1000.0)
        out |= {x, float(round(x)), float(math.ceil(x))}
    return out


# 시간 조절값의 근거 후보 줄 수 — 근거 대조(timed_matches)와 재질문 힌트(plan_contract)가 같은 후보를 본다 (#179: 20·20·4 로 갈려
# 대조가 받는 줄을 힌트가 보여 주지 않을 수 있었다)
TIMED_CANDIDATES = 20


def timed_hints(name: str, texts: Iterable[str], limit: int = TIMED_CANDIDATES) -> list[str]:
    """시간 조절값의 근거 후보 — 명세 자료·답에서 그 단위로 환산할 수 있는 수가 든 줄 (근거를 '없음'으로 쓴 답을 다시 묻는다)."""
    if param_unit(name) is None:
        return []
    out: list[str] = []
    for text in texts:
        for line in text.splitlines():
            line = line.strip()
            if _timed(line) and line not in out:
                out.append(line[:MAX_QUOTE])
    return out[:limit]


def _listed_value_ok(value: HarnessValue, quote: str) -> bool:
    """수 하나가 아닌 조절값(문자열·목록) — 문자열은 인용 안에 그대로, 목록은 모든 수가 인용의 수 후보에 있다."""
    if isinstance(value, list):
        numbers = {_number(n) for n in _NUM.findall(_NOT_VALUE.sub(" ", quote))}
        return all(float(v) in numbers for v in value)
    return str(value).lower() in quote.lower()


# ── 근거 값 글자로 대조 (hidden_plan.evidence_value · FeatureSeed.harness_rules) ─────────────────
#
# 계약은 근거 인용(evidence)과 함께 그 인용 안에서 값이 적힌 글자(evidence_value — 수와 단위, 범위면 범위째)를 낸다.
# 검사는 인용 문장에서 어느 수가 값인지 추측하지 않는다 — 값 글자가 가리키는 값 토큰만 재료 규칙대로 환산하고, 같은 종류의 값 토큰
# (시간 조절값이면 시간 단위가 붙은 수)끼리만 이름표·pick·여럿을 본다. 단위 없는 수(절 번호·목록 번호)는 시간·건수 값이 아니다.
# 2026-11-15 운영 계약 실패: 인용 '3.1 스크롤 … 상한: 500ms (p95 기준)' 의 절 번호 3.1 을 값으로 읽어 request_timeout_s 기대값 4 를
# 만들었고(재요청이 그 틀린 값을 알려 줬다), 범위 '14~16ms' 는 이름표가 앞 수에만·단위가 뒤 수에만 붙어 갈렸다. 인용 문장에서 값을 다시
# 읽는 방식은 알파마다 표기 규칙이 하나씩 늘었다 (11-07 분포·11-11 반올림·11-13 이름표·11-15 절 번호·범위).

# 값이 아닌 수 — 날짜·시각·백분율 (같은 길이 공백으로 지워 위치를 지킨다)
_RULE_SKIP = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}:\d{2}(?::\d{2})?|\d[\d,]*(?:\.\d+)?\s*%")
# 공백 하나 뒤에 와도 단위로 보는 말 — 그 밖의 말은 다음 항목이다 ('19.8 p95' · '35.1 최대' · '3.1 스크롤')
_COUNT_UNITS = ("건", "개", "명", "회", "번", "행", "배", "대", "곳", "줄")
# 개수가 아닌 양의 단위 — 배수·기간·돈·크기 (시간 단위는 _time_word). 개수 조절값의 값에 붙으면 다른 종류이고, 그 밖에 붙은 말(조사·어미)은
# 단위가 아니다. 2026-11-19 운영 ct-d367bb7b2d4c-b1: 개수 조절값이 '개수 단위나 단위 없음'만 받아 원문 '동시 활성 요청 수가 24로' 의
# 조사 '로'를 단위로 보고 맞는 근거를 세 번 다 거절했다 — 조사는 열거하지 않고 단위만 알아본다. '만큼'은 배수 '만'이 아니라 조사
_NON_COUNT = ("만", "억", "천", "시간", "일", "주", "개월", "년", "원", "gb", "mb", "kb", "tb")
# 수 바로 뒤의 비교 말 — 단위가 아니다 ('동시 요청 50 초과' · '50초과' · '2 이상'). 개수 조절값에서는 단위 없는 수로 보고, 시간 조절값에서는
# 시간 값이 아니다 (머리 줄 단위가 있으면 그 단위). #238: '초과'를 '초'로 읽어 개수 값 '50 초과'를 wrong_unit 으로 재요청했고(같은 근거뿐이면
# 계약 실패), 시간 조절값은 개수 문장 '2 초과'를 2초로 받아 통과시켰다
_COMPARE = ("초과", "이상", "이하", "미만")
# 머리 줄의 단위 표기 — '왕복 시간 (ms)' · '단위: 초'. 단위 없는 수는 이 단위
_RULE_HEAD = re.compile(
    r"\(\s*(?:단위\s*[:：]?\s*)?(ms|밀리초|초|sec|s|분|min)\s*\)|단위\s*[:：]\s*(ms|밀리초|초|sec|s|분|min)(?![A-Za-z])",
    re.I,
)
# 값 앞 이름표 — 백분위·평균·최대 등 (재료 규칙의 이름표와 함께 찾는다)
_RULE_STAT = r"[pP]\d{2,3}(?:\.\d)?|평균|중앙값|최댓값|최솟값|최대|최소|avg|mean|median|max|min"
_RULE_LEAD = 16  # 이름표를 찾는 값 앞 글자 수 (앞 토큰이 끝난 뒤부터)
# 시간 단위 낱말 — 앞에서부터 맞춘다 ('ms' 를 's' 보다 먼저). 뒤에 영문 글자가 이어지면 단위가 아니다 ('sql'·'msgs'·'minimum')
_TIME_WORDS = ("밀리초", "sec", "min", "ms", "분", "초", "s")
# 시간 단위 바로 뒤에 붙어도 단위로 읽는 한글의 첫 글자 — 조사·어미·의존 명사 ('2초를'·'30분마다'·'2초이내'·'2초초과'·'1분여'·'3초대'·
# '2초입니다'). 그 밖의 한글이 이어지면 다른 낱말이다 — '초과'·'초기'·'초안'·'분기'·'분석'·'분야' (#238). 받침 없는 '초'·'밀리초' 뒤의
# '과'는 조사가 아니고('와'가 조사 — '초과'), 받침 있는 '분' 뒤에는 '를·는·로·다·야…' 대신 '을·은·으로·이다·이야…'가 붙는다.
# 영문 단위(ms·s·sec·min) 뒤의 한글은 늘 조사·어미로 본다 ('500ms의'·'28ms입니다')
_UNIT_TAIL = "의에도만까부마씩쯤간내후뒤전대째정동짜남여넘이미초밖처뿐조보가인일임입"
_UNIT_TAIL_OPEN = "를는와로다였며고야나면라"  # 받침 없는 단위 (초·밀리초)
_UNIT_TAIL_CLOSED = "을은과으"  # 받침 있는 단위 (분)
# 수 — 쉼표는 세 자리 묶음일 때만 천 단위 구분 (1,200). 그 밖의 쉼표는 나열이라 수를 나눈다 — 예전 꼴(\d[\d,]*\d)은 '85,91,88건'을
# 한 토큰 859188 로 읽어 '값(8591)과 다름'으로 헛 재요청했다 (설계 검사 #332 T-F5, #318 알파 frame-2·r2-3)
_TOK_NUM = r"(?:\d{1,3}(?:,\d{3})+(?![\d,]\d)|\d+)(?:\.\d+)?"
_TOK_WORD = r"[^\s\d.,:;()\[\]{}/|=~～∼<>≤≥·\-–—%]+"
# 값 토큰 — 수 + 붙은 말, 범위면 '수~수' 째 ('~' 는 띄어 써도, 붙임표는 붙여 쓸 때만 — '3건 - 5건' 은 둘).
# 앞이 글자·숫자·점·붙임표·빗금이면 수가 아니다 (p50 의 50, EV-2024-011 의 011, 버전 1.2.3 의 뒤쪽, 15/33ms 의 33)
_VALUE_TOKEN = re.compile(
    rf"(?<![A-Za-z0-9.\-_/])(?P<a>{_TOK_NUM})(?P<ua>{_TOK_WORD})?"
    rf"(?:(?P<sep>\s*[~～∼]\s*|[\-–—])(?P<b>{_TOK_NUM})(?P<ub>{_TOK_WORD})?)?"
)
_SPACED_WORD = re.compile(rf"\s({_TOK_WORD})")


def _time_word(word: str) -> str | None:
    """붙은 말이 시간 단위인가 — 단위 그대로이거나 단위 뒤에 조사·어미만 이어진 말 ('초를'·'ms의'·'분마다'·'초초과').
    #238: 앞부분만 보아 '초과'·'초기'·'분기'·'msgs'·'minimum' 을 시간 단위로 읽었다."""
    w = word.lower()
    for t in _TIME_WORDS:
        if w.startswith(t) and _unit_tail(t, w[len(t) :]):
            return t
    return None


def _non_count(word: str) -> bool:
    """붙은 말이 개수가 아닌 양의 단위인가 — 시간 단위, 또는 배수·기간·돈·크기 단위로 시작 ('24만건'·'24시간'·'24GB').
    '24로'·'24입니다'·'24건으로' 처럼 조사·어미·개수 단위는 아니다."""
    w = word.lower()
    return bool(_time_word(word)) or (w.startswith(_NON_COUNT) and not w.startswith("만큼"))


def _unit_tail(unit: str, rest: str) -> bool:
    """단위 뒤 글자가 단위를 끊지 않는가 — 없음, 영문 글자가 아닌 것(숫자·기호 — '1분30초'), 한글이면 조사·어미의 첫 글자."""
    if not rest:
        return True
    c = rest[0]
    if not "가" <= c <= "힣":
        return not (c.isascii() and c.isalpha())
    if unit.isascii():
        return True
    return c in _UNIT_TAIL + (_UNIT_TAIL_CLOSED if unit == "분" else _UNIT_TAIL_OPEN)


@dataclass(frozen=True)
class _Tok:
    start: int
    end: int
    raw: str  # 인용 속 표기 (예: '500ms', '14~16ms')
    nums: tuple[float, ...]  # 값 하나, 또는 범위의 두 끝
    units: tuple[
        str, ...
    ]  # 끝마다 붙은 말 ('' = 없음) — 범위의 앞 끝에 말이 없으면 뒤 끝의 말을 같이 쓴다
    label: str | None  # 바로 앞 이름표
    compared: bool = False  # 수 바로 뒤에 비교 말(_COMPARE) — units 에는 넣지 않는다


def _value_tokens(quote: str, rule: HarnessRule | None) -> tuple[list[_Tok], str | None]:
    """인용 속 값 토큰과 머리 줄 단위 — 날짜·시각·백분율·머리 줄 단위 표기는 지우고 본다."""
    q = _RULE_SKIP.sub(lambda m: " " * len(m.group(0)), quote)
    head = _RULE_HEAD.search(q)
    head_unit = (head.group(1) or head.group(2)).lower() if head else None
    if head:
        q = q[: head.start()] + " " * (head.end() - head.start()) + q[head.end() :]
    extra = tuple(rule.units) if rule is not None else ()
    names = sorted({*(rule.labels if rule is not None else ())}, key=len, reverse=True)
    label_rx = re.compile("|".join([*(re.escape(n) for n in names), _RULE_STAT]), re.I)
    out: list[_Tok] = []
    prev_end = 0
    for m in _VALUE_TOKEN.finditer(q):
        a, b = _number(m.group("a")), _number(m.group("b") or "")
        if a is None:
            continue
        end = m.end()
        tail = m.group("ub") if m.group("b") else m.group("ua")
        spaced = None if tail else _SPACED_WORD.match(q, end)
        if spaced and (
            _time_word(spaced.group(1)) or spaced.group(1).startswith((*_COUNT_UNITS, *extra))
        ):
            tail, end = spaced.group(1), spaced.end()  # '500 ms' · '14 ~ 16 ms' · '17 건'
            spaced = None
        if b is None:
            nums, units = (a,), (tail or "",)
        else:  # 범위 — 한 끝에만 말이 붙었으면 두 끝이 같이 쓴다 ('14~16ms' · '14ms~16')
            ua = m.group("ua") or ""
            nums, units = (a, b), (ua or tail or "", tail or ua)
        # 비교 말은 단위가 아니다 — 붙여 쓴 꼴('50초과')은 단위 자리에서 빼고, 띄어 쓴 꼴('50 초과')은 토큰에 넣지 않고 표시만 (#238)
        compared = any(u.startswith(_COMPARE) for u in units) or bool(
            spaced and spaced.group(1).startswith(_COMPARE)
        )
        units = tuple("" if u.startswith(_COMPARE) else u for u in units)
        lead = q[max(prev_end, m.start() - _RULE_LEAD) : m.start()]
        labels = list(label_rx.finditer(lead))
        out.append(
            _Tok(
                m.start(),
                end,
                quote[m.start() : end].strip(),
                nums,
                units,
                labels[-1].group(0) if labels else None,
                compared,
            )
        )
        prev_end = end
    return out, head_unit


def _value_spans(quote: str, value_text: str) -> list[tuple[int, int]]:
    """값 글자가 인용에 나오는 자리 — 띄어쓰기 차이는 무시 ('500ms' ↔ '500 ms', '14~16ms' ↔ '14 ~ 16 ms')."""
    chars = [c for c in value_text if not c.isspace()]
    if not chars:
        return []
    # 다른 수의 일부는 아니다 ('8' 은 '17–18시' 의 8 이 아니다)
    head = r"(?<![\d.])" if chars[0].isdigit() else ""
    tail = r"(?!\.?\d)" if chars[-1].isdigit() else ""
    rx = re.compile(head + r"\s*".join(re.escape(c) for c in chars) + tail)
    return [(m.start(), m.end()) for m in rx.finditer(quote)]


def _amount(n: float, word: str, head_unit: str | None, unit: str | None) -> float:
    """조절값 단위로 — 시간 조절값은 붙은 시간 단위(없으면 머리 줄 단위, 그것도 없으면 조절값 단위)로 환산."""
    if unit is None:
        return n
    src = _time_word(word) if word else head_unit
    return n if src is None else n * _UNIT_MS[src] / (1.0 if unit == "ms" else 1000.0)


@dataclass(frozen=True)
class ValueCheck:
    verdict: Literal[
        "ok",
        "mismatch",
        "bad_quote",
        "no_value_text",
        "bad_value_text",
        "no_unit",
        "wrong_unit",
        "wrong_label",
        "not_extreme",
        "ambiguous",
    ]
    expected: tuple[float, float] | None = None  # 규칙대로 맞춘 값 (범위면 두 끝)
    source: tuple[str, ...] = ()  # 판정에 쓴 인용 속 토큰
    notes: tuple[str, ...] = ()  # 통과지만 승인자 확인


def _show(expected: tuple[float, float] | None) -> str:
    if expected is None:
        return "?"
    lo, hi = expected
    return f"{lo:g}" if lo == hi else f"{lo:g}~{hi:g}"


# 단위 없는 근거 값을 다른 시간 단위로 읽기 — 조절값 단위 → (곱할 수, 승인자 표시). 근거에 단위가 없어 어느 쪽인지 정할 수 없을 때만
_BARE_SWAP: dict[str, tuple[float, str]] = {
    "ms": (
        1000.0,
        "근거 값 글자 {vt!r} 에 단위가 없어 초로 읽은 값 (밀리초로 읽으면 다름) — 승인자 확인",
    ),
    "s": (
        0.001,
        "근거 값 글자 {vt!r} 에 단위가 없어 밀리초로 읽은 값 (초로 읽으면 다름) — 승인자 확인",
    ),
}


def _swapped_notes(notes: list[str], note: str) -> tuple[str, ...]:
    """단위를 바꿔 읽은 값이면 '조절값 단위로 읽음' 표시를 바꿔 읽은 표시로."""
    return (*(n for n in notes if "조절값 단위" not in n), note)


def value_check(
    value: HarnessValue,
    quote: str,
    value_text: str | None,
    texts: Iterable[str],
    unit: str | None,
    rule: HarnessRule | None = None,
) -> ValueCheck:
    """근거 인용 + 그 안의 값 글자로 조절값을 확인한다 — 인용이 자료에 그대로 있고, 값 글자가 인용 속 값 토큰 하나를 가리키고, 그 토큰을
    재료 규칙(없으면 값 그대로·정수로 맞춤)대로 환산한 값이 계약의 값과 같아야 ok. 이름표·pick·여럿은 같은 종류의 값 토큰끼리만 본다
    (시간 조절값은 시간 단위가 붙은 수, `rule.units` 가 있으면 그 단위가 붙은 수, 그 밖은 단위 없는 수·개수).
    범위 토큰은 pick 이 max·min 이면 그 끝, only 면 범위 안의 값을 모두 받는다."""
    q = quoted(quote, texts)
    if q is None:
        return ValueCheck("bad_quote")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ValueCheck("ok" if _listed_value_ok(value, q) else "mismatch")
    vt = _norm(value_text or "").strip(" \"'“”‘’")
    if not vt:
        return ValueCheck("no_value_text")
    toks, head_unit = _value_tokens(q, rule)
    hits = sorted(
        {t for s, e in _value_spans(q, vt) for t in toks if t.start < e and s < t.end},
        key=lambda t: t.start,
    )
    if not hits or len({(t.nums, t.units, t.label) for t in hits}) > 1:
        return ValueCheck("bad_value_text", source=tuple(t.raw for t in toks))
    claimed = hits[0]  # 같은 값이 두 번 적혀도 (같은 수·단위·이름표) 하나로 본다
    extra = tuple(rule.units) if rule is not None else ()

    def kind(t: _Tok) -> Literal["same", "bare", "other"]:
        words = [w for w in t.units if w]
        if unit is not None:  # 시간 조절값
            if words:
                return "same" if all(_time_word(w) for w in words) else "other"
            if head_unit:
                return "same"
            # 단위 없는 수 뒤 비교 말('2 초과')은 개수 문장 — 시간 값이 아니다 (#238)
            return "other" if t.compared else "bare"
        if extra:
            return "same" if words and all(w.startswith(extra) for w in words) else "other"
        return "other" if any(_non_count(w) for w in words) else "same"

    same = [t for t in toks if kind(t) == "same"]
    notes: list[str] = []
    if kind(claimed) == "other":
        return ValueCheck("wrong_unit", source=(claimed.raw,))
    if kind(claimed) == "bare":
        if (
            same
        ):  # 인용에 시간 단위가 붙은 값이 있으면 단위 없는 수는 값이 아니다 (절 번호·목록 번호)
            return ValueCheck("no_unit", source=tuple(t.raw for t in same))
        same = [t for t in toks if kind(t) == "bare"]
        notes.append(f"근거 값 글자 {vt!r} 에 단위가 없어 조절값 단위({unit})로 읽음")
    cands = same
    if rule is not None and rule.labels:
        names = {t.label.casefold() for t in same if t.label}
        chosen = next((n.casefold() for n in rule.labels if n.casefold() in names), None)
        cands = [t for t in same if (t.label.casefold() if t.label else None) == chosen]
        if claimed not in cands:
            return ValueCheck("wrong_label", source=tuple(t.raw for t in cands))
    span = {
        t: (
            min(_amount(n, w, head_unit, unit) for n, w in zip(t.nums, t.units, strict=True)),
            max(_amount(n, w, head_unit, unit) for n, w in zip(t.nums, t.units, strict=True)),
        )
        for t in cands
    }
    lo, hi = span[claimed]
    pick = rule.pick if rule is not None else "only"
    if pick == "max":
        top = max(h for _, h in span.values())
        if hi != top:
            return ValueCheck(
                "not_extreme", source=tuple(t.raw for t in cands if span[t][1] == top)
            )
        lo = hi
    elif pick == "min":
        bottom = min(low for low, _ in span.values())
        if lo != bottom:
            return ValueCheck(
                "not_extreme", source=tuple(t.raw for t in cands if span[t][0] == bottom)
            )
        hi = lo
    elif len(set(span.values())) > 1:
        if rule is not None:
            return ValueCheck("ambiguous", source=tuple(t.raw for t in cands))
        notes.append(f"근거 인용에 같은 종류의 숫자가 여럿 — 값 글자 {vt!r} 를 씀")
    v, eps = float(value), 1e-9
    # 근거 값에 단위가 없으면(머리 줄·붙은 단위 모두 없음) 어느 단위인지 정할 수 없다 — 조절값 단위로 읽은 값과 초↔밀리초로 바꿔 읽은
    # 값을 둘 다 받고, 바꿔 읽은 쪽이면 승인자 표시. 표기 패턴(메트릭 이름의 _seconds 등)을 하나씩 더하지 않는다.
    # 2026-11-25 목록 조회 알파: 'db_query_roundtrip_latency_seconds … p50=0.038' 을 ms 로만 읽어 0 을 강요, 38 을 거절해 계약 실패
    alt = _BARE_SWAP.get(unit or "") if kind(claimed) == "bare" else None
    if rule is not None:
        expected = (_fit(lo, rule.round), _fit(hi, rule.round))
        if expected[0] - eps <= v <= expected[1] + eps:
            return ValueCheck("ok", expected, (claimed.raw,), tuple(notes))
        if alt is not None:
            swapped = (_fit(lo * alt[0], rule.round), _fit(hi * alt[0], rule.round))
            if swapped[0] - eps <= v <= swapped[1] + eps:
                return ValueCheck(
                    "ok", swapped, (claimed.raw,), _swapped_notes(notes, alt[1].format(vt=vt))
                )
        return ValueCheck("mismatch", expected, (claimed.raw,))
    # 규칙 없는 조절값 — 값 그대로, 또는 정수로 맞춘 값. 시간 단위를 바꾼 값의 반올림·올림은 표시 없이 받고(500ms → 1초),
    # 그 밖에 정수로 맞춘 것은 승인자 표시 [확정: 2026-11-11 사용자 — 'p50: 8.1' 로 8]
    if lo - eps <= v <= hi + eps:
        return ValueCheck("ok", (lo, hi), (claimed.raw,), tuple(notes))
    converted = unit is not None and any(_time_word(w) for w in claimed.units if w)
    for x in (lo, hi):
        if converted and v in (_fit(x, "nearest"), _fit(x, "up")):
            return ValueCheck("ok", (lo, hi), (claimed.raw,), tuple(notes))
        if v in (_fit(x, "nearest"), _fit(x, "down"), _fit(x, "up")):
            notes.append(f"근거 값 글자 {claimed.raw!r} 를 정수로 맞춘 값")
            return ValueCheck("ok", (lo, hi), (claimed.raw,), tuple(notes))
    if alt is not None:
        for x in (lo * alt[0], hi * alt[0]):
            if lo * alt[0] - eps <= v <= hi * alt[0] + eps or v in (
                _fit(x, "nearest"),
                _fit(x, "up"),
                _fit(x, "down"),
            ):
                return ValueCheck(
                    "ok",
                    (lo * alt[0], hi * alt[0]),
                    (claimed.raw,),
                    _swapped_notes(notes, alt[1].format(vt=vt)),
                )
    return ValueCheck("mismatch", (lo, hi), (claimed.raw,))


def _fit(x: float, how: str) -> float:
    eps = 1e-9
    if how == "nearest":
        return float(math.floor(x + 0.5 + eps))
    if how == "up":
        return float(math.ceil(x - eps))
    if how == "down":
        return float(math.floor(x + eps))
    return x


def quoted(quote: str, texts: Iterable[str]) -> str | None:
    """인용을 공백 정리해 돌려준다 — 길이가 맞고 자료(1단계 자료·구체 답)에 그대로 있을 때만."""
    q = _norm(quote).strip(" \"'“”‘’…")
    if not MIN_QUOTE <= len(q) <= MAX_QUOTE or not any(q in _norm(t) for t in texts):
        return None
    return q


def describe_rule(rule: HarnessRule, unit: str | None) -> str:
    """규칙을 사람 말로 — 재요청 사유·계약 입력의 조절값 설명에 쓴다."""
    parts: list[str] = []
    if rule.labels:
        head, *rest = rule.labels
        parts.append(f"이름표 {head}" + (f"(없으면 {'·'.join(rest)})" if rest else "") + " 의 값")
    if rule.units:
        parts.append(f"{'·'.join(rule.units)} 단위가 붙은 수")
    if rule.pick != "only":
        parts.append("여럿이면(범위면) 가장 " + ("큰" if rule.pick == "max" else "작은") + " 값")
    else:
        parts.append("범위로 적혔으면 범위 안의 값")
    fit = {"exact": "", "nearest": " 반올림", "up": " 올림", "down": " 내림"}[rule.round]
    if unit:
        parts.append(f"{'초' if unit == 's' else unit} 단위로 바꿔" + (fit or " 그대로"))
    elif fit:
        parts.append(f"정수로{fit}")
    return ", ".join(parts)


def value_problem(
    name: str,
    value: HarnessValue,
    quote: str,
    value_text: str | None,
    got: ValueCheck,
    rule: HarnessRule | None,
    unit: str | None,
) -> str:
    """값 글자 검사 결과 → 재요청 사유 (문장 머리는 'HiddenPlan <조절값>' — builder 실패 사유는 그 앞까지만 싣는다)."""
    how = describe_rule(rule, unit) if rule is not None else ""
    vt = (value_text or "").strip()
    q = quote[:120]
    src = ", ".join(got.source)
    if got.verdict == "bad_quote":
        return (
            f"HiddenPlan {name}={value} 의 근거 {q!r} 가 자료에 그대로 없음 — 자료·답의 문장을 그대로 인용 "
            f"({MAX_QUOTE}자 이내, 값이 적힌 부분만)"
        )
    if got.verdict == "no_value_text":
        return (
            f"HiddenPlan {name} 의 근거 값 글자가 없음 — evidence_value 에 근거 인용 안에서 값이 적힌 글자를 그대로 "
            "(수와 단위, 범위면 범위째)"
        )
    if got.verdict == "bad_value_text":
        return (
            f"HiddenPlan {name} 의 근거 값 글자 {vt!r} 가 근거 인용 안의 값 하나(수와 단위)를 가리키지 않음 — "
            f"인용 {q!r} 안에서 값이 적힌 글자를 그대로 (수와 단위, 범위면 범위째)"
        )
    if got.verdict == "no_unit":
        return (
            f"HiddenPlan {name} 의 근거 값 글자 {vt!r} 에 시간 단위가 없음 — 절 번호·목록 번호 같은 단위 없는 수는 값이 아니다. "
            f"인용의 시간 값({src}) 중에서 고른다"
        )
    if got.verdict == "wrong_unit":
        want = (
            "시간 단위"
            if unit
            else f"{'·'.join(rule.units)} 단위"
            if rule is not None and rule.units
            else "개수 단위 또는 단위 없는 수"
        )
        return f"HiddenPlan {name} 의 근거 값 글자 {vt!r} 의 단위가 이 조절값의 것이 아님 — {want}가 붙은 값을 쓴다"
    if got.verdict in ("wrong_label", "not_extreme"):
        return f"HiddenPlan {name} 의 근거 값 글자 {vt!r} 가 재료 규칙({how})의 값이 아님 — " + (
            f"규칙에 맞는 값: {src}"
            if src
            else "이 인용에는 규칙에 맞는 값이 없음 (그 이름표가 붙은 값이 적힌 줄을 인용)"
        )
    if got.verdict == "ambiguous":
        return (
            f"HiddenPlan {name} 의 근거 인용 {q!r} 에 재료 규칙({how})의 값 후보가 여럿({src}) — "
            "그 값만 적힌 부분을 인용"
        )
    if rule is not None:
        return (
            f"HiddenPlan {name}={value} 이 근거 값 글자 {vt!r} 를 재료 규칙({how})으로 맞춘 값 "
            f"{_show(got.expected)} 과 다름 — 그 값을 쓴다"
        )
    return (
        f"HiddenPlan {name}={value} 이 근거 값 글자 {vt!r} 의 값({_show(got.expected)})과 다름 — "
        "그 값을 쓴다 (정수 조절값이면 반올림·올림, 시간은 조절값 단위로 바꿔)"
    )


def spec_missing_params(visible_texts: Sequence[str], manifest: TemplateManifest) -> list[str]:
    """명세 글에서 표기 패턴으로 값이 안 뽑히는 필수·visible 조절값 이름.

    명세 단계 사전 검사용 (§3.3 [제안] — 기능형 서술 검증에서 템플릿이 정해지면 재요청 사유로 쓴다).
    """
    return [
        p.name
        for p in manifest.harness_params
        if p.visible and p.required and not extract_values(p, visible_texts)
    ]


def _type_problem(param: HarnessParam, value: HarnessValue) -> str | None:
    ok = {
        "int": isinstance(value, int) and not isinstance(value, bool),
        "float": isinstance(value, int | float) and not isinstance(value, bool),
        "str": isinstance(value, str),
        "int_list": isinstance(value, list) and all(isinstance(x, int) for x in value),
    }[param.kind]
    if not ok:
        return f"HiddenPlan {param.name} 의 값 형식이 {param.kind} 가 아님"
    nums = value if isinstance(value, list) else [value]
    for n in nums:
        if isinstance(n, int | float) and not isinstance(n, bool):
            if param.minimum is not None and n < param.minimum:
                return f"HiddenPlan {param.name}={n} 이 템플릿 하한 {param.minimum:g} 보다 작음"
            if param.maximum is not None and n > param.maximum:
                return f"HiddenPlan {param.name}={n} 이 템플릿 상한 {param.maximum:g} 보다 큼"
    return None


def _harness(
    plan: HiddenPlan,
    spec: SpecView,
    manifest: TemplateManifest,
    rules: Mapping[str, HarnessRule] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    problems: list[str] = []
    gaps: list[str] = []
    flags: list[str] = []  # 통과는 시키되 승인자가 볼 것
    known = {p.name for p in manifest.harness_params}
    unknown = [k for k in plan.harness if k not in known]
    if unknown:
        problems.append(
            f"HiddenPlan harness 에 템플릿에 없는 조절값: {', '.join(unknown)} "
            f"(쓸 수 있는 것: {', '.join(sorted(known)) or '없음'})"
        )
    for p in manifest.harness_params:
        if p.name not in plan.harness:
            if p.required:
                problems.append(f"HiddenPlan harness 에 필수 조절값 {p.name} 이 없음")
            elif p.visible and (seen := extract_values(p, spec.visible_texts)):
                # 지원자가 자료에서 본 운영 값은 채점에도 그대로 — 빼면 하네스 기본값이 대신 들어가 실패 사유가 달라진다
                problems.append(
                    f"명세 자료에 운영 값 {p.name}({', '.join(seen)})이 있으니 HiddenPlan harness 에 넣는다"
                )
            continue
        value = plan.harness[p.name]
        bad = _type_problem(p, value)
        if bad:
            problems.append(bad)
            continue
        if not p.visible:
            continue
        quote = plan.evidence.get(p.name)
        vtext = plan.evidence_value.get(p.name)
        rule = (rules or {}).get(p.name)
        unit = param_unit(p.name)
        cited = (
            value_check(value, quote, vtext, spec.visible_texts, unit, rule)
            if quote and quote.strip() != NO_EVIDENCE
            else None
        )
        if cited is not None and rule is not None:
            # 재료가 값 만드는 규칙을 적었으면 그 규칙으로만 대조 — 템플릿 표기 패턴·일반 검사보다 숨은 요구가 정한 규칙이 정확하다
            if cited.verdict != "ok":
                problems.append(value_problem(p.name, value, quote or "", vtext, cited, rule, unit))
            elif cited.notes:
                flags.append(f"운영 값 {p.name}={value} — {'; '.join(cited.notes)}, 승인자 확인")
            continue
        seen = extract_values(p, spec.visible_texts)
        if seen:
            # 템플릿 표기 패턴이 값을 뽑았으면 그 값(pick)이 정답 — 패턴과 다른 값은 근거가 하나로 분명할 때만 받는다
            allowed = picked_values(p, seen)
            if any(_same(value, raw) for raw in allowed):
                if len(allowed) > 1:
                    flags.append(
                        f"운영 값 {p.name} 후보가 자료에 여럿({', '.join(allowed)}) — {value} 을 사용했음, 승인자 확인"
                    )
                continue
            if cited is not None and cited.verdict == "ok" and not cited.notes:
                flags.append(
                    f"운영 값 {p.name}={value} 이 템플릿 표기 패턴의 값({', '.join(allowed)})과 다름 — 근거 인용 {quote!r} 로 수용함, 승인자 확인"
                )
                continue
            problems.append(
                f"HiddenPlan {p.name}={value} 이 명세 자료의 값({', '.join(allowed)})과 다름 — 명세 자료의 값을 쓰고 evidence 에 그 값이 적힌 "
                "부분만 인용, evidence_value 에 그 안의 값 글자"
            )
            continue
        if cited is not None and cited.verdict == "ok":
            if cited.notes:  # 정수로 맞춤·같은 종류 값이 여럿·단위 없는 수 — 통과, 승인자 확인
                flags.append(f"운영 값 {p.name}={value} — {'; '.join(cited.notes)}, 승인자 확인")
            continue
        if cited is not None:
            problems.append(value_problem(p.name, value, quote or "", vtext, cited, None, unit))
        elif quote and quote.strip() == NO_EVIDENCE:
            # 시간 조절값인데 자료에 단위를 환산해 그 값이 되는 줄이 있으면 막지 않고 승인자에게 — 2026-11-10 nplus1-i: 자료의
            # '응답 p99 ≤ 800 ms' 로 request_timeout_s=1 을 맞게 정하고도 '시간 초과 값' 이 따로 없다며 근거를 세 번 다 '없음'으로 썼다
            matched = timed_matches(p.name, value, spec.visible_texts)
            if matched:
                flags.append(
                    f"운영 값 {p.name}={value} 의 근거를 계약이 '없음'으로 작성함. 자료의 시간 값과 환산해 일치함: "
                    + " / ".join(matched[:3])
                    + " — 승인자 확인"
                )
            elif p.required:
                gaps.append(
                    f"명세 1단계 자료·추가 요구 구체 답에 운영 수치 {p.name} 이 없음 — "
                    "지원자가 발견할 수 없는 조건으로 채점하게 되므로 명세 수정 필요"
                )
            # 선택 조절값의 '없음'은 보정(plan_contract.fit_unevidenced)이 먼저 뺀다 — 검사 가지를 두지 않는다 (#179: 닿지 않던 검사)
        else:
            problems.append(
                f"공개 조절값 {p.name}={value} 의 근거가 없음 — evidence 에 그 값이 적힌 자료·답 문장을 그대로, evidence_value 에 "
                f'그 안의 값 글자를 적는다 (자료에 없으면 evidence 에 "{NO_EVIDENCE}")'
            )
    return problems, gaps, flags


# ── 계약 ───────────────────────────────────────────────────

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _words(identifier: str) -> str:
    """connectionPoolSize · max_pool · /orders/{id}/retry-key → 낱말로 풀어 쓴 글."""
    return re.sub(r"[_\-/{}.]+", " ", _CAMEL.sub(" ", identifier)).strip().lower()


def _fields(contract: TaskContract) -> list[tuple[str, ContractField]]:
    """(위치, 필드) — 요청·응답 본문·엔티티의 모든 필드."""
    out: list[tuple[str, ContractField]] = []
    for e in contract.endpoints:
        out += [(f"{e.endpoint_id} 헤더", f) for f in e.headers]
        out += [(f"{e.endpoint_id} 쿼리", f) for f in e.query]
        out += [(f"{e.endpoint_id} 요청", f) for f in e.request]
        for r in e.responses:
            out += [(f"{e.endpoint_id} 응답 {r.status}", f) for f in r.body]
    for m in contract.data_shapes():  # 테이블 엔티티 + 응답 모양 (응답 칸도 공개 이름)
        out += [(f"{m.entity_id} {m.name}", f) for f in m.fields]
    return out


def contract_fields(contract: TaskContract) -> list[tuple[str, ContractField]]:
    """(위치, 필드) — 정적 검사에서도 쓰는 공개 이름."""
    return _fields(contract)


def contract_places(contract: TaskContract) -> list[tuple[str, str]]:
    """(자리, 글) — 계약에서 지원자에게 보이는 글 전부 + 식별자를 낱말로 푼 것. 숨은 요구 핵심어가 어디에 나왔는지 짚을 때 쓴다.
    2026-11-11 재측정 토큰 갱신: 누설 검사가 낱말('재사용')만 알려 줘 계약 답 3개가 모두 같은 자리(오류 응답 설명)에 같은 말을 넣었다."""
    out: list[tuple[str, str]] = []
    for e in contract.endpoints:
        api = f"API {e.method or e.endpoint_id} {e.path}"
        out += [(f"{api} 경로", f"{e.path} {_words(e.path)}"), (f"{api} 요약", e.summary)]
        out += [(f"{api} 응답 {r.status} 설명", r.description) for r in e.responses]
    for m in contract.entities:
        out += [(f"엔티티 {m.name}", f"{m.name} {_words(m.name)}")]
        out += [(f"엔티티 {m.name} 관계", r) for r in m.relations]
    for s in contract.response_shapes:
        out += [(f"응답 모양 {s.name}", f"{s.name} {_words(s.name)}")]
    for where, f in _fields(contract):
        out += [
            (f"{where} 필드 {f.name}", f"{f.name} {_words(f.name)} {f.type} {f.constraints or ''}")
        ]
    for i, fx in enumerate(contract.fixtures, 1):
        out += [(f"시드 파일 {i} 요약", fx.summary), (f"시드 파일 {i} 내용", fx.content)]
    for sc in contract.screens:
        out += [(f"화면 {sc.route}", f"{sc.route} {sc.summary}")]
        out += [
            (
                f"화면 {sc.route} 요소 {el.test_id}",
                f"{el.test_id} {_words(el.test_id)} {el.description} {el.action or ''}",
            )
            for el in sc.elements
        ]
        out += [(f"화면 {sc.route} 저장소 {k.name}", f"{k.name} {k.shape}") for k in sc.storage]
    for s in contract.seed:
        out += [(f"시드 {s.entity_id} 규칙", r) for r in s.rules]
        out += [
            (f"시드 {s.entity_id} 값 {k}", f"{k} {v}")
            for g in s.groups
            for k, v in g.values.items()
        ]
    out += [(f"공개 테스트 {t.test_id}", t.checks) for t in contract.public_tests]
    # README 에 그대로 나가는 그 밖의 칸 — #232: 빠져 있어 숨은 낱말이 지원자 README 로 나가도 재요청하지 않았다 (ui 계약은 background 가
    # 필수라 프론트 과제는 늘 이 빈틈을 지났다). 앞 자리의 짚는 순서가 바뀌지 않게 끝에 둔다
    out += [(f"설정 {s.key}", f"{s.key} {_words(s.key)} {s.meaning}") for s in contract.settings]
    out += [("화면 조작 없이 나가는 요청(background)", contract.background or "")]
    out += [(f"화면 {sc.route} 들어올 때(on_load)", sc.on_load or "") for sc in contract.screens]
    out += [
        (f"시드 파일 {i} 위치(path)", f"{fx.path} {_words(fx.path)}" if fx.path else "")
        for i, fx in enumerate(contract.fixtures, 1)
    ]
    return [(w, t) for w, t in out if t and t.strip()]


def contract_text(contract: TaskContract) -> str:
    """계약에서 지원자에게 보이는 글 전부 + 식별자를 낱말로 푼 것 (숨은 요구 핵심어 검사용)."""
    return "\n".join(t for _, t in contract_places(contract))


def _quote(text: str, term: str, width: int = 30) -> str:
    """글에서 term 이 나온 곳 앞뒤 width 글자."""
    i = text.lower().find(term.lower())
    if i < 0:
        return " ".join(text.split())[: width * 2]
    s = " ".join(text[max(0, i - width) : i + len(term) + width].split())
    return ("…" if i > width else "") + s + ("…" if i + len(term) + width < len(text) else "")


def element_refs(contract: TaskContract) -> dict[str, list[str]]:
    """계약 요소 id → 그 요소가 스스로 적은 refs (요소·필드·화면 요소), 처음 나온 순서."""
    out: dict[str, list[str]] = {}

    def add(eid: str, refs: Iterable[str]) -> None:
        got = out.setdefault(eid, [])
        got += [r for r in refs if r not in got]

    for e in contract.endpoints:
        add(e.endpoint_id, e.refs)
        for f in [*e.headers, *e.query, *e.request, *(f for r in e.responses for f in r.body)]:
            add(e.endpoint_id, f.refs)
    for m in contract.entities:
        add(m.entity_id, [r for f in m.fields for r in f.refs])
    for sc in contract.screens:
        add(sc.screen_id, [*sc.refs, *(r for el in sc.elements for r in el.refs)])
        add(sc.screen_id, [r for k in sc.storage for r in k.refs])
    return out


def fit_traps(plan: HiddenPlan, spec: SpecView) -> tuple[HiddenPlan, list[str]]:
    """숨은 케이스의 trap_id 가 명세의 숨은 요구가 아니면(예: 실패 사유 코드를 적음) 그 케이스의 숨김 수용 기준(ac_id)이 가리키는 숨은 요구로 —
    ac_id → trap 은 명세가 정하므로 기계적으로 맞출 수 있다. 명세의 다른 숨은 요구를 적었으면 건드리지 않는다 (검사가 알린다).
    2026-11-09 데이터 알파 재검증: 계약 첫 답이 8회 모두 trap_id 에 사유 코드(late_data_missed …)를 적어 재요청 — 매번 호출 하나를 썼다."""
    cases = []
    notes: list[str] = []
    for c in plan.cases:
        want = spec.hidden_acs.get(c.ac_id)
        if want is not None and c.trap_id != want and c.trap_id not in spec.trap_ids:
            notes.append(
                f"숨은 케이스 {c.case_id} 의 trap_id {c.trap_id} → {want} ({c.ac_id} 의 숨은 요구)"
            )
            c = c.model_copy(update={"trap_id": want})
        cases.append(c)
    return (plan.model_copy(update={"cases": cases}) if notes else plan), notes


def fit_covers(contract: TaskContract, spec: SpecView) -> tuple[TaskContract, list[str]]:
    """covers 키는 공개 요구(req_ids)만 — 공개 수용 기준(p…) 키는 빼고, 어느 공개 요구에도 이어지지 않은 계약 요소는 그 요소가 스스로 적은
    refs 의 공개 요구에 잇는다 (역색인은 코드가, 기계적 출력).

    2026-11-08 상류 평가: 200 응답 모양으로 만든 엔티티(필드 refs r1·p1)를 covers 에 잇지도 빼지도 않아 재요청 2회 모두 실패.
    refs 는 공개 id 만 허용되므로 잇는다고 숨은 요구가 계약에 들어오지는 않는다.
    #180: 수용 기준 키는 공개 테스트가 대응하는 것이라 covers 에 두지 않는다 — 저장된 알파 계약 207개 중 100개가 p… 키를 썼고(재요청하면
    되풀이), 예전엔 그 값까지 '연결됨'으로 세어 요소를 수용 기준에만 이어도 통과했다. refs 가 수용 기준뿐인 요소는 어느 요구와 관련인지
    계약에서 알 수 없어 잇지 않는다 — '연결되지 않은 요소'로 재요청."""
    req = set(spec.req_ids)
    dropped = [k for k in contract.covers if k not in req and k in spec.public_ac_ids]
    covers = {k: list(v) for k, v in contract.covers.items() if k not in dropped}
    notes: list[str] = []
    if dropped:
        notes.append(
            f"covers 의 공개 수용 기준 키 {', '.join(dropped)} 를 뺌 — covers 키는 공개 요구만 (수용 기준은 공개 테스트가 대응)"
        )
    covered = {i for ids in covers.values() for i in ids}
    for eid, refs in element_refs(contract).items():
        if eid in covered:
            continue
        keys = [r for r in refs if r in req]
        if not keys:
            continue
        for k in keys:
            covers.setdefault(k, []).append(eid)
        notes.append(
            f"계약 요소 {eid} 를 covers {', '.join(keys)} 에 이음 (그 요소가 적은 refs 대로)"
        )
    if not notes:
        return contract, []
    return contract.model_copy(update={"covers": covers}), notes


def _dups(label: str, ids: Sequence[str]) -> list[str]:
    dup = sorted(i for i, n in Counter(ids).items() if n > 1)
    return [f"{label} 이 겹침: {', '.join(dup)}"] if dup else []


def _ids(contract: TaskContract, plan: HiddenPlan) -> list[str]:
    problems = _dups("엔드포인트 id", [e.endpoint_id for e in contract.endpoints])
    problems += _dups("엔티티 id", [m.entity_id for m in contract.entities])
    problems += _dups("엔티티 이름", [m.name for m in contract.entities])
    problems += _dups("공개 테스트 id", [t.test_id for t in contract.public_tests])
    problems += _dups("입력 환경 id", [f.fixture_id for f in contract.fixtures])
    problems += _dups("화면 id", [sc.screen_id for sc in contract.screens])
    for sc in (
        contract.screens
    ):  # 같은 부품이 여러 화면에 있는 것은 정상 — 화면 안에서만 유일 (2026-11-07 종이 단계)
        problems += _dups(f"화면 {sc.screen_id} 의 test id", [el.test_id for el in sc.elements])
    problems += _dups("숨은 케이스 id", [c.case_id for c in plan.cases])
    problems += _dups(
        "엔드포인트(메서드·경로)", [f"{e.method or ''} {e.path}" for e in contract.endpoints]
    )
    return problems


def _coverage(contract: TaskContract, plan: HiddenPlan, spec: SpecView) -> list[str]:
    problems: list[str] = []
    refs_ok = set(spec.req_ids) | set(spec.public_ac_ids)
    element_ids = (
        {e.endpoint_id for e in contract.endpoints}
        | {m.entity_id for m in contract.entities}
        | {sc.screen_id for sc in contract.screens}
    )

    # 계약 요소 → 명세 (거꾸로 대응)
    for e in contract.endpoints:
        bad = [r for r in e.refs if r not in refs_ok]
        if bad:
            problems.append(
                f"엔드포인트 {e.endpoint_id} 의 refs 에 명세에 없는 id: {', '.join(bad)}"
            )
    for sc in contract.screens:
        for where, refs in (
            [(f"화면 {sc.screen_id}", sc.refs)]
            + [(f"화면 {sc.screen_id} 요소 {el.test_id}", el.refs) for el in sc.elements]
            + [(f"화면 {sc.screen_id} 저장소 {k.name}", k.refs) for k in sc.storage]
        ):
            bad = [r for r in refs if r not in refs_ok]
            if bad:
                problems.append(f"{where} 의 refs 에 명세에 없는 id: {', '.join(bad)}")
    for st in contract.settings:
        bad = [r for r in st.refs if r not in refs_ok]
        if bad:
            problems.append(f"설정 {st.key} 의 refs 에 명세에 없는 id: {', '.join(bad)}")
    for fx in contract.fixtures:
        bad = [r for r in fx.refs if r not in refs_ok]
        if bad:
            problems.append(
                f"입력 환경 {fx.fixture_id} 의 refs 에 명세에 없는 id: {', '.join(bad)}"
            )
    for where, f in _fields(contract):
        bad = [r for r in f.refs if r not in refs_ok]
        if bad:
            problems.append(f"{where} 필드 {f.name} 의 refs 에 명세에 없는 id: {', '.join(bad)}")
    covered = {i for ids in contract.covers.values() for i in ids}
    loose = sorted(element_ids - covered)
    if loose:
        problems.append(
            f"어느 공개 요구(covers)에도 연결되지 않은 계약 요소: {', '.join(loose)} — "
            "관련된 공개 요구가 있으면 그 요구의 covers 에 잇는다 (기존 API 가 다루는 대상 엔티티는 그 API 를 쓰는 요구에). "
            "어느 요구와도 관련 없을 때만 뺀다 — 요구에 없는 인터페이스·엔티티는 해법을 흘린다"
        )

    # 명세 → 계약. covers 값에는 입력 환경 id(f*)도 된다 (요구를 입력 환경에 잇는 것은 자연스럽다 — 덮였는지 따지지는 않음).
    # 응답 모양(v*)도 된다 — 응답의 일부라 덮였는지 따지지는 않는다 (2026-11-13). 키는 공개 요구만 — 수용 기준 키는 fit_covers 가 뺀다 (#180)
    targets = (
        element_ids
        | {fx.fixture_id for fx in contract.fixtures}
        | {s.shape_id for s in contract.response_shapes}
    )
    for key, ids in contract.covers.items():
        if key not in spec.req_ids:
            problems.append(
                f"covers 의 키 {key} 가 명세의 공개 요구 id 가 아님"
                + (
                    " (공개 수용 기준은 covers 키가 아니다 — 공개 테스트가 대응)"
                    if key in spec.public_ac_ids
                    else ""
                )
            )
        bad = [i for i in ids if i not in targets]
        if bad:
            problems.append(f"covers[{key}] 에 계약에 없는 id: {', '.join(bad)}")
    for r in spec.req_ids:
        if not contract.covers.get(r):
            problems.append(f"공개 요구 {r} 가 계약 covers 에 없음")

    tested = {t.ac_id for t in contract.public_tests}
    for t in contract.public_tests:
        if t.ac_id not in spec.public_ac_ids:
            problems.append(
                f"공개 테스트 {t.test_id} 의 ac_id {t.ac_id} 가 명세의 공개 수용 기준이 아님"
            )
    for p in spec.public_ac_ids:
        if p not in tested:
            problems.append(f"공개 수용 기준 {p} 에 공개 테스트가 없음")

    planned = {c.ac_id for c in plan.cases}
    for c in plan.cases:
        if c.ac_id not in spec.hidden_acs:
            problems.append(
                f"숨은 케이스 {c.case_id} 의 ac_id {c.ac_id} 가 명세의 숨김 수용 기준이 아님"
            )
        elif spec.hidden_acs[c.ac_id] != c.trap_id:
            problems.append(
                f"숨은 케이스 {c.case_id} 의 trap_id {c.trap_id} 가 {c.ac_id} 의 숨은 요구"
                f"({spec.hidden_acs[c.ac_id]})와 다름"
            )
        if c.trap_id not in spec.trap_ids:
            problems.append(
                f"숨은 케이스 {c.case_id} 의 trap_id {c.trap_id} 가 명세의 숨은 요구가 아님"
            )
    for h in spec.hidden_acs:
        if h not in planned:
            problems.append(f"숨김 수용 기준 {h} 에 숨은 케이스가 없음")
    return problems


_STYLE_WORDS = {
    r"^[a-z][a-z0-9_]*$": "snake_case — 소문자·숫자·밑줄 (예 payment_id, run_date)",
    r"^[a-z][A-Za-z0-9]*$": "camelCase — 소문자로 시작, 밑줄·하이픈 없이 (예 paymentId, runDate)",
}


def field_style(rx: str) -> str:
    """필드 이름 규칙을 말로 — 정규식만 주면 작은 모델이 읽지 못한다 (2026-11-10 gpt-5.4-nano: camelCase·--run-date 를 재요청에도 반복)."""
    return _STYLE_WORDS.get(rx, f"정규식 {rx}")


def suggest_field(name: str, rx: str) -> str | None:
    """규칙에 맞는 이름 제안 — 앞의 하이픈을 떼고 낱말을 나눠 snake_case·camelCase 중 규칙에 맞는 쪽 (못 맞추면 None)."""
    parts = [w.lower() for w in re.split(r"[-_\s.]+|(?<=[a-z0-9])(?=[A-Z])", name.lstrip("-")) if w]
    if not parts:
        return None
    for cand in ("_".join(parts), parts[0] + "".join(w[:1].upper() + w[1:] for w in parts[1:])):
        if re.fullmatch(rx, cand):
            return cand
    return None


def _command_paths(contract: TaskContract, manifest: TemplateManifest) -> list[str]:
    """명령(메서드 없는 엔드포인트) 경로가 시작 코드가 파일을 만들 수 있는 곳(manifest fill_slots.starter)인가 — 아니면 시작 코드가 진입점을
    만들지 못해 생성이 실패한다. 2026-11-10 데이터 알파 12회차: 계약이 batch/daily_sales_summary.py 로 정해 첫 답 그대로 통과했고, 시작 코드가
    '쓸 수 없는 경로'로 재요청에도 실패해 빌드가 시작도 못 했다 (앞 11회는 모두 pipeline/ 아래)."""
    slots = manifest.fill_slots.get("starter") or []
    if not slots:
        return []
    out: list[str] = []
    for e in contract.endpoints:
        if e.method is not None or glob_match(e.path, slots):
            continue
        suffix = PurePosixPath(e.path).suffix
        near = [g for g in slots if suffix and g.endswith(suffix)] or slots
        out.append(
            f"명령 {e.endpoint_id} 경로 {e.path!r} 는 시작 코드가 파일을 만들 수 없는 곳 — {', '.join(near)} 안에 둔다 "
            f"(예: {near[0].replace('**/', '').replace('*', 'run')})"
        )
    return out


_IDENT_TOKEN = re.compile(r"(?<![A-Za-z0-9_.])[A-Za-z][A-Za-z0-9_]*[A-Za-z0-9](?![A-Za-z0-9_])")
_CAMEL_JOIN = re.compile(r"[a-z0-9][A-Z]")


def spec_named_fields(spec: SpecView | None) -> set[str]:
    """명세에서 지원자에게 보이는 글(제목·상황·공개 요구·공개 수용 기준·자료·추가 요구 답)에 글자 그대로 나온 식별자 이름 — 밑줄로 잇거나
    (snake) 대문자로 낱말을 이은(camel) 것. API 요청·응답 필드는 그 이름을 인정한다. 2026-11-10 확장 알파 C #062: 공개 요구가 '요청 본문에
    member_id 와 amount, 응답에 recharge_id' 로 필드 이름을 정했는데 템플릿 규칙(camelCase)과 달라 재요청 3번에도 실패 — 승인된 명세가
    우선이다 (사용자 결정 A). #180: snake 만 봐 거울 사례(명세 `rechargeId` ↔ snake 규칙 템플릿)는 같은 실패였다.
    경계는 영문자·숫자·밑줄로만 본다 ('recharge_id와' 처럼 조사가 붙어도). 낱말 하나(amount)는 어느 규칙에도 맞아 볼 필요가 없다."""
    if spec is None:
        return set()
    return {
        w
        for t in (*spec.public_texts, *spec.visible_texts)
        for w in _IDENT_TOKEN.findall(t)
        if "_" in w or _CAMEL_JOIN.search(w)
    }


# 분·시간 단위 설정 → 초로 곱할 수 (칸 unit 으로 — 키 이름·뜻 글자로 짐작하지 않는다)
_TO_SECONDS = {"minutes": 60, "hours": 3600}
SETTING_DEFAULT_MAX = 100_000  # AppSetting.default 상한


def _seconds_key(key: str, unit: str) -> str:
    """키 끝 마디가 단위 칸의 낱말(minutes·minute)이면 seconds 로 — 값이 초가 되므로 키가 다른 단위를 말하지 않게."""
    head, sep, last = key.rpartition("-") if "-" in key.rsplit(".", 1)[-1] else key.rpartition(".")
    return f"{head}{sep}seconds" if last in (unit, unit.removesuffix("s")) else key


def fit_setting_units(
    contract: TaskContract, plan: HiddenPlan
) -> tuple[TaskContract, HiddenPlan, list[str]]:
    """unit 이 분·시간인 설정을 초 단위로 — 기본값에 60·3600 을 곱하고, 키 끝 마디가 그 단위 낱말이면 seconds 로 바꿔 계약·숨은 계획 글의
    옛 키도 바꾼다. 테스트는 설정을 기본값과 다른 작은 값(도우미 SETTING_…_PROBE)으로 덮고 결과가 따르는지 보는데, 시간 설정이면 덮은
    값만큼 기다린다 — 분 단위면 수 분을 기다려 채점 시간 한도를 넘는다.
    2026-11-24 plan_contract v32 첫 평가(세션 유지 과제): '초 단위 키' 지시에도 2회 중 1회가 app.session.ttl-minutes=30 으로 적었고,
    재요청으로 물으니 명세의 'TTL(30분)'을 따라 다시 분으로 답해 계약이 실패했다 — 그래서 묻지 않고 코드가 맞춘다.
    초로 바꾼 기본값이 상한을 넘거나 바꾼 키가 이미 있으면 그대로 두고 _setting_units 가 재요청한다."""
    taken = {st.key for st in contract.settings}
    renames: dict[str, str] = {}
    settings: list[AppSetting] = []
    notes: list[str] = []
    for st in contract.settings:
        factor = _TO_SECONDS.get(st.unit)
        key = _seconds_key(st.key, st.unit)
        if (
            factor is None
            or st.default * factor > SETTING_DEFAULT_MAX
            or (key != st.key and key in taken)
        ):
            settings.append(st)
            continue
        default = st.default * factor
        settings.append(st.model_copy(update={"key": key, "default": default, "unit": "seconds"}))
        if key != st.key:
            renames[st.key] = key
        notes.append(
            f"설정 {st.key} (기본 {st.default} {st.unit}) 를 초 단위로: {key} = {default} — 테스트가 설정을 작은 값으로 덮고 그만큼 기다린다"
        )
    if not notes:
        return contract, plan, []

    def renamed(text: str) -> str:
        for old, new in renames.items():
            text = re.sub(r"(?<![\w.-])" + re.escape(old) + r"(?![\w-]|\.\w)", new, text)
        return text

    body = contract.model_dump(mode="json", exclude={"settings"})
    contract = TaskContract.model_validate(
        {
            **json.loads(renamed(json.dumps(body, ensure_ascii=False))),
            "settings": [x.model_dump(mode="json") for x in settings],
        }
    )
    plan = HiddenPlan.model_validate_json(renamed(plan.model_dump_json()))
    return contract, plan, notes


def _setting_units(contract: TaskContract) -> list[str]:
    """fit_setting_units 가 초로 바꾸지 못한 분·시간 단위 설정 → 재요청 (초로 바꾼 기본값이 상한을 넘음 등)."""
    return [
        f"설정 {st.key} 가 {st.unit} 단위인데 초로 바꾸면 기본값이 {SETTING_DEFAULT_MAX} 을 넘음 — 시간 설정은 초 단위(unit seconds)로 "
        "적는다. 테스트가 이 설정을 작은 값으로 덮고 그만큼 기다린다 — 더 긴 기간은 기다려 확인할 수 없으니 설정으로 두지 않는다"
        for st in contract.settings
        if st.unit in _TO_SECONDS
    ]


def _naming(
    contract: TaskContract, manifest: TemplateManifest, spec: SpecView | None = None
) -> list[str]:
    problems: list[str] = []
    if contract.interface_kind not in manifest.interfaces:
        problems.append(
            f"인터페이스 {contract.interface_kind} 를 템플릿 {manifest.ref} 이 지원하지 않음 "
            f"(지원: {', '.join(manifest.interfaces)})"
        )
    if contract.settings and not supports_settings(manifest):  # 설정 덮어쓰기 — 2026-11-13 #161
        problems.append(
            f"settings 는 스프링 템플릿에서만 쓴다 (템플릿 {manifest.ref}) — 설정 대신 명령 옵션·상수로 적는다"
        )
    rx = manifest.naming
    for e in contract.endpoints:
        if contract.interface_kind in ("http", "ui"):  # ui 의 엔드포인트는 화면이 부르는 서버 API
            if e.method is None:
                problems.append(f"엔드포인트 {e.endpoint_id} 에 HTTP 메서드가 없음")
            if not re.fullmatch(rx.path, e.path):
                problems.append(
                    f"엔드포인트 {e.endpoint_id} 경로 {e.path!r} 가 템플릿 경로 규칙과 다름"
                )
        elif e.method is not None:
            problems.append(
                f"엔드포인트 {e.endpoint_id} 는 {contract.interface_kind} 라 HTTP 메서드를 쓰지 않음"
            )
    for sc in contract.screens:
        if not re.fullmatch(rx.path, sc.route):
            problems.append(f"화면 {sc.screen_id} 경로 {sc.route!r} 가 템플릿 경로 규칙과 다름")
        bad_ids = [el.test_id for el in sc.elements if not re.fullmatch(rx.test_id, el.test_id)]
        if bad_ids:
            problems.append(
                f"화면 {sc.screen_id} 의 test id 가 템플릿 규칙과 다름: {', '.join(bad_ids)}"
            )
    for m in contract.entities:
        if not re.fullmatch(rx.entity, m.name):
            problems.append(f"엔티티 이름 {m.name!r} 가 템플릿 이름 규칙과 다름")
    headers = {id(f) for e in contract.endpoints for f in e.headers}
    # API 요청·응답·쿼리 필드는 명세가 정한 이름이면 인정 (엔티티 필드는 규칙대로 — DB 열 이름은 snake 변환으로 맞는다)
    api = {
        id(f)
        for e in contract.endpoints
        for f in [*e.query, *e.request, *(x for r in e.responses for x in r.body)]
    }
    named = spec_named_fields(spec)
    bad_fields = sorted(
        {
            f.name
            for _, f in _fields(contract)
            if id(f) not in headers
            and not re.fullmatch(rx.field, f.name)
            and not (id(f) in api and f.name in named)
        }
    )
    if bad_fields:
        fixes = [f"{n} → {sug}" if (sug := suggest_field(n, rx.field)) else n for n in bad_fields]
        problems.append(
            f"필드 이름이 템플릿 이름 규칙과 다름 (필드는 {field_style(rx.field)}): {', '.join(fixes)} — 그 이름으로 바꿔 적는다"
        )
    bad_headers = sorted(
        {
            f.name
            for e in contract.endpoints
            for f in e.headers
            if not re.fullmatch(rx.header, f.name)
        }
    )
    if bad_headers:
        problems.append(f"헤더 이름이 템플릿 규칙과 다름: {', '.join(bad_headers)}")
    return problems


def hidden_terms(seed: FeatureSeed | None, spec: SpecView, extra: Sequence[str] = ()) -> list[str]:
    """계약에 나오면 안 되는 말 — 재료의 숨은 요구·원인·운영 조건 낱말 + 먼저 말할 개념. 공개된 업무 요구에 나온 말은 뺀다.
    extra: 검사에만 더하는 말 — 재료의 원인 기술 용어(PublicIncident.hidden_terms). 명세 단계는 보는데 빌드 단계는 안 봐 'idempotency'
    같은 원인 용어가 계약·시작 코드·README 로 나갈 수 있었다 (설계 검사 #332 T-F7). 회사 사례에서는 LLM 입력에 싣지 않는 칸
    (company_incidents.OTHER_FIELDS)이라 계약 생성 입력(forbidden_text)에는 넣지 않는다."""
    terms = [*spec.concepts, *extra]
    if seed is not None:
        terms += [*seed.leak_terms, *seed.stage2_cause_terms, *seed.trigger_terms]
        public = set(find_terms(terms, seed.business_need))
        terms = [t for t in terms if t not in public]
    return list(dict.fromkeys(terms))


def _contract_hygiene(contract: TaskContract, avoid_terms: Sequence[str]) -> list[str]:
    """계약 글의 비밀 값·외부 주소·원 사례 고유어 (starter/hygiene — 생성기 산출물과 같은 규칙). 시드 SQL·계약 도우미·공개 테스트 값처럼
    코드가 계약에서 쓰는 파일은 생성기 몫 정적 검사에서 빠지므로, 그 글의 출처인 계약에서 보고 계약 재요청으로 고친다 (설계 검사 #332 T-F6:
    생성기가 고칠 수 없는 재생성이 되었다)."""
    out: list[str] = []
    for where, text in contract_places(contract):
        hits = hygiene_hits(text, avoid_terms)
        if hits.secret:
            out.append(
                f"공개 계약 {where} 에 비밀 값처럼 보이는 문자열이 있음 — 시험용 값으로 바꾼다"
            )
        if hits.hosts:
            out.append(
                f"공개 계약 {where} 에 외부 네트워크 주소: {', '.join(hits.hosts)} — 검증·채점은 네트워크 없이 돈다 "
                "(example.com·localhost 같은 주소로)"
            )
        if hits.named:
            out.append(
                f"공개 계약 {where} 에 원 사례의 실존 이름·고유어: {', '.join(hits.named)} — 가상 이름으로"
            )
    return list(dict.fromkeys(out))


def _leaks(
    contract: TaskContract, spec: SpecView, seed: FeatureSeed | None, extra: Sequence[str] = ()
) -> list[str]:
    places = contract_places(contract)
    shown = find_terms(hidden_terms(seed, spec, extra), "\n".join(t for _, t in places))
    if not shown:
        return []
    where = []
    for term in shown:
        hits = [(w, t) for w, t in places if find_terms([term], t)]
        at = "; ".join(f'{w}: "{_quote(t, term)}"' for w, t in hits[:2])
        where.append(f"{term} ({at})" if at else term)
    return [
        "숨은 요구·운영 조건을 드러내는 말이 공개 계약(경로·필드·설명·시드·공개 테스트)에 나옴: "
        + ", ".join(where)
        + " — 그 자리에서 이 말을 빼거나 업무 말로 바꾼다. 계약은 '무엇을'만, 숨은 요구와 운영 조건은 담지 않는다"
    ]


def _reasons(
    plan: HiddenPlan, manifest: TemplateManifest, seed: FeatureSeed | None = None
) -> list[str]:
    codes = set(manifest.reason_codes)
    if (
        seed is not None and seed.reasons
    ):  # 재료가 선언한 이 숨은 요구의 사유만 (FeatureSeed.reasons)
        codes &= set(seed.reasons)
    bad = sorted({c.intended_reason for c in plan.cases if c.intended_reason not in codes})
    if not bad:
        return []
    return [
        f"숨은 케이스의 의도한 실패 사유가 템플릿 코드 목록(재료가 정했으면 그 숨은 요구의 사유)에 없음: {', '.join(bad)} "
        f"(쓸 수 있는 것: {', '.join(sorted(codes)) or '없음'})"
    ]


# 글에 나온 'METHOD /path' — 백틱으로 감싸도, 경로 변수는 {id}·:id 둘 다, 끝 문장 부호는 빼고 (`POST /x.` → /x).
# 계약 검사·화면 폼 대조가 같은 파서를 쓴다 (#179: 두 정규식이 같은 글을 다르게 잘랐다)
_API = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/(?:[A-Za-z0-9_\-./:{}]*[A-Za-z0-9_\-/}])?)")


def api_mentions(text: str) -> list[tuple[str, str]]:
    """글의 (메서드, 경로) 들."""
    return _API.findall(text)


def _api_key(method: str, path: str) -> str:
    """같은 API 인지 견줄 키 — 경로 변수({id}·:id)는 {} 로."""
    shape = re.sub(r"\{[^}]*\}|:[A-Za-z_]\w*", "{}", path.rstrip("/") or "/")
    return f"{method.upper()} {shape}"


def mentioned_apis(texts: Iterable[str]) -> list[str]:
    """글에 나온 'METHOD /path' (경로 변수는 {} 로) — 예: 'PATCH /deliveries/{}/status'."""
    return list(dict.fromkeys(_api_key(m, p) for t in texts for m, p in api_mentions(t)))


# 새로 만드는 API 라는 말 — 'API 를 추가한다'·'엔드포인트를 구현한다'·'새로'·'신규'
_ADDS = re.compile(r"추가|새로|신규|구현한다|구현해|구현하고|만든다|만들어")
_SENT = re.compile(r"[^.。\n]+")


def added_apis(texts: Iterable[str]) -> set[str]:
    """새로 만든다고 말한 문장에 나온 'METHOD /path' — 그 문장은 그 API 가 이미 있다는 근거가 아니다."""
    return {
        k
        for t in texts
        for sent in _SENT.findall(t)
        if _ADDS.search(sent)
        for k in mentioned_apis([sent])
    }


def existing_endpoints(contract: TaskContract, spec: SpecView) -> list[Endpoint]:
    """이미 있는 API (시작 코드가 동작하게 만든다) = 계약이 `existing` 으로 표시한 것 + 명세 공개 글에 `METHOD /path` 로 나온 계약 API.
    시작 코드 생성 입력(generate.existing_apis)과 시작 코드 정적 검사(static_check._starter_new_api_tests)가 같은 판정을 쓴다.
    '…엔드포인트를 구현한다'처럼 새로 만든다고 말한 문장의 언급은 빼고 센다 (2026-11-15 손 시범 retry: 'POST /api/v1/charges 엔드포인트를 구현한다')."""
    named = set(mentioned_apis(spec.public_texts)) - added_apis(spec.public_texts)
    out: list[Endpoint] = []
    for e in contract.endpoints:
        key = mentioned_apis([f"{e.method} {e.path}"]) if e.method else []
        if e.existing or (key and key[0] in named):
            out.append(e)
    return out


def _added_marked_existing(contract: TaskContract, spec: SpecView) -> list[str]:
    """요구가 새로 추가한다는 API 를 계약이 existing(이미 있음)으로 적음 — 시작 코드가 그 API 를 구현해 지원자 몫이 줄어든다.
    2026-11-15 손 시범 coupons: r1 '…차감 후 남은 수량을 응답하는 API를 추가한다' 인데 POST /tools/{toolId}/deduct 가 existing — 시작 코드가
    차감·수량 부족 거절을 다 구현했고, 공개 테스트 4개 중 1개만 통과해 수리 ②' 의 '모두 통과' 기준도 비켜 갔다."""
    added = added_apis(spec.public_texts)
    out = []
    for e in contract.endpoints:
        if not e.existing:
            continue
        key = mentioned_apis([f"{e.method} {e.path}"]) if e.method else []
        # 그 요구를 함께 가리키는 새 API 가 있으면 추가되는 것은 그쪽이다 — 2026-11-15 N+1: r1 '목록 GET 엔드포인트를 추가한다' 를
        # 새 GET /api/posts 와 이미 있는 GET /api/posts/{id} 가 함께 가리켰다 (단건 조회는 정말 이미 있는 API)
        why = [
            r
            for r in e.refs
            if (t := spec.req_texts.get(r))
            and re.search(r"(?:API|엔드포인트|기능)[^.。\n]{0,10}(?:추가|새로|신규|구현)", t)
            and not any(not o.existing and r in o.refs for o in contract.endpoints)
        ]
        if (key and key[0] in added) or why:
            out.append(
                f"API {e.method} {e.path} 를 existing(이미 있는 API)으로 적었지만 "
                + (
                    f"요구 {', '.join(why)} 가 새로 만든다고 한다"
                    if why
                    else "명세가 새로 만든다고 한다"
                )
                + " — existing: false 로 둔다 (시작 코드는 새 API 를 자리 표시로 둔다)"
            )
    return out


def _contract_api_rx(contract: TaskContract) -> list[tuple[str, re.Pattern[str]]]:
    """계약 API → (메서드, 경로 정규식). 경로 변수 자리는 아무 한 마디 — 글에 나온 `/deliveries/15/status` 도 맞는다."""
    out: list[tuple[str, re.Pattern[str]]] = []
    for e in contract.endpoints:
        if e.method:
            path = re.escape(e.path.rstrip("/") or "/")
            out.append(
                (
                    e.method.upper(),
                    re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", path) + "/?$"),
                )
            )
    return out


def _in_contract(api: str, rx: list[tuple[str, re.Pattern[str]]]) -> bool:
    method, _, path = api.partition(" ")
    return any(method == m and pat.match(path) for m, pat in rx)


def _api_refs(contract: TaskContract, plan: HiddenPlan, spec: SpecView) -> list[str]:
    """명세·공개 테스트·숨은 케이스에 나온 API 는 계약에 있어야 한다 — 시작 코드·숨은 테스트가 그 계약으로 요청한다.
    글의 구체 id(`/deliveries/15/status`)는 계약의 경로 변수 자리와 맞는다 (2026-11-07 v3 스모크에서 오탐)."""
    rx = _contract_api_rx(contract)
    problems: list[str] = []
    missing = [a for a in mentioned_apis(spec.public_texts) if not _in_contract(a, rx)]
    if missing:
        problems.append(
            f"명세(상황·요구·수용 기준)에 나온 API 가 계약에 없음: {', '.join(missing)} — "
            "기존 API 도 요청 헤더·본문·응답·오류까지 계약에 넣는다"
        )
    # 어디서 무엇을 썼는지 글 그대로 — 2026-11-09 v17 프론트: 'GET /drafts/{}' 만 보여 주고 고칠 방법이 없어 재요청 2회에도 같은 답
    sources = [(t.test_id, t.checks) for t in contract.public_tests]
    sources += [(c.case_id, f"{c.scenario} {c.expect}") for c in plan.cases]
    stray: dict[str, list[str]] = {}
    for where, text in sources:
        for m, p in api_mentions(text):
            if not _in_contract(_api_key(m, p), rx):
                stray.setdefault(f"{m.upper()} {p}", []).append(where)
    if stray:
        have = ", ".join(f"{e.method} {e.path}" for e in contract.endpoints if e.method)
        # 화면 과제의 테스트는 서버를 직접 부르지 않고 목 서버 상태를 읽는다 — 'GET … 응답 확인' 이라 쓰면 모델이 조회 API 를 짓는다.
        # 2026-11-09 v19-fe: pt4 'GET /drafts/{draftId} 응답에 최신 내용' 이 위 문구로 재요청 2번에도 그대로 남았다
        fix = (
            " — 화면 과제의 테스트는 API 를 부르지 않고 서버 상태로 확인한다: 'GET …' 대신 서버에 무엇이 있는지로 쓴다 "
            "(예: '서버의 초안 목록에 최신 내용이 있음'). 화면이 실제로 그 API 를 부를 때만 endpoints 에 더한다"
            if contract.interface_kind == "ui"
            else f" — 계약에 있는 API({have})로 확인하거나, 꼭 필요하면 그 API 를 endpoints 에 더한다"
        )
        problems.append(
            "공개 테스트·숨은 케이스가 계약에 없는 API 를 씀: "
            + "; ".join(f"{api} ({', '.join(dict.fromkeys(ws))})" for api, ws in stray.items())
            + fix
        )
    entity_ids = {m.entity_id for m in contract.data_shapes()}
    bad = sorted(
        {
            f"{e.endpoint_id} {r.status} → {r.returns}"
            for e in contract.endpoints
            for r in e.responses
            if r.returns and r.returns.removesuffix("[]") not in entity_ids
        }
    )
    if bad:
        problems.append(f"응답 returns 가 계약에 없는 엔티티·응답 모양을 가리킴: {', '.join(bad)}")
    problems += _shape_problems(contract)
    return problems


def _shape_problems(contract: TaskContract) -> list[str]:
    """응답 모양(response_shapes) — 테이블 엔티티와 이름이 겹치지 않고, 어느 응답이든 가리킨다 (쓰지 않는 모양은 지원자를 헷갈리게 한다)."""
    out: list[str] = []
    tables = {m.name.lower() for m in contract.entities}
    clash = [s.name for s in contract.response_shapes if s.name.lower() in tables]
    if clash:
        out.append(
            f"응답 모양 이름이 테이블 엔티티와 같음: {', '.join(clash)} — 응답 모양은 테이블이 아니다. 다른 이름(예: …Summary·…Item)으로"
        )
    used = {
        r.returns.removesuffix("[]") for e in contract.endpoints for r in e.responses if r.returns
    } | {token for _, f in _fields(contract) for token in re.findall(r"\b(v\d{1,2})\b", f.type)}
    ids = {m.entity_id for m in contract.entities}
    out += [
        f"응답 모양 {s.shape_id} {s.name} 의 row_of {s.row_of} 가 계약의 테이블 엔티티가 아님 — 한 건이 대응하는 엔티티 id(m…)나 null"
        for s in contract.response_shapes
        if s.row_of is not None and s.row_of not in ids
    ]
    unused = [f"{s.shape_id} {s.name}" for s in contract.response_shapes if s.shape_id not in used]
    if unused:
        out.append(
            f"어느 응답도 가리키지 않는 응답 모양: {', '.join(unused)} — 응답의 returns(예: v1[])로 가리키거나 지운다"
        )
    return out


def _no_seed_rows(
    contract: TaskContract, plan: HiddenPlan, manifest: TemplateManifest
) -> list[str]:
    """DB 행을 바꾸지 않는 템플릿(cli·file·ui) — 시드·행 배정 대신 입력 환경(fixtures)과 숨은 케이스 setup.
    2026-11-07 종이 단계: 데이터·ML 과제가 결과 파일·평가 기록을 시드 행으로 억지로 적고 코드가 행 수를 부풀렸다."""
    out: list[str] = []
    if contract.seed:
        out.append(
            f"템플릿 {manifest.ref} 은 DB 행 시드를 쓰지 않는다 — seed 를 비우고, 시작 코드가 깔아 줄 입력 환경은 fixtures 에 적는다"
        )
    with_data = [c.case_id for c in plan.cases if c.data]
    if with_data:
        out.append(
            f"숨은 케이스 {', '.join(with_data)} 의 data 를 비운다 — 이 템플릿은 행 배정이 없다. "
            "케이스에서 하네스가 입력 환경을 바꾸는 단계는 setup 에 적는다"
        )
    return out


_CHANGES = {"POST", "PUT", "PATCH", "DELETE"}


def _target_rows(contract: TaskContract, plan: HiddenPlan) -> list[str]:
    """경로 변수({id})로 기존 대상을 바꾸는 API 가 있으면 그 대상의 시드 행과 숨은 케이스의 data 가 있어야 한다.

    2026-11-07 알파 테스트 (새 공고): covers 재요청에 LLM 이 대상 엔티티(거래)를 잇지 않고 지워, 계약에 감사 기록 엔티티(0행)만
    남았다. 시작 코드는 기존 API 를 TODO 로 비웠고, 숨은 케이스는 없는 id 로만 요청해 모든 응답이 404 — 흔한 구현도 통과했다.
    """
    apis = [
        f"{e.method} {e.path}"
        for e in contract.endpoints
        if (e.method or "").upper() in _CHANGES and "{" in e.path
    ]
    if not apis:
        return []
    out: list[str] = []
    shown = ", ".join(apis)
    if sum(s.rows for s in contract.seed) == 0:
        out.append(
            f"시드에 행이 하나도 없는데 경로 변수로 기존 대상을 바꾸는 API 가 있음: {shown} — "
            "그 API 가 다루는 대상 엔티티(상태를 가진 것)를 entities·seed 에 두고 covers 로 그 API 를 쓰는 요구에 잇는다. "
            "없는 id 로만 요청하면 모든 응답이 404 라 숨은 케이스가 구현을 가르지 못한다"
        )
    empty = [c.case_id for c in plan.cases if not c.data and not c.setup]
    if empty:
        out.append(
            f"숨은 케이스 {', '.join(empty)} 가 바꿀 행(data)을 선언하지 않음 — {shown} 처럼 기존 대상을 바꾸는 API 를 "
            "시험하면 그 대상 엔티티의 행을 data 로 선언한다 (id 범위는 코드가 나눈다)"
        )
    return out


def _entity_existing(contract: TaskContract, plan: HiddenPlan) -> tuple[list[str], list[str]]:
    """숨은 케이스 data 는 이미 있는 엔티티(existing)에만 — 엔티티에 표시가 하나도 없으면(옛 계약) 판단하지 않는다.
    새로 만드는 엔티티에 시드 행이 있으면 표시만 (드물게 새 참조 테이블일 수 있음)."""
    existing = {m.entity_id for m in contract.entities if m.existing}
    if not existing:
        return [], []
    names = {m.entity_id: m.name for m in contract.entities}
    problems: list[str] = []
    for c in plan.cases:
        bad = sorted({d.entity_id for d in c.data if d.entity_id not in existing})
        if bad:
            problems.append(
                f"숨은 케이스 {c.case_id} 의 data 가 새로 만드는 엔티티({', '.join(names.get(b, b) for b in bad)})에 있음 — "
                "바꿀 행은 이미 있는 엔티티(existing: true, 기존 API 가 다루는 대상)에 둔다. 새 엔티티는 요청으로 쌓이는 것이라 시드 행이 아니다"
            )
    flags = [
        f"새로 생성하는 엔티티 {names[s.entity_id]} 에 시드 {s.rows}행 — 이 과제 전에 없던 테이블이면 처음 0행이어야 한다 (사람 확인)"
        for s in contract.seed
        if s.rows > 0 and s.entity_id not in existing and s.entity_id in names
    ]
    return problems, flags


_MUTATING = ("POST", "PUT", "PATCH", "DELETE")
ROW_NEEDS = ("stateful_target", "transition_rules", "empty_record_entity", "related_entities")
_NAME_WORDS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
# 개수·합계를 뜻하는 낱말 — 다른 엔티티 행을 세거나 더한 칸 (itemCount·item_count·numItems·totalItems)
AGGREGATE_WORDS = frozenset({"count", "cnt", "num", "number", "total", "sum"})


def _name_words(name: str) -> tuple[str, ...]:
    """이름 → 소문자 낱말열 (camel·snake 모두): customerName·customer_name → (customer, name)."""
    return tuple(w.lower() for w in _NAME_WORDS.findall(name))


def _singular(words: tuple[str, ...]) -> tuple[str, ...]:
    return (*words[:-1], words[-1].removesuffix("s")) if words else words


def _copied_fields(t: Entity, r: Entity) -> list[str]:
    """t 의 칸 중 r 에서 읽어야 할 딸린 정보를 복사한 것 — 낱말열로 견준다 (#180: camel 결합·`…Count` 꼴만 봐 `customer_name`·
    `item_count`·`numItems` 는 지나갔다). `<r 이름 또는 그 끝 낱말><r 의 칸>` (customerName ← Customer.name) 이나
    `<r 이름 또는 그 끝 낱말>` 앞뒤의 개수·합계 낱말 (itemCount·numItems ← OrderItem). 외래 키 칸·id 칸은 뺀다."""
    fks = {f for f, _ in t.fk_pairs()}
    words = _name_words(r.name)
    heads = {words, words[-1:]} - {()}
    copies = {
        h + _name_words(g.name)
        for h in heads
        for g in r.fields
        if _name_words(g.name)[-1:] != ("id",)
    }
    out: list[str] = []
    for f in t.fields:
        fw = _name_words(f.name)
        if f.name in fks or not fw or fw[-1] in ("id", "ids"):
            continue
        counted = (len(fw) > 1 and fw[0] in AGGREGATE_WORDS and _singular(fw[1:]) in heads) or (
            len(fw) > 1 and fw[-1] in AGGREGATE_WORDS and _singular(fw[:-1]) in heads
        )
        if fw in copies or counted:
            out.append(f.name)
    return out


def _related_problems(contract: TaskContract, note: str) -> list[str]:
    """딸린 정보는 다른 엔티티에서 읽는다 (FeatureSeed.contract_needs related_entities) — 시드 있는 두 엔티티가 외래 키로 이어져 있고,
    딸린 정보가 시드 있는 테이블 엔티티의 칸·시드 값으로 복사돼 있지 않다. 2026-11-12 행마다 조회 알파: 계약이 고객 이름·상품 수를 주문 테이블
    칸·시드 값으로 둬, 그 칸만 읽으면 목록 한 번이 쿼리 하나로 끝나 숨은 요구가 사라졌다. 응답에 그 칸이 필요하면 응답 모양(response_shapes)에 둔다."""
    by_name = {m.name: m for m in contract.entities}
    rows = {s.entity_id: s.rows for s in contract.seed}
    pairs: list[tuple[Entity, Entity]] = []
    for m in contract.entities:
        for _, target in m.fk_pairs():
            r = by_name.get(target)
            if (
                r is not None
                and r is not m
                and rows.get(m.entity_id, 0)
                and rows.get(r.entity_id, 0)
            ):
                pairs.append((m, r))
    if not pairs:
        return [
            f"숨은 요구에 필요한 것이 계약에 없음 — 딸린 정보를 읽을 다른 엔티티: {note}. 대상 엔티티와 딸린 정보 엔티티를 "
            'foreign_keys 로 잇고(예: "customerId → Customer") 둘 다 시드 행을 둔다'
        ]
    copied = sorted(
        {
            f"{t.name}.{f}"
            for m, r in pairs
            for t, s in ((m, r), (r, m))
            for f in _copied_fields(t, s)
        }
    )
    if not copied:
        return []
    return [
        f"딸린 정보가 시드 있는 테이블 엔티티의 칸으로 복사돼 있음: {', '.join(copied)} — {note}. 테이블 칸·시드 값으로 두면 목록이 다른 "
        "엔티티를 읽을 필요가 없어진다. 그 칸은 테이블 엔티티에서 빼고, 응답에 필요한 모양은 response_shapes(예: v1 OrderSummary — 칸 제약에 "
        "출처)로 두어 목록·한 건 조회의 returns(v1[]·v1)로 가리킨다"
    ]


def _forbidden_storage(contract: TaskContract, forbidden: Sequence[ForbiddenStorage]) -> list[str]:
    """함정 틀 검사 4 (#318) — 계약 엔티티가 틀이 금지한 저장 방식이면 재요청 (빌드 안전망 — 다른 틀 검사는 표시만).
    새 엔티티만 보면 b3 의 Session(existing=true)을 놓쳐 새·기존 모두 본다. 낱말 대조(_leaks)로는 못 한다 — 식별자를 낱말로 풀어
    sessionId 같은 공개 칸까지 걸리고, 업무 요구에 나온 말('세션')은 숨은 낱말에서 빠진다. 그래서 엔티티 이름 단위로 — 시작 코드 정적
    검사와 같은 규칙(starter/forbidden_storage.names_entity: 끝 낱말들이 같으면, 단·복수 같게 — UserSession·LoginSession 도).
    틀의 why(LLM 글)는 싣지 않는다 — 허용 설계(해법)가 들어 있을 수 있고 계약은 지원자가 받는다 (설계 검사 B-F10)."""
    out: list[str] = []
    for m in contract.entities:
        if forbidden_for(m.name, forbidden) is not None:
            out.append(
                f"엔티티 {m.name}({m.entity_id}) 는 공개 계약에 두지 않는다 — 함정 틀: 흔한 구현의 저장 방식이라 숨은 요구를 드러낸다. "
                "entities·seed·공개 테스트에서 빼고, 그 상태를 어떻게 둘지는 구현 몫으로 남긴다"
            )
    return out


def _needs(
    contract: TaskContract, plan: HiddenPlan, seed: FeatureSeed | None, row_seeds: bool = True
) -> list[str]:
    """재료가 선언한 '숨은 요구에 계약이 꼭 가져야 할 것'(FeatureSeed.contract_needs)을 기계적으로 대조 — 빠진 것을 구체적으로 알려 다시 요청.
    2026-11-07~08 알파 평가: 대상 엔티티 없음(감사 기록에 시드), 상태 전이 규칙 없음(숨은 테스트·시작 코드가 다른 전이를 짐작)."""
    if seed is None or not seed.contract_needs:
        return []
    # 행 기반 종류(대상 엔티티·전이 규칙·기록 엔티티)는 행 시드 템플릿에서만, 프론트 종류(바꾸는 API·결과 화면)는 모든 템플릿에서
    need = {n.kind: n.note for n in seed.contract_needs if row_seeds or n.kind not in ROW_NEEDS}
    rows = {s.entity_id: s.rows for s in contract.seed}
    flagged = any(m.existing for m in contract.entities)
    in_cases = {d.entity_id for c in plan.cases for d in c.data}
    targets = [
        m
        for m in contract.entities
        if rows.get(m.entity_id, 0) > 0 and (m.existing or not flagged) and m.entity_id in in_cases
    ]
    out: list[str] = []
    if "stateful_target" in need and not targets:
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 상태를 가진 대상 엔티티: {need['stateful_target']}. "
            "entities 에 existing: true 로 두고, seed 에 행을 두고, 숨은 케이스 data 를 그 엔티티에 둔다"
        )
    # 칸(state)으로 — 화살표 글만 적은 답(plan_contract v9 이하)은 칸으로 옮길 수 있으면 있는 것으로 본다 (fit_state 가 옮긴다)
    if "transition_rules" in need and not any(m.state or legacy_rule(contract, m) for m in targets):
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 상태 전이 규칙: {need['transition_rules']}. 대상 엔티티의 state 칸에 "
            "상태 값 전부(values)·허용 전이(transitions, 'A→B' 꼴로 빠짐없이)·거절 코드(rejected_status)를 적는다 "
            "(숨은 테스트와 시작 코드가 같은 규칙을 따르게)"
        )
    mutating = [e for e in contract.endpoints if (e.method or "").upper() in _MUTATING]
    if "mutation_endpoint" in need and not mutating:
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 화면이 부르는 바꾸는 API: {need['mutation_endpoint']}. "
            "endpoints 에 POST·PUT·PATCH·DELETE 중 하나로 (요청 본문·성공 응답 본문 포함) 적는다"
        )
    if "dated_input_folders" in need:
        out += _dated_folder_problems(contract, need["dated_input_folders"])
    if "result_screen" in need and not any(sc.elements for sc in contract.screens):
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 결과를 보여 주는 화면 요소: {need['result_screen']}. "
            "screens 에 그 값을 보여 주는 요소(test id·역할·설명)를 적는다"
        )
    if "related_entities" in need:
        out += _related_problems(contract, need["related_entities"])
    if "empty_record_entity" in need and not [
        m for m in contract.entities if rows.get(m.entity_id, 0) == 0 and not m.existing
    ]:
        out.append(
            f"숨은 요구에 필요한 것이 계약에 없음 — 요청마다 쌓이는 기록 엔티티: {need['empty_record_entity']}. "
            "entities 에 existing: false 로 두고 seed 는 rows 0"
        )
    return out


def _internal_expect(plan: HiddenPlan, manifest: TemplateManifest) -> list[str]:
    """숨은 케이스 통과 조건(expect)이 조절값 이름을 말하면 재요청 — 조절값 이름(식별자)을 정확히 맞추는 판정이라 오탐이 드물다.
    2026-11-07 알파 5회차: "모든 요청이 pool_size(30) 내에서 연결을 점유" 처럼 HTTP 로 관찰할 수 없는 조건이라, 숨은 테스트가 단언으로
    옮기다 '시간 초과가 일어나야 한다'로 거꾸로 써 올바른 구현까지 떨어뜨렸다. 2026-11-08 상류 기준선: 9회 중 3회 — 표시만으로는 줄지 않아 재요청으로."""
    # 밖에서 관찰할 수 있는 한도는 매니페스트가 선언한다(hidden_rules.observable_params) — 2026-11-10 nplus1-h: '응답이
    # request_timeout_s(1초) 내에 도착' 을 세 번 다 막아 목록 재료 계약이 실패했다. 이름 끝(_ms·_s)으로 면제하던 것은 #180
    observable = set(manifest.hidden_rules.observable_params)
    names = [p.name for p in manifest.harness_params if p.name not in observable]
    out: list[str] = []
    for c in plan.cases:
        hit = [
            n
            for n in names
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", c.expect)
        ]
        if hit:
            out.append(
                f"숨은 케이스 {c.case_id} 의 통과 조건(expect)이 조절값({', '.join(hit)})을 말함: {c.expect[:120]} — "
                "통과 조건은 HTTP 로 관찰할 수 있는 결과로만 적는다 (예: '모든 요청이 기대 상태 코드 — 시간 초과·500 없음', "
                "'요청마다 결과와 맞는 기록 1건', '롤백된 요청에 SUCCESS 기록 없음'). 연결 수·풀 크기 같은 내부 동작은 쓰지 않는다 — "
                "조절값은 harness 에만"
            )
    return out


def _foreign_keys(contract: TaskContract) -> list[str]:
    """foreign_keys 의 필드·대상 엔티티가 계약에 있다."""
    names = {m.name for m in contract.entities}
    out: list[str] = []
    for m in contract.entities:
        fields = {f.name for f in m.fields}
        for fk_field, target in m.fk_pairs():
            if fk_field not in fields:
                out.append(f"외래 키 {m.name}.{fk_field} → {target}: {m.name} 에 없는 필드")
            if target not in names:
                # 2026-11-10 프론트 알파 fe7b-offline: 고치는 법 없이 '없는 엔티티'만 알려 3번 모두 같은 외래 키를 걸었다
                out.append(
                    f"외래 키 {m.name}.{fk_field} → {target}: 계약에 없는 엔티티 — {target} 를 entities 에 넣거나(이미 있는 데이터면 existing), "
                    f"과제가 {target} 를 다루지 않으면 외래 키를 빼고 {fk_field} 를 그냥 칸(식별 값)으로 둔다"
                )
    return out


INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "input",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "link",
        "tab",
        "menuitem",
    }
)


_FORMATS = ("jsonl", "csv", "parquet")


def command_options(contract: TaskContract) -> dict[str, ContractField]:
    """명령(메서드 없는 엔드포인트)의 옵션 — 이름 → 칸 (요청·쿼리)."""
    return {f.name: f for e in contract.endpoints if not e.method for f in [*e.request, *e.query]}


_PATH_SLOT = re.compile(r"\{([^{}/]*)\}")


def path_slots(path: str) -> list[str]:
    """위치 패턴의 자리표시자 이름 (`{raw_dir}/{run_date}/*.csv` → raw_dir, run_date)."""
    return _PATH_SLOT.findall(path)


def _dated_folder_problems(contract: TaskContract, note: str) -> list[str]:
    """받은 날짜별 입력 폴더 — 파일 입력 환경의 위치 패턴(Fixture.path)에 마디 하나가 통째로 날짜 형식 명령 옵션(`{run_date}`)인 것.
    #187 woori001: 위치가 자유 글뿐이라 `YYYY-MM-DD/` 표기만 받던 검사가 `{YYYY-MM-DD}/` 로 바르게 적은 계약을 재요청했고, 실행 날짜 옵션도
    이름의 'date' 글자로 짐작했다 — 둘 다 칸으로 (마디 = 옵션, 옵션 형식 = date)."""
    options = command_options(contract)
    dates = {
        n for n, f in options.items() if non_null(parse_field(f.type)).kind in ("date", "datetime")
    }
    segments = [
        seg
        for fx in contract.fixtures
        if fx.kind == "file" and fx.path
        for seg in fx.path.split("/")
    ]
    if any(seg in {f"{{{d}}}" for d in dates} for seg in segments):
        return []
    untyped = sorted(
        {s[1:-1] for s in segments if s.startswith("{") and s.endswith("}") and s[1:-1] in options}
        - dates
    )
    hint = (
        f" (경로 마디로 쓴 옵션 {', '.join(untyped)} 의 type 이 날짜가 아님 — 실행 날짜면 type 을 date 로)"
        if untyped
        else ""
    )
    return [
        f"숨은 요구에 필요한 것이 계약에 없음 — 날짜별 입력 폴더: {note}. 입력 파일 환경(fixtures kind file)의 path 에 받은 날짜 폴더 마디를 "
        f"실행 날짜 옵션으로 적고(예: {{raw_dir}}/{{run_date}}/*.jsonl), 그 옵션은 명령의 type date 칸으로 둔다{hint}"
    ]


def _file_fixtures(contract: TaskContract) -> list[str]:
    """파일을 다루는 템플릿(interfaces 에 file): 파일 fixture 마다 형식 하나·행 모양 엔티티 이름. 2026-11-09 데이터 알파:
    입력 형식이 'csv 또는 parquet', 입력 레코드 필드가 엔티티 없이 글로만('결제번호·매장ID 등') — 테스트와 구현이 필드 이름을 서로 짐작해 모든 구현이 실패."""
    out: list[str] = []
    names = [m.name for m in contract.entities]
    options = command_options(contract)
    for fx in contract.fixtures:
        if fx.kind != "file":
            continue
        if not fx.path:
            out.append(
                f"파일 {fx.fixture_id} 에 위치(path)가 없음 — 명령 옵션을 {{옵션 이름}} 으로 쓴 위치 패턴을 적는다 (예: {{raw_dir}}/{{run_date}}/*.jsonl)"
            )
        else:
            unknown = [s for s in path_slots(fx.path) if s not in options]
            if unknown:
                out.append(
                    f"파일 {fx.fixture_id} 의 path {fx.path} 자리표시자 {', '.join(unknown)} 가 명령 옵션이 아님 — {{…}} 에는 명령 옵션 이름만 "
                    f"(옵션: {', '.join(options) or '없음'})"
                )
        text = f"{fx.summary} {fx.content}"
        formats = {f for f in _FORMATS if re.search(rf"\b{f}\b", text.lower())}
        if len(formats) > 1:
            out.append(
                f"파일 {fx.fixture_id} 의 형식이 하나로 정해지지 않음 ({'·'.join(sorted(formats))}) — jsonl·csv·parquet 중 하나만 적는다"
            )
        if not any(re.search(rf"\b{re.escape(n)}\b", text) for n in names):
            out.append(
                f"파일 {fx.fixture_id} 에 행 모양 엔티티가 없음 — 그 파일의 한 줄(한 행)이 어떤 엔티티인지 이름을 적고 (예: '한 줄에 Payment 하나'), "
                "그 엔티티를 entities 에 필드로 둔다 (입력 레코드와 출력 행은 따로)"
            )
    return out


def _harness_scope(contract: TaskContract, manifest: TemplateManifest) -> list[str]:
    """채점 하네스가 시험할 수 없는 계약 요소 (manifest harness_scope) — 깔 수 없는 종류의 입력 환경, 파일 위치 옵션이 없는 명령.
    #256 (2026-11-18 데이터 알파 woori001): 명세의 '스케줄에 등록' 요구로 계약이 `pipeline/setup_schedule.py --schedule-time` 과
    kind env 입력 환경(crontab 줄)을 지었고, 채점 컨테이너에 crontab 이 없어 허용·흔한 구현 4개가 모두 그 공개 테스트에서 떨어졌다
    — 수리는 '테스트 의심'으로 공개 테스트만 다시 만들다 예산을 썼다. 안내 글(hint)은 템플릿 지식이라 매니페스트에 둔다."""
    scope = manifest.harness_scope
    if scope is None:
        return []
    out = [
        f"입력 환경 {fx.fixture_id} 의 kind {fx.kind} 는 채점 하네스가 깔 수 없음 (깔 수 있는 것: {', '.join(scope.fixture_kinds)}) "
        "— 이 입력 환경을 빼고, 그것으로 담던 요구는 깔 수 있는 입력 환경·명령 옵션으로 담는다"
        for fx in contract.fixtures
        if fx.kind not in scope.fixture_kinds
    ]
    files = [fx for fx in contract.fixtures if fx.kind == "file"]
    options = command_options(contract)
    # 파일 위치가 모두 정해지고 자리표시자가 모두 명령 옵션일 때만 명령을 판정한다 — 위치가 없거나 틀리면 _file_fixtures 가 먼저 묻는다
    # (11-15 이전 계약처럼 path 가 없으면 본 명령까지 '파일에 닿지 않음'으로 잘못 걸린다)
    located = (
        {slot for fx in files for slot in path_slots(fx.path or "")}
        if files and all(fx.path for fx in files)
        else set()
    )
    if scope.command_file_option and located and located <= set(options):
        for e in contract.endpoints:
            if e.method is not None:
                continue
            names = [f.name for f in [*e.request, *e.query]]
            if not located & set(names):
                out.append(
                    f"명령 {e.endpoint_id} {e.path} 의 옵션({', '.join(names) or '없음'})이 어느 파일 입력 환경의 위치(path 의 {{옵션}})에도 "
                    "쓰이지 않음 — 채점은 케이스마다 임시 작업 폴더에 입력을 쓰고 그 위치를 명령 옵션으로 넘겨 출력을 읽으므로, 파일을 읽거나 "
                    "쓰지 않는 명령은 시험할 수 없다. 파일 위치를 옵션으로 받게 하거나(입력 환경 path 에 그 {옵션}), 파일을 다루지 않는 명령이면 "
                    "빼고 그 요구를 파일을 다루는 명령으로 담는다"
                )
    if out and scope.hint:
        out[-1] += f" — {scope.hint}"
    return out


def _ui_actions(contract: TaskContract) -> list[str]:
    """조작하는 화면 요소(버튼·입력 등)에 조작 방법(action)이 없음 — 무엇을 하면 언제 서버 요청이 나가는지 계약이 정해야 테스트와 구현이
    같은 조작을 한다. 2026-11-09 React 알파(낙관적 갱신): 수량 입력의 저장 시점이 계약에 없어 테스트는 입력 즉시, 허용 구현은 포커스 이동 때 요청."""
    missing = [
        el.test_id
        for sc in contract.screens
        for el in sc.elements
        if el.role in INTERACTIVE_ROLES and not (el.action or "").strip()
    ]
    if not missing:
        return []
    return [
        f"조작하는 화면 요소에 조작 방법(action)이 없음: {', '.join(missing[:8])} — 무엇을 하면 무엇이 일어나고 서버 요청이 언제 나가는지 "
        "한 줄로 적는다 (예: '클릭하면 수량 1 증가 — 바로 저장 요청', '값을 바꾸고 포커스를 옮기면 저장 요청'). "
        "입력칸·선택도 조작하는 요소다 — 서버 요청이 없으면 그렇게 적는다 (예: '입력하면 화면 값만 바뀜 — 요청은 저장 버튼에서')"
    ]


_CHOICE_ROLES = ("listbox", "radio", "option", "tab")
_SELECT_HINT = re.compile(r"select|dropdown|선택|고른다|고름|드롭다운", re.I)
_PLACEHOLDER = re.compile(
    r"^\s*(?:-+|선택|선택하세요|선택해\s*주세요|전체|select\b.*|choose\b.*)\s*$", re.I
)


def choice_element(role: str, test_id: str, description: str) -> bool:
    """고를 수 있는 값이 정해진 요소인가 — 역할이 선택형이거나, combobox 인데 이름·설명이 선택 목록을 말함
    (검색 자동완성 입력란도 combobox 라 그것은 빼야 한다)."""
    if role in _CHOICE_ROLES:
        return True
    return role == "combobox" and bool(_SELECT_HINT.search(f"{test_id} {description}"))


def _ui_options(contract: TaskContract) -> list[str]:
    """선택형 요소에 고를 수 있는 값(options)이 없거나 안내 문구가 섞임 — 값이 글로만 있으면 테스트·시작 코드·구현이 각자 짐작한다.
    2026-11-10 프론트 알파(fefix 008d6314): 숨은 테스트가 첫 항목(안내 문구, 빈 값)을 골라 필수 입력을 검사하는 허용 구현만 떨어짐."""
    missing, placeholder = [], []
    for sc in contract.screens:
        for el in sc.elements:
            if not choice_element(el.role, el.test_id, el.description):
                continue
            if not el.options:
                missing.append(el.test_id)
            elif any(_PLACEHOLDER.match(v) for v in el.options):
                placeholder.append(el.test_id)
    out = []
    if missing:
        out.append(
            f"선택형 화면 요소에 고를 수 있는 값(options)이 없음: {', '.join(missing[:8])} — 화면에 보이는 값 그대로 적는다 "
            "(예: options: ['캘리브레이션', '센서', '액추에이터']). 설명 글에만 두지 않는다 — 테스트·시작 코드가 이 값을 그대로 쓴다"
        )
    if placeholder:
        out.append(
            f"options 에 안내 문구가 섞임: {', '.join(placeholder[:8])} — '선택하세요' 같은 빈 항목은 넣지 않고 고를 수 있는 값만"
        )
    return out


_ENUM_SQL = re.compile(r"\bENUM\s*\(([^)]*)\)", re.I)
_ENUM_BLOCK = re.compile(r"\benum\s+\w+\s*\{([^}]*)\}")
_PIPES = re.compile(r"""(?:["']?[A-Za-z][\w-]*["']?\s*\|\s*)+["']?[A-Za-z][\w-]*["']?""")
_TOKEN = re.compile(r"[A-Za-z][\w-]*")
_UPPER = re.compile(r"^[A-Z][A-Z0-9_]*$")
FLAG_MATERIAL = "계약 상태 값이 자료와 다름 (재요청 후에도 — 요구가 새 상태를 추가하는지 확인): "


def material_enums(texts: Iterable[str]) -> list[list[str]]:
    """1단계 자료가 적은 값 목록 — SQL ENUM(…), enum X { … }, A|B|C (따옴표 없는 것은 대문자 값만)."""
    out: list[list[str]] = []
    for text in texts:
        for m in _ENUM_SQL.finditer(text):
            out.append(re.findall(r"""["']([^"']+)["']""", m.group(1)))
        for m in _ENUM_BLOCK.finditer(text):
            out.append([x for x in _TOKEN.findall(m.group(1)) if _UPPER.match(x)])
        for m in _PIPES.finditer(text):
            quoted = "'" in m.group(0) or '"' in m.group(0)
            vals = [x for x in _TOKEN.findall(m.group(0)) if quoted or _UPPER.match(x)]
            out.append(vals)
    return [list(dict.fromkeys(v)) for v in out if len(set(v)) >= 2]


def material_state_problems(contract: TaskContract, spec: SpecView) -> list[str]:
    """state 칸 값이 자료의 값 목록과 대부분 겹치는데 자료 밖 값이 있음 — 기존 API 자료가 MEMBER 인데 계약이 GENERAL 처럼.
    2026-11-09 smilegate_2026#002 계약 14개 중 8개가 자료 밖 상태 값(GENERAL·NORMAL·SUB_MASTER, 값이 아닌 RANK 까지)을 지어냈다."""
    enums = material_enums(spec.visible_texts)
    out: list[str] = []
    for m in contract.entities:
        if m.state is None or not enums:
            continue
        values = set(m.state.values)
        best = max(enums, key=lambda e: len(values & set(e)))
        common = values & set(best)
        outside = sorted(values - set(best))
        if outside and len(common) >= max(2, (len(values) + 1) // 2):
            out.append(
                f"{m.name}.state values 의 {', '.join(outside)} 가 1단계 자료의 값({', '.join(best)})에 없음 — "
                "자료가 쓰는 값 그대로 쓴다 (요구가 새 상태를 더하는 것이면 그대로 둔다)"
            )
    return out


def _ui_load_and_background(contract: TaskContract) -> list[str]:
    """ui 계약: 화면마다 진입 때 요청(on_load), 화면 조작 없이 나가는 요청(background)을 정한다 — 없으면 테스트와 구현이
    서로 다르게 짐작한다. 2026-11-09 React 알파: 공개 테스트는 '목록에 들어오면 GET', 구현 4개는 로컬 저장소만 읽어 모두
    '요청이 오지 않음'. 서버 요청이 없는 화면은 '서버 요청 없음', 없는 배경 요청은 '없음' 이라고 적으면 된다."""
    if contract.interface_kind != "ui":
        return []
    out: list[str] = []
    missing = [sc.route for sc in contract.screens if not (sc.on_load or "").strip()]
    if missing:
        out.append(
            f"화면에 들어올 때 동작(on_load)이 없음: {', '.join(missing)} — 무엇을 읽고(로컬 저장소·서버) 어떤 서버 요청이 "
            "나가는지 한 줄로 (서버 요청이 없으면 '서버 요청 없음', 예: '로컬 IndexedDB 의 초안을 읽어 보여 줌 — 서버 요청 없음')"
        )
    if not (contract.background or "").strip():
        out.append(
            "화면 조작 없이 나가는 서버 요청(background)이 없음 — 동기화·재시도처럼 버튼 없이 나가는 요청과 그 시점을 한 줄로 "
            "(예: '브라우저 online 이벤트가 오면 대기열 요청을 순서대로 보냄'). 없으면 '없음'"
        )
    return out


# 항목 여러 개를 보여 주는 요소 — 처음 항목이 어디서 오는지 계약에 있어야 테스트가 그 항목을 줄 수 있다
LIST_ROLES = frozenset({"list", "item", "listbox", "option", "row", "table", "grid", "listitem"})


def _path_named(path: str, text: str) -> bool:
    """글이 이 API 경로를 말하나 — 메서드·쿼리 문자열은 없어도 되고 경로 변수({id}·:id) 자리는 아무 마디 (`/api/notifications?size=20`)."""
    parts = [
        r"[^/\s?]+" if re.fullmatch(r"\{[^}]*\}|:[A-Za-z_]\w*", seg) else re.escape(seg)
        for seg in path.rstrip("/").split("/")
    ]
    return bool(re.search(r"(?<![\w/])" + "/".join(parts) + r"(?![\w/-])", text))


def _ui_data_source(contract: TaskContract) -> list[str]:
    """ui 계약: 목록을 보여 주는 화면인데 처음 항목이 어디서 오는지 없음 — 읽는 API(GET)·만드는 API(POST)·브라우저 저장소·브라우저 입력 환경
    중 아무것도 없으면 테스트는 처음 항목을 줄 방법이 없어 하네스에 없는 인자를 지어내고(renderApp(경로, 초기 데이터)), 구현은 샘플 항목을
    코드에 박는다. 2026-11-10 프론트 알파 fe-v3-opt(plan_contract v20): 장바구니 화면 on_load '서버 요청 없음'·API 는 PATCH 하나."""
    if contract.interface_kind != "ui":
        return []
    # 화면마다 본다 (#180: 계약에 GET·POST 가 하나라도 있으면 통째로 건너뛰어, 목록과 무관한 결제 POST 만 있어도 지나갔다).
    # 화면↔API·화면↔입력 환경을 잇는 칸이 없어 두 가지로 잇는다 — 화면 글(on_load·요소 action)이 그 API 경로를 말함(메서드는 없어도),
    # 또는 화면과 같은 공개 요구를 refs 로 적음. 읽는 API(GET)만 출처다 — 만드는 API(POST)만으로는 처음 항목이 없다
    reads = [e for e in contract.endpoints if e.method == "GET"]
    browser = [f for f in contract.fixtures if f.kind == "browser"]

    def fed(sc: Screen) -> bool:
        text = " ".join([sc.on_load or "", *(el.action or "" for el in sc.elements)])
        refs = {*sc.refs, *(r for el in sc.elements for r in el.refs)}
        return (
            bool(sc.storage)
            or any(_path_named(e.path, text) or refs & set(e.refs) for e in reads)
            or any(refs & set(f.refs) for f in browser)
        )

    empty = [
        sc.route
        for sc in contract.screens
        if any(el.role in LIST_ROLES for el in sc.elements) and not fed(sc)
    ]
    if not empty:
        return []
    return [
        f"목록을 보여 주는 화면 {', '.join(empty)} 의 처음 항목이 어디서 오는지 계약에 없음 (그 화면이 부르는 읽기 API — 화면 글에 경로가 "
        "나오거나 같은 공개 요구를 refs 로 적은 GET — ·브라우저 저장소·입력 환경이 없음) — 화면에 들어올 때 목록을 읽는 서버 API(GET, 이미 있는 "
        "API 면 existing)를 endpoints 와 그 화면 on_load 에 'GET /경로' 로 적거나, 브라우저 저장소(storage)와 "
        "그 처음 내용(fixtures kind=browser)을 적는다. 테스트는 그 API 응답·저장소로만 처음 항목을 줄 수 있다"
    ]


_FIELD_ROLES = frozenset(
    {"input", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch", "listbox"}
)


# 경로는 경로 글자만 — 'POST /api/auth/login으로'·'… login.' 의 조사·마침표를 먹지 않게 (리뷰 2026-11-11)
# 사용자가 입력하지 않는 요청 칸 — 앱이 만드는 번호·토큰·시각 (폼 입력란으로 만들지 않는다). 화면·API 경로 변수도 앱이 채운다.
# 'userId' 처럼 이름이 …Id 여도 로그인 폼에선 사용자가 입력하므로 …Id 전체를 빼지 않는다 (2026-11-11 fe7d-user 재생).
_SYSTEM_FIELD = re.compile(
    r"^(?:id|token|.+Token|.+At|.+_at|(?:request|idempotency|client|trace|correlation)(?:Id|Key|_id|_key))$"
)
_PATH_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}|:([A-Za-z_][A-Za-z0-9_]*)")
_MAX_ELEMENTS = 30  # UiScreen.elements 상한


def _route_shape(path: str) -> str:
    return re.sub(r"\{[^}/]+\}|:[^/]+", ":", path.rstrip("/")) or "/"


def _form_endpoint(contract: TaskContract, form: UiElement) -> Endpoint | None:
    """폼의 조작·설명에 적힌 제출 요청(메서드·경로)과 같은 계약 API. 적혀 있지 않으면, 본문을 받는 변경 API(POST·PUT·PATCH)가
    계약에 하나뿐이고 다른 요소의 조작이 그 API 를 말하지 않을 때만 그것 (2026-11-10 fe7c-user: 조작 '제출하면 로그인 요청을 보내고…')."""
    for text in (form.action or "", form.description):
        for method, path in api_mentions(text):
            for e in contract.endpoints:
                if e.method == method and _route_shape(e.path) == _route_shape(path):
                    return e
    if api_mentions(form.action or ""):
        return None  # 다른 요청을 적었다 — 짐작하지 않는다
    writes = [e for e in contract.endpoints if e.method in ("POST", "PUT", "PATCH") and e.request]
    if len(writes) != 1:
        return None
    mentioned = any(
        (m := _API.search(el.action or "")) is not None
        and m.group(1) == writes[0].method
        and _route_shape(m.group(2)) == _route_shape(writes[0].path)
        for sc in contract.screens
        for el in sc.elements
        if el is not form
    )
    return None if mentioned else writes[0]


def _kebab(name: str) -> str:
    """칸 이름 → test id 꼬리 (camelCase → kebab). 영문·숫자가 없는 이름은 빈 문자열 — 그 칸은 입력란으로 만들지 않는다."""
    out = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower()
    return re.sub(r"[^a-z0-9]+", "-", out).strip("-")


def _input_fields(e: Endpoint, route: str) -> list[ContractField]:
    """요청 본문 칸 중 사용자가 입력하는 것 (요청 번호·토큰·시각처럼 앱이 만드는 칸, 화면·API 경로 변수, 객체·배열,
    영문 이름이 없는 칸은 뺀다)."""
    path_vars = {a or b for a, b in _PATH_VAR.findall(f"{route} {e.path}")}
    return [
        f
        for f in e.request
        if not _SYSTEM_FIELD.match(f.name)
        and f.name not in path_vars
        and _kebab(f.name)
        and non_null(parse_field(f.type)).kind not in ("list", "object")
    ]


def _field_element(form: UiElement, f: ContractField) -> UiElement:
    ft = non_null(
        parse_field(f.type)
    )  # 계약 형식 해석 한 곳 (#172 — 선택지가 따옴표째 나오고 bigint·smallint 는 글자 칸이었다)
    values = list(ft.values) if ft.kind == "enum" else []
    if values and len(values) <= 20 and all(len(v) <= 60 for v in values):
        role, options = "combobox", values
    elif ft.kind == "boolean":
        role, options = "checkbox", []
    elif ft.kind in ("integer", "number") or "password" in f.name.lower():
        role, options = "input", []  # 숫자(spinbutton)·비밀번호 입력란 — 역할이 textbox 가 아니다
    else:
        role, options = "textbox", []
    what = (
        f"{f.name} 입력란 — 폼 {form.test_id} 의 요청 칸 {f.name}{'' if f.required else ' (선택)'}"
    )
    return UiElement(
        test_id=f"{form.test_id}-{_kebab(f.name)}",
        role=role,  # type: ignore[arg-type]
        description=what[:200],
        action="값을 입력한다 — 제출하기 전에는 요청이 나가지 않는다",
        options=options,
        refs=list(form.refs),
    )


def _owned(form: UiElement, el: UiElement) -> bool:
    """화면에 폼이 여럿일 때 요소가 그 폼의 것인지 — test id 접두(`login-form-…`·`login-…`)로."""
    stem = re.sub(r"-?form$", "", form.test_id) or form.test_id
    return el is not form and (
        el.test_id.startswith(f"{form.test_id}-") or el.test_id.startswith(f"{stem}-")
    )


# 입력하는 동안 요청이 나갈 수 있는 입력란 — 요청 시점이 판정을 가르므로 조작을 짐작하지 않는다 (2026-11-09 수량 입력)
_LIVE_INPUT = re.compile(r"검색|search|필터|filter|자동|실시간|즉시|debounce|query", re.I)
_FIELD_ACTION = "값을 입력한다 — 제출하기 전에는 요청이 나가지 않는다"  # _field_element 가 만드는 입력란과 같은 문장


def fit_field_actions(contract: TaskContract) -> tuple[TaskContract, list[str]]:
    """조작 방법(action)이 빠진 글자·숫자 입력란 — 같은 화면(폼이 여럿이면 그 폼)에서 요청을 보내는 제출 대상(API)이 하나뿐이면
    '제출하기 전에는 요청 없음'으로 코드가 채운다. 폼과 그 제출 버튼이 같은 API 를 말하면 하나로 센다.

    2026-11-15 c0df 재빌드(React v13 검증): 로그인 화면 입력란 login-email·login-password 에 조작 방법이 없어 재요청 뒤에도 계약이 막혔다
    (제출은 로그인 요청 하나). 검색·필터·자동 저장처럼 입력하는 동안 요청이 나갈 수 있는 입력란, 제출 대상이 없거나 여럿인 화면은
    짐작하지 않는다 — 검사(_ui_actions)가 다시 묻는다."""
    if contract.interface_kind != "ui":
        return contract, []

    def target(el: UiElement) -> tuple[str, str] | None:
        if el.role == "form":
            e = _form_endpoint(contract, el)
            return (e.method, _route_shape(e.path)) if e is not None else None
        if el.role == "button":
            hit = api_mentions(el.action or "")
            return (hit[0][0], _route_shape(hit[0][1])) if hit else None
        return None

    notes: list[str] = []
    screens = []
    for sc in contract.screens:
        forms = [el for el in sc.elements if el.role == "form"]
        filled: list[str] = []
        elements = []
        for el in sc.elements:
            if (
                el.role in ("input", "textbox")
                and not (el.action or "").strip()
                and not _LIVE_INPUT.search(f"{el.test_id} {el.description}")
            ):
                own = [f for f in forms if _owned(f, el)]
                scope = [
                    x for x in sc.elements if not own or x in own or any(_owned(f, x) for f in own)
                ]
                targets = {t for x in scope if (t := target(x)) is not None}
                if len(targets) == 1:
                    el = el.model_copy(update={"action": _FIELD_ACTION})
                    filled.append(el.test_id)
            elements.append(el)
        if filled:
            notes.append(
                f"화면 {sc.route} 의 입력란 {', '.join(filled)} 에 조작 방법이 없어 '{_FIELD_ACTION}' 으로 채움 "
                "(요청을 보내는 제출 대상이 하나)"
            )
            screens.append(sc.model_copy(update={"elements": elements}))
        else:
            screens.append(sc)
    if not notes:
        return contract, []
    return contract.model_copy(update={"screens": screens}), notes


def fit_forms(contract: TaskContract) -> tuple[TaskContract, list[str]]:
    """ui 계약의 폼 — 입력란·제출 버튼이 요소로 없으면 폼의 조작에 적힌 제출 요청(메서드·경로)의 요청 칸에서 코드가 채운다.

    2026-11-11 프론트 알파 fe8-user: 로그인 폼(login-form, 조작 'POST /api/auth/login', 요청 칸 username·password)에 입력란도
    버튼도 없어 계약 답 3개가 모두 막혔다 (재요청 문구가 'form 대신 button 하나로'를 알려 줬지만 이 폼은 입력란이 필요했다).
    - 요청 칸이 있으면 칸마다 입력란 `<폼>-<칸>` (요청 번호·토큰·시각처럼 앱이 채우는 칸은 빼고), 버튼이 없으면 제출 버튼 `<폼>-submit`.
    - 요청 칸이 없는 폼이면 입력란이 필요 없다 — 버튼이 없으면 폼을 같은 test id 의 버튼으로, 있으면 폼을 묶음(region)으로 (test id 는 그대로).
    - 화면에 폼이 여럿이면 폼마다 그 폼의 요소(test id 접두)로 본다. 요소 수 상한(30)은 넘지 않는다.
    - 제출 요청을 찾지 못한 폼은 그대로 — 검사(_ui_form_fields)가 다시 묻는다."""
    if contract.interface_kind != "ui":
        return contract, []
    notes: list[str] = []
    screens = []
    for sc in contract.screens:
        forms = [el for el in sc.elements if el.role == "form"]
        if not forms:
            screens.append(sc)
            continue
        elements = list(sc.elements)
        ids = {el.test_id for el in elements}
        for form in forms:
            mine = [el for el in elements if len(forms) == 1 or _owned(form, el)]
            has_field = any(el.role in _FIELD_ROLES for el in mine)
            has_button = any(el.role == "button" for el in mine)
            if has_field and has_button:
                continue
            e = _form_endpoint(contract, form)
            if e is not None and not has_field and not e.request:
                at = elements.index(form)
                role, how = (
                    ("region", "묶음(region)으로 (버튼이 제출한다)")
                    if has_button
                    else ("button", "같은 test id 의 버튼으로")
                )
                elements[at] = form.model_copy(update={"role": role})
                notes.append(
                    f"화면 {sc.route} 의 폼 {form.test_id} 는 요청 칸이 없는 {e.method} {e.path} 를 보내 입력란이 필요 없다 — {how}"
                )
                continue
            fields = _input_fields(e, sc.route) if e is not None else []
            if e is not None and not has_field and fields:
                room = _MAX_ELEMENTS - len(elements) - (0 if has_button else 1)
                new = [
                    el
                    for f in fields
                    if (el := _field_element(form, f)).test_id not in ids and len(el.test_id) <= 60
                ][: max(0, room)]
                if new:
                    at = elements.index(form) + 1
                    elements[at:at] = new
                    ids |= {el.test_id for el in new}
                    has_field = True
                    notes.append(
                        f"화면 {sc.route} 의 폼 {form.test_id} 에 입력란이 없어 {e.method} {e.path} 의 요청 칸으로 채움: "
                        + ", ".join(f"{el.test_id}({el.role})" for el in new)
                    )
            submit = form.action or (
                f"클릭하면 폼을 제출 — {e.method} {e.path} 요청" if e is not None else ""
            )
            sid = f"{form.test_id}-submit"
            if (
                has_field
                and not has_button
                and submit
                and sid not in ids
                and len(elements) < _MAX_ELEMENTS
            ):
                after = (
                    elements.index(form)
                    + 1
                    + sum(1 for el in elements if el.test_id.startswith(f"{form.test_id}-"))
                )
                button = UiElement(
                    test_id=sid,
                    role="button",
                    description=f"폼 {form.test_id} 제출 버튼",
                    action=submit[:200],
                    refs=list(form.refs),
                )
                elements.insert(after, button)
                ids.add(sid)
                notes.append(
                    f"화면 {sc.route} 의 폼 {form.test_id} 에 제출 버튼이 없어 {sid} 을 더함 (폼의 조작대로)"
                )
        screens.append(
            sc.model_copy(update={"elements": elements}) if elements != list(sc.elements) else sc
        )
    if not notes:
        return contract, []
    return contract.model_copy(update={"screens": screens}), notes


def _ui_form_fields(contract: TaskContract) -> list[str]:
    """ui 계약: 폼(form) 요소만 있고 그 안의 입력란·제출 버튼이 요소로 없음 — 테스트는 test id 로만 찾으므로 입력란을 짐작한다
    (`input[name="userId"]`). 2026-11-10 저장된 화면 계약에서 폼이 있는 화면 13개 중 12개가 이랬고, 알파 fe7d-user 숨은 테스트가
    로그인 입력란을 name 속성으로 찾다 모든 구현에서 예외. 제출 요청이 폼에 적혀 있으면 fit_forms 가 먼저 채우므로, 여기 남는 것은
    제출 요청을 찾지 못했거나 요청 칸이 모두 앱이 채우는 칸인 폼이다 (2026-11-11 알파 fe8-user)."""
    if contract.interface_kind != "ui":
        return []
    out: list[str] = []
    for sc in contract.screens:
        forms = [el for el in sc.elements if el.role == "form"]
        if not forms:
            continue
        # 폼마다 그 폼의 요소로 — fit_forms 와 같은 소속 (폼이 하나면 화면 전체, 여럿이면 test id 접두). #248: 화면 전체의 역할을
        # 한데 모아 완전한 폼(login-form) 옆의 입력란·버튼 없는 폼(profile-form)을 놓쳤다. 빠진 것이 같은 폼끼리 한 문장
        broken: dict[tuple[str, ...], list[UiElement]] = {}
        for form in forms:
            roles = {el.role for el in sc.elements if len(forms) == 1 or _owned(form, el)}
            missing = tuple(
                w
                for w, ok in (
                    ("입력란", bool(roles & _FIELD_ROLES)),
                    ("제출 버튼", "button" in roles),
                )
                if not ok
            )
            if missing:
                broken.setdefault(missing, []).append(form)
        for missing, bad in broken.items():
            sent = [(f, e) for f in bad if (e := _form_endpoint(contract, f)) is not None]
            if sent:
                how = "; ".join(
                    f"{f.test_id} 는 {e.method} {e.path} 를 보낸다 — 요청 칸 {'·'.join(x.name for x in e.request) or '없음'} 중 사용자가 입력하는 칸마다"
                    for f, e in sent
                )
                advice = f"{how} 입력란(textbox 등)을, 그리고 제출 버튼을 각각 test id 가 있는 요소로 적고 조작(action)을 쓴다"
            else:
                advice = (
                    "폼 요소의 조작(action)에 제출할 때 나가는 요청을 메서드·경로로 적고 (예: '제출 버튼을 누르면 POST /api/auth/login'), "
                    "그 요청 칸 중 사용자가 입력하는 칸마다 입력란(textbox 등)과 제출 버튼을 각각 test id 가 있는 요소로 적는다"
                )
            out.append(
                f"화면 {sc.route} 의 폼({', '.join(f.test_id for f in bad)})에 {'·'.join(missing)} 요소가 없음 — {advice} "
                "(테스트는 test id 로만 찾는다). 입력란이 필요 없는 폼이면 form 대신 button 하나로 적는다"
            )
    return out


def _faults(contract: TaskContract, plan: HiddenPlan, spec: SpecView) -> list[str]:
    """숨은 케이스의 실패 지정(HiddenCase.faults) — 계약에 있는 API·그 요청 본문 칸인지, 그리고 명세에서 승인된 실패 지정
    (숨김 수용 기준 faults: 동작·항목·횟수·상태)을 같은 값으로 옮겼는지 [확정: 2026-11-11 사용자 — 무엇을 실패시킬지는 명세가 정하고
    이 단계는 API·칸에 연결만]. 승인된 지정이 있는 케이스는 시나리오도 순번으로 실패를 정하면 안 된다 (2026-11-11 재측정 오프라인)."""
    by_id = {e.endpoint_id: e for e in contract.endpoints}
    out: list[str] = []
    for c in plan.cases:
        for f in c.faults:
            e = by_id.get(f.endpoint)
            if e is None:
                out.append(f"숨은 케이스 {c.case_id} 의 실패 지정 API {f.endpoint} 가 계약에 없음")
                continue
            names = {x.name for x in e.request}
            bad = sorted(set(f.when) - names)
            if bad:
                out.append(
                    f"숨은 케이스 {c.case_id} 의 실패 지정이 {e.method or ''} {e.path} 요청 본문에 없는 칸으로 고름: {', '.join(bad)} "
                    f"(요청 본문 칸: {', '.join(sorted(names)) or '없음'})"
                )
        approved = spec.hidden_faults.get(c.ac_id, ())
        for hf in approved:
            if not any(
                hf.item in {str(v) for v in f.when.values()}
                and f.times == hf.times
                and f.status == hf.status
                for f in c.faults
            ):
                what = f"{hf.status} 응답" if hf.status is not None else "연결 실패"
                out.append(
                    f"숨은 케이스 {c.case_id} 가 숨김 수용 기준 {c.ac_id} 의 승인된 실패 지정을 옮기지 않음: '{hf.operation}' 의 "
                    f"항목 '{hf.item}' 처음 {hf.times}번 → {what} — 그 동작의 계약 API 를 endpoint 로, 항목을 가리키는 요청 본문 칸을 "
                    f"when 에 (값 '{hf.item}' 그대로), times·status 도 같게 faults 에 넣는다"
                )
        m = ORDINAL_FAULT.search(" ".join([c.scenario, c.expect, *c.setup])) if approved else None
        if m:
            out.append(
                f"숨은 케이스 {c.case_id} 시나리오가 실패를 순번으로 정함('{m.group(0)}') — 승인된 실패 지정대로 항목 내용으로 적는다 "
                "(하나씩 보내는 구현과 한꺼번에 보내는 구현에서 다른 요청이 실패한다)"
            )
    return out


def check_contract(
    contract: TaskContract,
    plan: HiddenPlan,
    spec: SpecView,
    manifest: TemplateManifest,
    seed: FeatureSeed | None = None,
    first_answer: bool = True,
    interface: MaterialInterface | None = None,
    forbidden_storage: Sequence[ForbiddenStorage] = (),
    check_terms: Sequence[str] = (),
    avoid_terms: Sequence[str] = (),
) -> ContractCheck:
    """공개 계약·HiddenPlan 결정적 검사 (§3.3). problems 는 재요청 사유, spec_gaps 는 명세 수정 사유.
    first_answer: 자료와 다른 상태 값은 첫 답에서만 재요청하고 그 뒤엔 확인 표시 (요구가 새 상태를 더할 수 있다).
    interface: 재료가 정한 명령·입출력 파일·엔티티 (#256) — 그대로인지·밖의 요소가 없는지 보고, 누설 검사에서는 그 부분을 뺀다."""
    problems = _ids(contract, plan)
    problems += _coverage(contract, plan, spec)
    problems += _naming(contract, manifest, spec)
    problems += _setting_units(contract)
    problems += _command_paths(contract, manifest)
    problems += _api_refs(contract, plan, spec)
    problems += _added_marked_existing(contract, spec)
    problems += _leaks(without_fixed(contract, interface), spec, seed, check_terms)
    problems += _forbidden_storage(contract, forbidden_storage)
    problems += _contract_hygiene(contract, avoid_terms)
    problems += _ui_actions(contract)
    problems += _ui_options(contract)
    problems += _ui_load_and_background(contract)
    problems += _ui_data_source(contract)
    problems += _ui_form_fields(contract)
    if "file" in manifest.interfaces:
        problems += _file_fixtures(contract)
    problems += _harness_scope(contract, manifest)
    harness_problems, gaps, flags = _harness(
        plan, spec, manifest, seed.harness_rules if seed is not None else None
    )
    problems += harness_problems
    interface_issues, interface_flags = interface_problems(contract, interface, manifest)
    problems += interface_issues
    flags += interface_flags
    problems += _reasons(plan, manifest, seed)
    problems += _faults(contract, plan, spec)
    problems += feasibility_problems(plan, manifest)
    if manifest.seed_rows:
        problems += seed_problems(contract)
        problems += row_use_problems(contract)
        problems += row_target_problems(contract)
        problems += case_rows_problems(contract, plan, manifest)
        problems += _target_rows(contract, plan)
    else:
        problems += _no_seed_rows(contract, plan, manifest)
    problems += _foreign_keys(contract)
    problems += _internal_expect(plan, manifest)
    if manifest.seed_rows:
        entity_problems, entity_flags = _entity_existing(contract, plan)
        problems += entity_problems
        flags += entity_flags
    # 재료가 선언한 필요 요소는 행 시드가 없는 템플릿(프론트엔드)에서도 — 2026-11-09 팩트 체크: 행 시드 템플릿에서만 돌아 프론트 재료의 선언이 무시됐다
    problems += _needs(contract, plan, seed, manifest.seed_rows)
    problems += state_problems(contract, plan)
    materials = material_state_problems(contract, spec)
    if first_answer:
        problems += materials
    else:
        flags += [FLAG_MATERIAL + p for p in materials]
    problems += unreachable_targets(contract, plan)
    # 권한 칸·헤더 값 검사는 서버가 판정하는 과제(행 시드·요청자 행) 몫 — 화면 과제는 테스트가 서버 응답(401 포함)을 직접 정한다.
    # 2026-11-10 프론트 알파 fe7-token: 토큰 만료 401 에 '요청자 행·X-Actor 통과 값'을 요구해 계약 3번이 모두 거절됐다.
    if contract.interface_kind != "ui":
        problems += auth_problems(contract, manifest.seed_rows)
        flags += auth_flags(contract)
        problems += header_problems(contract)
    return ContractCheck(problems=problems, spec_gaps=gaps, review_flags=flags)
