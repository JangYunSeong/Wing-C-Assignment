"""공고 원문 → (절, 문장) 목록.

1순위 규칙: 절 제목 사전 (업무내용·담당업무 → duties, 지원자격·자격요건 → required, 우대사항 → preferred,
그 밖의 제목 → other). 제목 다음 줄부터 다음 제목 전까지가 그 절. 글머리 기호는 지운다.
`< 소제목 >` / `**…**` 같은 묶음 제목 줄은 문장으로 넣지 않는다.

2순위 LLM (prompts/segment_posting): 담당업무·자격요건 제목을 하나도 찾지 못했을 때만.
LLM 출력 문장은 원문에 글자 그대로 있어야 한다 (지어낸 문장 버림).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from task_spec.llm import LLMClient, SchemaViolation, TransientLLMError, normalize_for_quote
from task_spec.llm.client import EmptyResponse
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import PostingLineIn, SegmentPostingInput, SegmentPostingOutput

_HEADINGS: list[tuple[str, str]] = [
    (
        "duties",
        r"업무\s?내용|담당\s?업무|주요\s?업무|수행\s?업무|직무\s?내용|하시게\s?될\s?업무|이런\s?일을\s?해요|하는\s?일|what you.?ll do|responsibilities",
    ),
    (
        "required",
        r"지원\s?자격|자격\s?요건|필수\s?(?:요건|사항|역량|자격)|필요\s?역량|이런\s?분을\s?찾\w*|requirements|qualifications",
    ),
    (
        "preferred",
        r"우대\s?사항|우대\s?(?:요건|조건|역량)?|이런\s?분이면\s?더\s?좋\w*|preferred|nice to have|plus",
    ),
    (
        "other",
        r"회사\s?소개|팀\s?소개|복지|혜택|근무\s?(?:조건|형태|지|환경)|전형\s?(?:절차|과정)|채용\s?(?:절차|과정)|"
        r"제출\s?서류|기타\s?사항|접수\s?(?:기간|방법)|모집\s?(?:기간|인원)",
    ),
]
_HEADING_RX = [(sec, re.compile(rf"^(?:{rx})$", re.IGNORECASE)) for sec, rx in _HEADINGS]
_BULLET = re.compile(r"^\s*(?:[-•·*▶▷■□◆◇○●ㆍ※✔✓]|\d+[.)]|[①-⑳])\s*")
_DECOR = re.compile(r"[#*\[\]【】<>〈〉《》:：\s■□▶▷◆◇●○ㆍ·\-]+")
_GROUP_TITLE = re.compile(r"^\s*(?:\*\*)?\s*[<〈《\[]\s*.{1,40}\s*[>〉》\]]\s*(?:\*\*)?\s*$")


@dataclass(frozen=True)
class Segmented:
    lines: list[PostingLineIn]
    method: str  # "rules" | "llm" | "llm_failed" | "none"


def _heading(line: str) -> str | None:
    core = _DECOR.sub("", line)
    if not core or len(core) > 20:
        return None
    for sec, rx in _HEADING_RX:
        if rx.match(core):
            return sec
    return None


def segment_rules(text: str) -> list[PostingLineIn]:
    out: list[PostingLineIn] = []
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        sec = _heading(line)
        if sec is not None:
            current = sec
            continue
        if current is None or _GROUP_TITLE.match(line):
            continue
        body = _BULLET.sub("", line).strip().strip("*").strip()
        if len(body) >= 2:
            out.append(PostingLineIn(text=body, section=current))  # type: ignore[arg-type]
    return out


async def segment(text: str, llm: LLMClient | None, prompts: PromptRegistry | None) -> Segmented:
    lines = segment_rules(text)
    if any(x.section in ("duties", "required") for x in lines):
        return Segmented(lines, "rules")
    if llm is None or prompts is None:
        return Segmented(lines, "none")
    prompt = assemble(
        prompts.get("segment_posting"), SegmentPostingInput(text=text), ontology_version="n/a"
    )
    try:
        result = await llm.call(prompt, SegmentPostingOutput)
    except (SchemaViolation, EmptyResponse, TransientLLMError):
        return Segmented(lines, "llm_failed")  # 보조 실패 — 규칙 결과(비어 있을 수 있음)로
    source = normalize_for_quote(text)
    kept = [
        x
        for x in result.parsed.lines
        if normalize_for_quote(x.text) and normalize_for_quote(x.text) in source
    ]
    return Segmented(kept, "llm")
