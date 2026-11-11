"""명세 칸 부분 수리 — 재요청 뒤에도 남은 '낱말이 보이면 안 되는 칸' 문제를, 걸린 칸의 글만 고쳐 다시 검사한다.

2026-11-11 명세 알파: 남은 실패가 거의 모두 칸 하나·문장 하나의 문제였다 (상황에 운영 조건 '번갈아'·숨은 요구 '잠금',
추가 요구 답에 원인 용어 '캐시 키', 상황에 '대기 목록 정리 시점'). 재요청은 명세 전체를 다시 쓰게 해, 같은 문장을 또 넣거나
다른 칸이 흔들렸다 (로그아웃: 첫 답과 최종 답의 문제가 같음). 고칠 칸의 글만 보내고 코드가 끼워 넣은 뒤 같은 검사를 다시 돌린다.

어느 칸을 고칠지는 검사 메시지가 정한다 — 메시지마다 그 검사가 보는 칸(RULES)과 걸린 낱말을 짝지어, 그 낱말이 든 칸만 고친다.
여러 칸의 관계가 문제인 검사(행동 기준의 수행준거 인용 등)는 여기서 다루지 않는다 (전체 재요청에 남는다).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from task_spec.llm import LLMClient
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import FieldFixInput, FieldFixOutput, Provenance

logger = logging.getLogger(__name__)

TASK = "fix_feature_fields"

# 칸 묶음 — DraftFeatureOutput 의 지원자에게 보이는 글
STORY = ("title", "scenario", "requirements", "acceptance_public")
VISIBLE = (*STORY, "deliverables", "clarifications", "artifact_titles")
STAGE2 = ("debug_scenario", "debug_deliverables", "debug_artifact_titles")

# 검사 메시지 머리 → 그 검사가 보는 칸 (spec/feature.py check_feature · spec/draft.py check 의 메시지와 같은 말로 시작)
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("숨은 요구를 드러내는 말이 1단계 지원자 화면", VISIBLE),
    ("운영 조건을 드러내는 말이 1단계 제목·상황·공개 요구·공개 수용 기준", STORY),
    ("운영 조건을 드러내는 말이 1단계 자료 제목", ("artifact_titles",)),
    ("숨은 요구·원인을 드러내는 말이 2단계", STAGE2),
    (
        "원 사례의 원인 용어가 공개 요구·추가 요구 답·2단계 상황",
        ("requirements", "clarifications", "debug_scenario", "debug_deliverables"),
    ),
    ("먼저 말할 개념이 지원자에게 보이는 곳", (*STORY, "deliverables", "clarifications")),
    ("먼저 말할 개념이 2단계", STAGE2),
    (
        "제목·공개 요구·공개 수용 기준이 구현 방법을 지시함",
        ("title", "requirements", "acceptance_public"),
    ),
    ("핵심 원인 개념이 제목·상황·제출물", ("title", "scenario", "deliverables")),
    ("원 사례의 원인 용어가 제목·상황·제출물", ("title", "scenario", "deliverables")),
    ("본문에 기술 환경 밖 언어가 나옴", ("title", "scenario", "deliverables", "artifact_titles")),
)

_TERMS = re.compile(r"(?:나옴|드러남)[^:]*:\s*([^—\n]+)|지시함 \(([^)]*)\)")


@dataclass(frozen=True)
class Target:
    path: str  # 예: scenario · requirements[1].text · clarifications[3].answer
    text: str
    terms: tuple[str, ...]
    why: str  # 걸린 검사 메시지의 머리


def _fields(out: Any, group: str) -> Iterator[tuple[str, str]]:
    """칸 묶음 → (경로, 글)."""
    dbg = getattr(out, "debug_stage", None)
    if group in ("title", "scenario"):
        yield group, getattr(out, group) or ""
    elif group == "deliverables":
        yield from ((f"deliverables[{i}]", d) for i, d in enumerate(out.deliverables))
    elif group == "requirements":
        yield from ((f"requirements[{i}].text", r.text) for i, r in enumerate(out.requirements))
    elif group == "acceptance_public":
        yield from (
            (f"acceptance_public[{i}].text", a.text) for i, a in enumerate(out.acceptance_public)
        )
    elif group == "clarifications":
        for i, c in enumerate(out.clarifications):
            yield f"clarifications[{i}].answer", c.answer
            if getattr(c, "answer_hint", None):
                yield f"clarifications[{i}].answer_hint", c.answer_hint
    elif group == "artifact_titles":
        yield from ((f"artifacts[{i}].title", a.title) for i, a in enumerate(out.artifacts))
    elif dbg is not None and group == "debug_scenario":
        yield "debug_stage.scenario", dbg.scenario
    elif dbg is not None and group == "debug_deliverables":
        yield from ((f"debug_stage.deliverables[{i}]", d) for i, d in enumerate(dbg.deliverables))
    elif dbg is not None and group == "debug_artifact_titles":
        yield from (
            (f"debug_stage.artifacts[{i}].title", a.title) for i, a in enumerate(dbg.artifacts)
        )


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def problem_terms(problem: str) -> list[str]:
    """검사 메시지에서 걸린 낱말 — '…나옴: A, B — …' · '…지시함 (A, B) — …'."""
    m = _TERMS.search(problem)
    if m is None:
        return []
    raw = m.group(1) or m.group(2) or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


# 추가 요구 하나를 id 로 짚는 메시지 — 그 추가 요구의 모호한 답 칸만 (spec/feature.py check_hints)
_CLAR_HINT = re.compile(
    r"^추가 요구 (?P<cid>\w+) 의 모호한 답에 숨은 요구를 드러내는 말: (?P<terms>.+)$"
)


def _clar_hint_target(out: Any, problem: str) -> Target | None:
    m = _CLAR_HINT.match(problem.strip())
    if m is None:
        return None
    for i, c in enumerate(out.clarifications):
        if c.clarification_id == m.group("cid") and getattr(c, "answer_hint", None):
            terms = tuple(t.strip() for t in m.group("terms").split(",") if t.strip())
            return Target(
                f"clarifications[{i}].answer_hint", c.answer_hint, terms, problem.split(":", 1)[0]
            )
    return None


def targets(out: Any, problems: list[str]) -> list[Target]:
    """고칠 칸 — 문제마다 그 검사가 보는 칸 중 걸린 낱말이 든 칸. 같은 칸은 낱말을 모은다."""
    found: dict[str, Target] = {}
    for p in problems:
        hint = _clar_hint_target(out, p)
        if hint is not None:
            old = found.get(hint.path)
            found[hint.path] = Target(
                hint.path,
                hint.text,
                tuple(dict.fromkeys([*(old.terms if old else ()), *hint.terms])),
                hint.why,
            )
            continue
        rule = next((groups for head, groups in RULES if p.startswith(head)), None)
        terms = problem_terms(p)
        if rule is None or not terms:
            continue
        why = p.split(":", 1)[0]
        for group in rule:
            for path, text in _fields(out, group):
                hit = tuple(t for t in terms if _norm(t) in _norm(text))
                if not hit:
                    continue
                old = found.get(path)
                merged = tuple(dict.fromkeys([*(old.terms if old else ()), *hit]))
                found[path] = Target(path, text, merged, old.why if old else why)
    return list(found.values())


_PATH = re.compile(r"^(\w+)(?:\[(\d+)\])?(?:\.(\w+))?(?:\[(\d+)\])?(?:\.(\w+))?$")


def apply(out: Any, fixed: dict[str, str]) -> Any:
    """고친 글을 그 경로에 끼운다 (모르는 경로는 버림). 모델을 새로 만들어 돌려준다."""
    data = out.model_dump()
    for path, text in fixed.items():
        m = _PATH.match(path)
        if m is None:
            continue
        keys: list[str | int] = [m.group(1)]
        if m.group(2) is not None:
            keys.append(int(m.group(2)))
        if m.group(3):
            keys.append(m.group(3))
        if m.group(4) is not None:
            keys.append(int(m.group(4)))
        if m.group(5):
            keys.append(m.group(5))
        node: Any = data
        try:
            for k in keys[:-1]:
                node = node[k]
            last = keys[-1]
            if isinstance(node, list) and isinstance(last, int) and 0 <= last < len(node):
                node[last] = text
            elif isinstance(node, dict) and isinstance(last, str) and last in node:
                node[last] = text
        except (KeyError, IndexError, TypeError):
            continue
    return type(out).model_validate(data)


async def fix_fields(
    out: Any,
    problems: list[str],
    *,
    llm: LLMClient,
    prompts: PromptRegistry,
    ontology_version: str,
    checker: Callable[[Any], list[str]],
) -> tuple[Any | None, list[str], Provenance | None]:
    """걸린 칸만 고쳐 다시 검사 — 문제가 줄면 (고친 출력, 남은 문제, provenance), 아니면 (None, problems, provenance|None)."""
    tg = targets(out, problems)
    if not tg:
        return None, problems, None
    try:
        template = prompts.get(TASK)
    except Exception:  # 프롬프트가 없는 레지스트리 (옛 평가) — 부분 수리 없이
        return None, problems, None
    inp = FieldFixInput(
        fields="\n".join(f"[{t.path}] {t.text}" for t in tg),
        rules="\n".join(f"- [{t.path}] 빼야 할 말: {', '.join(t.terms)} — {t.why}" for t in tg),
    )
    try:
        r = await llm.call(
            assemble(template, inp, ontology_version=ontology_version), FieldFixOutput
        )
    except Exception as exc:  # LLM 실패는 부분 수리만 포기 — 원래 결과를 그대로 쓴다
        logger.warning("%s failed %s", TASK, type(exc).__name__)
        return None, problems, getattr(exc, "provenance", None)
    allowed = {t.path for t in tg}
    fixed = {f.path: f.text for f in r.parsed.fields if f.path in allowed}  # type: ignore[attr-defined]
    try:
        patched = apply(out, fixed)
    except Exception as exc:  # 길이 상한 등 — 이 수리는 버린다
        logger.warning("%s apply_invalid %s", TASK, type(exc).__name__)
        return None, problems, r.provenance
    left = checker(patched)
    logger.info(
        "%s targets=%d fixed=%d problems %d->%d",
        TASK,
        len(tg),
        len(fixed),
        len(problems),
        len(left),
    )
    if len(left) < len(problems):
        return patched, left, r.provenance
    return None, problems, r.provenance
