"""손 시범 재료·풀이 판·경로 (docs/design/analysis-stage.md §9.1).

재료 하나 = 빌드 검증을 통과한 백엔드 알파 하나 + 손으로 쓴 것(요청 글·`request_mentions`·사실 두 층·공개 수용 기준 → 요구·요구별 예측).
`data/eval/analysis-pilot/materials/<mid>/material.json` 과 같은 폴더의 `README.thin.md`(B1 — 요청 절, 계약은 모양만, 진행 방식 절 없음),
`public-behavior/`(②용 공개 테스트 — 사유 값 단언을 빼고 상태 코드는 4xx 로 고친 사본), `synonyms.json`(expand_terms 결과).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from task_spec.schemas import TaskItem
from task_spec.schemas.base import Schema
from task_spec.spec.feature import find_terms
from task_spec.starter.answer_rule import term_in
from task_spec.starter.facilitator import Answer, facilitator_view
from task_spec.starter.facilitator import Material as FacilitatorMaterial

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "eval" / "analysis-pilot"
RUNS = Path(os.environ.get("PILOT_RUNS", "/ssd/solo_runs/analysis-pilot"))
MODELS = ("opus", "sonnet", "haiku")  # 강한 순 — fable 제외 (지금 관례)

FactLayer = Literal["base", "pilot"]
Mode = Literal["new", "legacy"]


class Fact(Schema):
    """요청자 지식 1건 (§4.2). layer — base: 공식 요구 사실·B1 예외 경로의 칸·값 (빌드 알파도 씀),
    pilot: 기존 공개 테스트를 풀 수 있게 넣는 예전 방식 값 (규칙별 상태 코드·사유 값 — 시범만)."""

    fact_id: str = Field(pattern=r"^f\d{1,2}$")
    covers: list[str] = Field(default_factory=list)  # r# · p#
    topic: str = Field(min_length=1)
    match_terms: list[str] = Field(min_length=1)
    answer: str = Field(min_length=1)
    answer_hint: str | None = None
    # 구체 답 조건 문장 — 진행자 LLM 이 질문과 견준다 (§4.2 새 형식, 2026-11-13 사용자: 낱말 대신 문장 단위로)
    ask_when: str | None = None
    layer: FactLayer = "base"
    # 시작 코드·계약이 이미 보여 주는 사실 — 정보 값 0. 기존 기능 코드는 지우지 않고 이렇게 표시만 한다 (§9.1 재료, #151 검토 4차)
    info_zero: bool = False


class Material(Schema):
    mid: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    alpha: str = Field(min_length=1)  # 레포 기준 알파 출력 경로 (data/eval/build/<알파>)
    request: str = Field(min_length=1)  # 요청 글 (§4.1)
    request_mentions: list[str] = Field(default_factory=list)  # 요청 글이 직접 말한 r#
    facts: list[Fact] = Field(min_length=1)
    req_ids: dict[str, list[str]] = Field(
        default_factory=dict
    )  # p# → r# (공개 수용 기준 → 공식 요구)
    predictions: dict[str, Literal["짐작 가능", "어려움"]] = Field(
        default_factory=dict
    )  # r# → 사람 예측 (판정에 쓰지 않음)
    # ② 테스트 메서드("<클래스>.<메서드>") → 그 메서드가 보는 공식 요구. 있으면 '만들었나'를 공개 수용 기준이 아니라 메서드로 매긴다
    # (#151 검토 4차 — 한 공개 수용 기준이 요구 둘을 같이 보면 가를 수 없었다)
    method_req: dict[str, list[str]] = Field(default_factory=dict)
    # 사실·구체 조건 문장을 어떻게 썼나 — once: 질문 목록을 보기 전에 명세만 보고 한 번 (과적합 막기, §9.1),
    # tuned: 질문 목록으로 여러 번 다듬음 (감사 기록 재료 — 이 규칙 전)
    fact_tuning: Literal["once", "tuned"] = "once"

    @property
    def dir(self) -> Path:
        return DATA / "materials" / self.mid

    def covering_facts(self, req: str) -> list[Fact]:
        """요구 하나를 덮는 사실 — `covers` 에 그 요구가 있거나, 그 요구를 가리키는 공개 수용 기준(p#)이 있는 것."""
        acs = {p for p, rs in self.req_ids.items() if req in rs}
        return [f for f in self.facts if req in f.covers or acs & set(f.covers)]

    def needs_asking(self, req: str) -> bool:
        """묻지 않고는 모르는 요구 — 요청 글이 말하지 않았고, 덮는 사실 중 정보 값이 있는 것(`info_zero` 아님)이 있다 (§9.1 재료)."""
        return req not in self.request_mentions and any(
            not f.info_zero for f in self.covering_facts(req)
        )

    @property
    def alpha_dir(self) -> Path:
        return ROOT / self.alpha

    def task(self) -> TaskItem:
        return TaskItem.model_validate_json((self.alpha_dir / "task.json").read_text("utf-8"))

    def contract(self) -> dict[str, object]:
        raw = json.loads((self.alpha_dir / "contract.json").read_text("utf-8"))
        return raw.get("contract", raw)

    def synonyms(self) -> dict[str, list[str]]:
        p = self.dir / "synonyms.json"
        return json.loads(p.read_text("utf-8")).get("items", {}) if p.exists() else {}

    def measured(self) -> list[str] | None:
        """유효성 검사(validity.py)를 통과한 요구 — 돌연변이가 ② 에서 떨어지는 요구만 측정한다. 검사 전이면 None."""
        p = self.dir / "validity.json"
        return list(json.loads(p.read_text("utf-8")).get("measured", [])) if p.exists() else None


def load_material(mid: str) -> Material:
    return Material.model_validate_json(
        (DATA / "materials" / mid / "material.json").read_text("utf-8")
    )


def all_materials() -> list[Material]:
    root = DATA / "materials"
    return [load_material(p.parent.name) for p in sorted(root.glob("*/material.json"))]


@dataclass(frozen=True)
class Arm:
    """풀이 판 (§9.1 표)."""

    n: int
    readme: Literal["thin", "current"]  # thin = B1 얇은 README, current = 알파 그대로
    ask: Mode | None  # 진행자 — None 이면 묻지 못함
    facts_in_readme: bool = False  # 판 0 — 사실 두 층을 README 에 처음부터


ARMS = {
    0: Arm(0, "thin", None, facts_in_readme=True),
    1: Arm(1, "thin", "new"),
    2: Arm(2, "thin", None),
    3: Arm(3, "current", "legacy"),
    4: Arm(4, "thin", "new"),
}

_LABEL = re.compile(
    r"^(?P<mid>[a-z0-9]+(?:-[a-z0-9]+)*)-p(?P<arm>[0-4])-(?P<model>opus|sonnet|haiku)-r(?P<run>\d+)$"
)


def run_label(mid: str, arm: int, model: str, run: int) -> str:
    return f"{mid}-p{arm}-{model}-r{run}"


def parse_label(label: str) -> tuple[str, int, str, int]:
    m = _LABEL.match(label)
    if m is None:
        raise ValueError(f"풀이 이름이 아님: {label}")
    return m["mid"], int(m["arm"]), m["model"], int(m["run"])


def clarification_items(task: TaskItem) -> list[Answer]:
    return [
        Answer(
            id=c.clarification_id,
            topic=c.topic,
            match_terms=list(c.match_terms),
            answer=c.answer,
            answer_hint=c.answer_hint,
        )
        for c in task.clarifications
    ]


def fact_items(m: Material, layers: tuple[FactLayer, ...] = ("base", "pilot")) -> list[Answer]:
    return [
        Answer(
            id=f.fact_id,
            topic=f.topic,
            match_terms=list(f.match_terms),
            answer=f.answer,
            answer_hint=f.answer_hint,
            ask_when=f.ask_when,
        )
        for f in m.facts
        if f.layer in layers
    ]


def facilitator_items(
    m: Material, mode: Mode, synonyms: dict[str, list[str]] | None = None
) -> list[Answer]:
    """진행자가 답을 고르는 항목 — new: 사실 두 층 + 추가 요구, legacy(판 3): 추가 요구만.
    synonyms 를 주면 매칭 낱말에 더한다 (진행자 쪽 고치기 — 생성기 동의어 확장 단계의 시뮬레이션)."""
    task = m.task()
    items = (
        clarification_items(task)
        if mode == "legacy"
        else [*fact_items(m), *clarification_items(task)]
    )
    if not synonyms:
        return items
    return [
        i.model_copy(
            update={"match_terms": list(dict.fromkeys([*i.match_terms, *synonyms.get(i.id, [])]))}
        )
        for i in items
    ]


def stage1_materials(task: TaskItem) -> list[FacilitatorMaterial]:
    return facilitator_view("pilot", task).stage1.materials.items


def legacy_limits(task: TaskItem) -> tuple[int | None, int | None]:
    """판 3 한도 — 알파 명세에 적힌 지금 값 그대로 (질문 MAX_CLARIFICATIONS, 자료 _limit)."""
    return task.max_clarifications, task.max_requests


# 규칙을 이름으로 말하는 저장소 메서드 (Spring Data 파생 쿼리) → 그 규칙을 말하는 요구 문장의 낱말
_DERIVED = re.compile(r"\b(?:find|count|exists|delete|get|read|query|stream)\w*?By\w+")
_NAME_RULES = (
    (re.compile(r"OrderBy\w*(?:Asc|Desc)"), re.compile(r"정렬|순서|최신|오름|내림|최근")),
    (re.compile(r"Top\d*|First\d*"), re.compile(r"건수|최대|개까지|상한|크기|개를|개만")),
    (
        re.compile(r"GreaterThan|LessThan|Between|Before|After"),
        re.compile(r"이상|이하|초과|미만|이전|이후|사이|기간"),
    ),
    (re.compile(r"Distinct"), re.compile(r"중복")),
)
_TODO = re.compile(r"UnsupportedOperationException\(\s*\"([^\"]*)\"")


def _thin_sources(m: Material) -> list[str]:
    """판 0·1·2·4 가 받는 Java 시작 코드 — 응시용 프로젝트에 재료 `starter-thin/` 사본을 덮은 것 (prepare.apply_starter_thin)."""
    cand = m.alpha_dir / "candidate"
    src = cand / "src" / "main"
    out: list[str] = []
    for p in sorted(src.rglob("*.java")) if src.exists() else []:
        thin = m.dir / "starter-thin" / p.relative_to(cand)
        out.append((thin if thin.is_file() else p).read_text("utf-8", errors="ignore"))
    return out


def starter_shows(m: Material) -> list[str]:
    """시작 코드가 이미 보여 주는 규칙인데 그 요구를 덮는 사실이 정보 값 0(`info_zero`)이 아닌 곳 — §9.1 "기능 코드가 이미 보여 주는 규칙은 정보 값 0".
    2026-11-15 손 시범: coupons 의 시작 코드가 새 차감 API 를 구현(계약이 existing 으로 적음), retry 의 저장소에
    `findByMemberIdOrderByCreatedAtDesc` 가 있었는데, 재료를 쓸 때 시작 코드를 보지 않아 두 재료 모두 정보 값을 잘못 적었다.
    Java 시작 코드만 본다 (손 시범 재료는 백엔드) — starter-thin 을 덮은 것. (1) 자리 표시(UnsupportedOperationException)가 없는 계약 API =
    시작 코드가 구현, (2) 규칙을 이름으로 말하는 파생 쿼리 메서드 — 그 규칙 낱말이 있는 요구. 부르는 곳이 없는 선언(이름이 한 번만 나옴)은
    정보 값 0 이 아니라 starter-thin 에서 지운다 (§9.1 예외 [확정: 2026-11-15 사용자]) — 정보 값과 상관없이 알린다."""
    files = _thin_sources(m)
    if not files:
        return []
    code = "\n".join(files)
    task = m.task()
    req_text = {r.req_id: r.text for r in task.requirements}
    stubs = " ".join(_TODO.findall(code))
    out: list[str] = []

    def unzeroed(reqs: set[str]) -> list[str]:
        return sorted({f.fact_id for r in reqs for f in m.covering_facts(r) if not f.info_zero})

    for e in m.contract().get("endpoints", []):  # type: ignore[union-attr]
        api = f"{e.get('method')} {e.get('path')}"
        reqs = {r for r in e.get("refs", []) if r in req_text}
        if api in stubs or not reqs:
            continue
        if facts := unzeroed(reqs):
            out.append(
                f"시작 코드가 {api} 를 구현함 (자리 표시 없음) — 덮는 요구 {', '.join(sorted(reqs))} 의 사실 {', '.join(facts)} 정보 값 0 후보"
            )
    for name in sorted(set(_DERIVED.findall(code))):
        for name_rx, words in _NAME_RULES:
            if not name_rx.search(name):
                continue
            reqs = {r for r, t in req_text.items() if words.search(t)}
            if reqs and len(re.findall(rf"\b{name}\b", code)) == 1:
                out.append(
                    f"시작 코드의 메서드 이름 {name} 이 규칙을 말하는데 부르는 곳이 없음 (요구 {', '.join(sorted(reqs))}) — "
                    "starter-thin 에서 선언을 지운다 (§9.1)"
                )
            elif facts := unzeroed(reqs):
                out.append(
                    f"시작 코드의 메서드 이름 {name} 이 규칙을 말함 — 요구 {', '.join(sorted(reqs))} 의 사실 {', '.join(facts)} 정보 값 0 후보"
                )
    return out


def check_material(m: Material) -> list[str]:
    """손으로 쓴 재료 점검 (§4.1·§4.2·§5.1 의 명세 쪽 검사 중 시범에 필요한 것). 빈 목록이면 통과."""
    task = m.task()
    reqs = {r.req_id for r in task.requirements}
    pubs = {a.ac_id for a in task.acceptance_public}
    problems: list[str] = []
    covered = {c for f in m.facts for c in f.covers}
    for f in m.facts:
        bad = [c for c in f.covers if c not in reqs | pubs]
        if bad:
            problems.append(f"{f.fact_id}: covers 에 명세에 없는 id {bad}")
    for x in sorted(reqs | pubs):
        if x not in covered and x not in m.request_mentions:
            problems.append(f"{x}: 덮는 사실이 없고 요청 글도 말하지 않음 — 물어서 알 수 없는 요구")
    for p in sorted(pubs):
        if not m.req_ids.get(p):
            problems.append(f"{p}: 공개 수용 기준 → 공식 요구(req_ids) 없음")
    linked = {r for rs in m.req_ids.values() for r in rs}
    for r in sorted(reqs - linked):
        problems.append(
            f"{r}: 이 요구를 가리키는 공개 수용 기준이 없음 — '만들었나'를 정할 수 없음"
        )
    bad_mentions = [r for r in m.request_mentions if r not in reqs]
    if bad_mentions:
        problems.append(f"request_mentions 에 명세에 없는 요구 {bad_mentions}")
    if re.search(r"\d", m.request):
        problems.append("요청 글에 숫자가 있음 (§4.1 — 수치는 물어서)")
    concepts = [o.value for o in task.observables if o.kind == "first_mention_of"]
    shown = find_terms(concepts, m.request)
    if shown:
        problems.append(f"요청 글에 숨은 요구 개념 {shown}")
    for f in m.facts:
        leak = find_terms(concepts, "\n".join([f.answer, f.answer_hint or ""]))
        if leak:
            problems.append(f"{f.fact_id}: 사실 답에 숨은 요구 개념 {leak}")
    problems += term_overlaps(facilitator_items(m, "new"))
    problems += starter_shows(m)
    thin = m.dir / "starter-thin"
    for path in sorted(thin.rglob("*")) if thin.exists() else []:
        rel = path.relative_to(thin)
        if path.is_file() and not (m.alpha_dir / "candidate" / rel).exists():
            problems.append(f"starter-thin/{rel}: 응시용 프로젝트에 없는 파일 — 바꾸기만 한다")
    return problems


def term_overlaps(items: list[Answer]) -> list[str]:
    """항목끼리 매칭 낱말이 겹치면 한 주제 질문이 우연히 둘에 걸린다 (§5.1)."""
    out: list[str] = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            pairs = [
                (s, t)
                for s in a.match_terms
                for t in b.match_terms
                if term_in(s, t)
                or term_in(t, s)  # 판 1·4 단어 단위 — 큰 쪽에 걸리는 질문은 둘 다 걸림
            ]
            if pairs:
                out.append(f"{a.id}·{b.id}: 매칭 낱말 겹침 {pairs[:3]}")
    return out
