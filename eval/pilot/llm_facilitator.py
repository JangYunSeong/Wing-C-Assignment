"""손 시범 진행자 LLM — 응시 플랫폼처럼 LLM 이 질문이 묻는 항목을 고른다 (docs/design/analysis-stage.md §4.6·§9.1).

응시 플랫폼의 진행자는 질문을 LLM(gemini-3.8-flash)으로 받는다 (2026-11-13 사용자). 손 시범도 같은 모델로 흉내 낸다.
- LLM 은 질문이 묻는 항목과 구체 답 조건 해당 여부만 고른다 (`prompts/facilitator_choose`). 답 문장은 코드가 승인된 문장을
  그대로 낸다 — "고르기만, 문장 안 바꿈"(2026-11-06 확정)이 구조로 지켜진다.
- 항목 상태(없음 → 모호 → 열림)·한 가지씩·반복·적중은 코드가 정한다 (§4.6 과 같게). LLM 판단은 상태와 무관하므로 캐시한다 —
  같은 질문(같은 규칙 글·요청 글·항목)에는 늘 같은 답 (손 시범 안의 재현성).
- 판 1·4: 새 규칙 글(`ANSWER_RULE_ASK`) + 항목의 구체 조건 문장(ask_when, 없으면 매칭 낱말). 판 3: 지금 규칙 글(`ANSWER_RULE`) + 지금 자료.

입력은 AI 풀이의 질문과 공고로 만든 과제 자료뿐 — 지원자 데이터 없음.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import SecretStr

from eval.pilot.model import DATA, ROOT
from task_spec.llm.client import LLMClient
from task_spec.llm.openai_compat import OpenAICompatBackend
from task_spec.prompting import PromptRegistry, assemble
from task_spec.schemas import AskedItem, FacilitatorChooseInput, FacilitatorChooseOutput
from task_spec.starter.answer_rule import NO_INFO, ONE_AT_A_TIME, Reply, State, is_clarification
from task_spec.starter.facilitator import ANSWER_RULE, ANSWER_RULE_ASK, Answer

TASK = "facilitator_choose"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
CACHE = DATA / "llm-facilitator-cache.json"


def render_items(items: Sequence[Answer], legacy: bool) -> str:
    """LLM 에 주는 항목 — 플랫폼 보기(FacilitatorView)의 칸 그대로. 판 3(legacy)은 지금 자료 모양(매칭 낱말)."""
    lines: list[str] = []
    for i in items:
        lines.append(f"[{i.id}]")
        lines.append(f"주제(topic): {i.topic}")
        if not legacy and i.ask_when:
            lines.append(f"구체 조건(ask_when): {i.ask_when}")
        elif legacy:
            lines.append(f"매칭 낱말(match_terms): {', '.join(i.match_terms) or '(없음)'}")
        else:
            lines.append(
                f"매칭 낱말(match_terms, 예시 표현): {', '.join(i.match_terms) or '(없음)'}"
            )
        lines.append(f"answer: {i.answer}")
        lines.append(f"answer_hint: {i.answer_hint if i.answer_hint is not None else '(없음)'}")
        lines.append("")
    return "\n".join(lines).strip()


def cache_key(prompt_ref: str, rule: str, request: str, items_text: str, question: str) -> str:
    raw = json.dumps(
        [prompt_ref, rule, request, items_text, " ".join(question.split())], ensure_ascii=False
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _gemini_key() -> str:
    key = dotenv_values(ROOT / ".env").get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY 가 .env·환경 변수에 없습니다")
    return key


def gemini_client(timeout: float = 120.0) -> LLMClient:
    return LLMClient(
        {
            "gemini": OpenAICompatBackend(
                api_key=SecretStr(_gemini_key()), base_url=GEMINI_BASE, timeout=timeout
            )
        }
    )


class Chooser:
    """질문 → 묻는 항목. 캐시를 파일에 남긴다 (값마다 provenance — 불변식 8)."""

    def __init__(
        self, llm: LLMClient | None = None, cache: Path = CACHE, version: int | None = None
    ) -> None:
        self.template = PromptRegistry(ROOT / "prompts").get(TASK, version)  # None = CURRENT
        model = dotenv_values(ROOT / ".env").get("GEMINI_MODEL") or os.environ.get("GEMINI_MODEL")
        if model and f"gemini:{model}" != self.template.meta.model:
            raise SystemExit(
                f"GEMINI_MODEL({model}) 과 프롬프트 모델({self.template.meta.model})이 다름 — 플랫폼과 맞춘 새 판이 필요"
            )
        self.llm = llm
        self.path = cache
        self._lock = threading.Lock()
        # 호출마다 asyncio.run 으로 루프를 새로 열면 LLM 클라이언트의 연결이 닫힌 루프에 묶여 깨진다 —
        # 루프 하나를 백그라운드 스레드에 두고 모든 호출을 거기서 돌린다 (진행자 서버는 스레드마다 요청을 받는다)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = (
            json.loads(cache.read_text("utf-8")) if cache.exists() else {}
        )

    async def choose(
        self, question: str, items: Sequence[Answer], legacy: bool, request: str
    ) -> list[AskedItem]:
        rule = ANSWER_RULE if legacy else ANSWER_RULE_ASK
        items_text = render_items(items, legacy)
        req = request or "(요청 글 없음)"
        # 판 번호만이 아니라 내용 해시까지 — 판을 고쳐 쓰면 예전 판단을 다시 쓰지 않게
        key = cache_key(
            f"{self.template.ref}#{self.template.sha256}", rule, req, items_text, question
        )
        with self._lock:
            hit = self._cache.get(key)
        if hit is None:
            if self.llm is None:
                self.llm = gemini_client()
            prompt = assemble(
                self.template,
                FacilitatorChooseInput(rule=rule, request=req, items=items_text, question=question),
                ontology_version="eval",
            )
            res = await self.llm.call(prompt, FacilitatorChooseOutput)
            hit = {
                "asked": [a.model_dump() for a in res.parsed.asked],
                "provenance": res.provenance.model_dump(mode="json"),
            }
            with self._lock:
                self._cache[key] = hit
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._cache, ensure_ascii=False, indent=1), "utf-8")
        known = {i.id for i in items}
        return [AskedItem.model_validate(a) for a in hit["asked"] if a["item_id"] in known]

    def _running_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=loop.run_forever, name="facilitator-llm", daemon=True
                ).start()
                self._loop = loop
            return self._loop

    def choose_sync(
        self, question: str, items: Sequence[Answer], legacy: bool, request: str
    ) -> list[AskedItem]:
        fut = asyncio.run_coroutine_threadsafe(
            self.choose(question, items, legacy, request), self._running_loop()
        )
        return fut.result()


def apply_choice(
    asked: Sequence[AskedItem], items: Sequence[Answer], states: Mapping[str, State], legacy: bool
) -> Reply:
    """LLM 이 고른 항목 → 진행자 답 (승인된 문장 그대로). 새 규칙은 §4.6 의 상태·한 가지씩·반복·적중을, 판 3 은 지금 규칙을 따른다."""
    by_id = {i.id: i for i in items}
    picked = list(dict.fromkeys(a.item_id for a in asked if a.item_id in by_id))
    concrete = {a.item_id: a.concrete for a in asked if a.item_id in by_id}
    if legacy:  # 지금 규칙 — 상태·한 가지씩 없음, 적중을 세지 않음
        if not picked:
            return Reply(text=NO_INFO, rule=3, layer="none")
        k = picked[0]
        item = by_id[k]
        if concrete[k]:
            return Reply(text=item.answer, rule=1, layer="answer", item_id=k, candidates=picked)
        hint = item.answer_hint is not None
        return Reply(
            text=item.answer_hint if hint else item.answer,  # type: ignore[arg-type]
            rule=2,
            layer="hint" if hint else "answer",
            item_id=k,
            candidates=picked,
        )
    state: dict[str, State] = {i.id: states.get(i.id, "none") for i in items}
    if not picked:  # 5. 어느 항목도 묻지 않음
        return Reply(text=NO_INFO, rule=5, layer="none", hit=0.0, states=state)
    closed = [k for k in picked if state[k] != "open"]
    if len(closed) >= 2:  # 1. 안 열린 항목 둘 이상 — 한 가지씩
        return Reply(
            text=ONE_AT_A_TIME,
            rule=1,
            layer="one_at_a_time",
            hit=0.0,
            candidates=closed,
            states=state,
        )
    if not closed:  # 3. 열린 항목만 — 같은 구체 답을 다시
        k = picked[0]
        return Reply(
            text=by_id[k].answer,
            rule=3,
            layer="answer",
            item_id=k,
            repeat=True,
            candidates=picked,
            states=state,
        )
    k = closed[0]
    item = by_id[k]
    if concrete[k]:  # 2. 구체 조건에 해당 — 구체 답, 열림
        return Reply(
            text=item.answer,
            rule=2,
            layer="answer",
            item_id=k,
            hit=1.0,
            candidates=[k],
            states={**state, k: "open"},
        )
    # 4. 주제만 — 모호한 답 (§4.6 (iv): 모호한 답이 없는 f# 은 주제만으로 열지 않는다)
    if state[k] == "hint":
        return Reply(
            text=item.answer_hint or item.answer,
            rule=4,
            layer="hint",
            item_id=k,
            repeat=True,
            candidates=[k],
            states=state,
        )
    if item.answer_hint is not None:
        return Reply(
            text=item.answer_hint,
            rule=4,
            layer="hint",
            item_id=k,
            hit=0.5,
            candidates=[k],
            states={**state, k: "hint"},
        )
    if is_clarification(k):
        return Reply(
            text=item.answer,
            rule=4,
            layer="answer",
            item_id=k,
            hit=0.5,
            candidates=[k],
            states={**state, k: "open"},
        )
    return Reply(text=NO_INFO, rule=5, layer="none", hit=0.0, candidates=[k], states=state)
