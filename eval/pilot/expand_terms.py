"""동의어 목록 — 진행자 쪽 고치기에 쓰는 고정 절차 (docs/design/analysis-stage.md §9.1 원인 가르기 1).

운영에서도 재현할 수 있게 생성기와 같은 모델(qwen)·고정 프롬프트(`prompts/expand_match_terms`)로 항목마다 다른 표현을 만들고,
§5.1 정리를 똑같이 건다 — 일반어(`too_general`, 추가 요구 c# 에만)·지금 낱말이 이미 덮는 말·공개 글(요청 글 — 계약 칸 이름은 손잡이라 남김)에 있는 말·
다른 항목과 겹치는 말(단어 단위로 한쪽이 다른 쪽을 덮음)을 뺀다.
첫 풀이(판 2 opus) 전에 만들어 고정하고, 막힘이 나왔을 때만 판 1·4 진행자에 더한다 (`prepare --synonyms`) — 생성기에 동의어 확장 단계를 더하는 시뮬레이션.

  uv run python -m eval.pilot.expand_terms --mid <재료>
"""

from __future__ import annotations

import argparse
import asyncio
import json

from eval.pilot.model import ROOT, Material, facilitator_items, load_material
from task_spec.config import Settings
from task_spec.llm.factory import build_llm_client
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import ExpandTermsInput, ExpandTermsOutput
from task_spec.spec.feature import too_general
from task_spec.starter.answer_rule import is_clarification, term_in
from task_spec.starter.facilitator import Answer

MAX_TERMS = 6


def items_text(items: list[Answer]) -> str:
    return "\n".join(
        f"[{i.id}] 주제: {i.topic} / 지금 낱말: {', '.join(i.match_terms)}" for i in items
    )


def _overlap(a: str, b: str) -> bool:
    """한쪽 낱말의 단어가 다른 쪽에 모두 들어 있으면, 큰 쪽에 걸리는 질문은 두 항목에 다 걸린다 (판 1·4 단어 단위 맞추기)."""
    return term_in(a, b) or term_in(b, a)


def clean(
    items: list[Answer], proposed: dict[str, list[str]], public: str
) -> tuple[dict[str, list[str]], dict[str, list[list[str]]]]:
    """§5.1 정리. (남은 낱말, 뺀 낱말과 사유). 다른 항목의 지금 낱말·제안 낱말과 겹치면 양쪽 다 뺀다 — 순서와 상관없이."""
    by_id = {i.id: i for i in items}
    kept: dict[str, list[str]] = {}
    dropped: dict[str, list[list[str]]] = {}

    def drop(k: str, t: str, why: str) -> None:
        dropped.setdefault(k, []).append([t, why])

    first: dict[str, list[str]] = {}
    for k, terms in proposed.items():
        if k not in by_id:
            continue
        for t in dict.fromkeys(x.strip() for x in terms if x.strip()):
            if is_clarification(k) and too_general(t):  # 두 글자 필터는 추가 요구(c#)에만
                drop(k, t, "일반어")
            elif any(
                term_in(u, t) for u in by_id[k].match_terms
            ):  # 지금 낱말이 이미 다 덮음 — 더 좁은 말
                drop(k, t, "지금 낱말이 덮음")
            elif term_in(t, public):
                drop(k, t, "공개 글에 있음")
            else:
                first.setdefault(k, []).append(t)
    for k, terms in first.items():
        others = [
            u for j, i in by_id.items() if j != k for u in [*i.match_terms, *first.get(j, [])]
        ]
        for t in terms:
            if any(_overlap(t, u) for u in others):
                drop(k, t, "다른 항목과 겹침")
            elif len(kept.get(k, [])) < MAX_TERMS:
                kept.setdefault(k, []).append(t)
            else:
                drop(k, t, "개수 초과")
    return kept, dropped


async def expand(m: Material) -> dict[str, object]:
    items = facilitator_items(m, "new")
    prompt = assemble(
        PromptRegistry(ROOT / "prompts").get("expand_match_terms"),
        ExpandTermsInput(items=items_text(items)),
        ontology_version="eval",
    )
    llm = build_llm_client(Settings())  # type: ignore[call-arg]
    res = await llm.call(prompt, ExpandTermsOutput)
    proposed = {x.item_id: list(x.terms) for x in res.parsed.items}
    # 공개 글 = 요청 글만 — 계약 칸 이름(limit·after·userId 등)은 묻는 손잡이라 빼지 않는다 (§5.1, #151 검토 4차).
    # 사실(f#)은 잘못 열려도 함정이 아니고 놓치면 손해다.
    public = m.request
    kept, dropped = clean(items, proposed, public)
    return {
        "items": kept,
        "proposed": proposed,
        "dropped": dropped,
        "provenance": res.provenance.model_dump(mode="json"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True)
    a = ap.parse_args()
    m = load_material(a.mid)
    out = m.dir / "synonyms.json"
    if out.exists():
        raise SystemExit(f"이미 있음: {out} — 고정한 목록은 다시 만들지 않는다")
    data = asyncio.run(expand(m))
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(data["items"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
