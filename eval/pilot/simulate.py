"""질문 목록으로 진행자 답 규칙 점검 — 사실 낱말 다듬기용 (docs/design/analysis-stage.md §9.1 재료, #151 검토 4차).

재료 폴더 `questions/<목록>.json` = [{"q": "질문", "about": "f1" | ["f1", "f2"] | null}] — about 은 그 질문이 묻는 항목
(한 id = 한 주제 질문, 여러 id = 여러 주제 질문, null = 주제 밖). about 은 답 규칙을 돌리기 전에 붙인다.
질문마다 처음 묻는 것으로(상태 없음) §4.6 답 규칙에 넣고 센다:
- 한 주제 질문: 맞는 항목의 구체 답 / 맞는 항목의 모호한 답 / 정보 없음 / 한 가지씩 / 다른 항목이 열림
- 여러 주제 질문: 한 가지씩 / 그 밖 — 기록만
- 주제 밖 질문: 정보 없음 / 잘못 열림 — 기록만
판정(`passed`)은 요청자 지식(f#) 한 항목만 묻는 질문으로만 — 구체 답 ≥ 80%, 한 가지씩 ≤ 10% (§9.1 질문 목록).
추가 요구(c#)는 생성기가 만든 그대로 쓰고 손으로 다듬지 않으므로 기준에 넣지 않고 따로 기록한다.
튜닝용 목록(tune)으로 사실 낱말을 다듬고, 다른 에이전트가 따로 쓴 확인용 목록(check)으로 고정 전에 한 번만 잰다 — 한 목록에 맞춰지지 않게.

  uv run python -m eval.pilot.simulate --mid <재료> --set tune [--synonyms]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable
from typing import Any

from eval.pilot.llm_facilitator import Chooser, apply_choice
from eval.pilot.model import Material, facilitator_items, load_material
from task_spec.schemas import AskedItem
from task_spec.starter.answer_rule import NO_INFO, Reply, answer, is_clarification
from task_spec.starter.facilitator import Answer

CORRECT_MIN = 0.8  # 한 주제 질문 중 정답 항목의 구체 답 비율 하한 (§9.1, 확인용 목록 기준)
ONE_AT_A_TIME_MAX = 0.1  # 한 주제 질문 중 "한 가지씩" 비율 상한


VAGUE = "vague"  # 특정 항목을 콕 집지 않는 넓은 질문 — 구체 답이 열리면 안 된다 (2026-11-13 사용자: 모호하게 물어 여럿 여는 것 조심)
VAGUE_OPEN_MAX = 0.1  # 모호한 질문 중 구체 답이 열리는 비율 상한 [제안]


def classify(about: str | list[str] | None, r: Reply) -> str:
    """about — 한 항목 id(한 주제 질문) / 여러 id(여러 주제 질문) / None(주제 밖) / "vague"(넓은 질문)."""
    if about == VAGUE:
        opened = r.layer == "answer" and r.item_id is not None and not r.repeat
        return "모호·구체 열림" if opened else "모호·막음"
    if about is None:
        return "주제 밖·정보 없음" if r.text == NO_INFO else "주제 밖·잘못 열림"
    if isinstance(about, list):
        return "여러 주제·한 가지씩" if r.rule == 1 else "여러 주제·그 밖"
    if r.rule == 1:
        return "한 가지씩"
    if r.text == NO_INFO:
        return "정보 없음"
    if r.item_id != about:
        return "다른 항목"
    return "구체 답" if r.layer == "answer" else "모호한 답"


def _rate(rows: list[dict[str, Any]], result: str) -> float | None:
    return (sum(x["result"] == result for x in rows) / len(rows)) if rows else None


def run(
    items: list[Answer],
    questions: list[dict[str, Any]],
    request: str = "",
    choose: Callable[[str], list[AskedItem]] | None = None,
) -> dict[str, Any]:
    """질문마다 처음 묻는 것으로(상태 없음) 답 규칙에 넣고 센다. 판정은 한 주제 질문으로 — 정답 항목 구체 답 ≥ CORRECT_MIN,
    "한 가지씩" ≤ ONE_AT_A_TIME_MAX. 정보 없음·다른 항목·주제 밖 잘못 열림은 같이 기록한다.
    choose 를 주면 낱말 규칙 대신 그것(진행자 LLM — 응시 플랫폼 흉내)이 항목을 고른다."""
    rows = []
    for q in questions:
        r = (
            apply_choice(choose(q["q"]), items, {}, legacy=False)
            if choose is not None
            else answer(q["q"], items, {}, request)
        )
        rows.append(
            {
                "q": q["q"],
                "about": q.get("about"),
                "result": classify(q.get("about"), r),
                "item": r.item_id,
                "rule": r.rule,
            }
        )
    vague = [x for x in rows if x["about"] == VAGUE]
    one = [x for x in rows if isinstance(x["about"], str) and x["about"] != VAGUE]
    single = [x for x in one if not is_clarification(x["about"])]  # 판정 — 사실(f#) 한 항목 질문
    clar = [x for x in one if is_clarification(x["about"])]  # 추가 요구(c#) — 기록만
    names = ("구체 답", "모호한 답", "정보 없음", "한 가지씩", "다른 항목")
    rates = {name: _rate(single, name) for name in names}
    off = [x for x in rows if x["about"] is None]
    correct, broad = rates["구체 답"], rates["한 가지씩"]
    return {
        "counts": dict(Counter(x["result"] for x in rows)),
        "single_topic": len(single),
        "rates": rates,
        "clarification_rates": {name: _rate(clar, name) for name in names},
        "vague_open_rate": _rate(vague, "모호·구체 열림"),
        "vague_passed": (v := _rate(vague, "모호·구체 열림")) is not None and v <= VAGUE_OPEN_MAX,
        "off_topic_wrong_open": _rate(off, "주제 밖·잘못 열림"),
        "passed": correct is not None
        and broad is not None
        and correct >= CORRECT_MIN
        and broad <= ONE_AT_A_TIME_MAX,
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True)
    ap.add_argument("--set", required=True, help="questions/<목록>.json")
    ap.add_argument("--synonyms", action="store_true", help="고정한 동의어 목록을 더해서")
    ap.add_argument(
        "--llm", action="store_true", help="진행자 LLM 이 고른다 (응시 플랫폼과 같게, 판 1·4)"
    )
    ap.add_argument(
        "--prompt-version", type=int, default=None, help="facilitator_choose 판 (기본 CURRENT)"
    )
    a = ap.parse_args()
    m: Material = load_material(a.mid)
    qs = json.loads((m.dir / "questions" / f"{a.set}.json").read_text("utf-8"))
    items = facilitator_items(m, "new", m.synonyms() if a.synonyms else None)
    choose = None
    if a.llm:
        chooser = Chooser(version=a.prompt_version)

        def choose(q: str) -> list[AskedItem]:
            return chooser.choose_sync(q, items, False, m.request)

    out = run(items, qs, m.request, choose)
    suffix = (
        ("-syn" if a.synonyms else "")
        + ("-llm" if a.llm else "")
        + (f"-v{a.prompt_version}" if a.llm and a.prompt_version else "")
    )
    (m.dir / "questions" / f"{a.set}{suffix}.result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), "utf-8"
    )
    for x in out["rows"]:
        print(f"  {x['result']:10s} {str(x['item']):4s} ← [{x['about']}] {x['q']}")
    keys = (
        "counts",
        "single_topic",
        "rates",
        "clarification_rates",
        "vague_open_rate",
        "off_topic_wrong_open",
        "passed",
    )
    print(json.dumps({k: out[k] for k in keys}, ensure_ascii=False))


if __name__ == "__main__":
    main()
