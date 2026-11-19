"""모델 JSON에서 첫 필드인 질문만 스트리밍한다. 사례 JSON은 검증 후에만 전달한다. 포크 interview_stream.py 그대로 (#262).

질문 조각은 가명화 입력으로 만든 LLM 글이다. 저장·확정되는 질문은 서비스가 따로 검사한다(패턴 탐지기·위조 토큰).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

from langchain_core.utils.json import parse_partial_json

_QUESTION = re.compile(r'^\s*(?:```(?:json)?\s*)?\{\s*"question"\s*:\s*')


class QuestionStream:
    def __init__(self, emit: Callable[[str, dict], Awaitable[None]]) -> None:
        self.emit = emit
        self.buffer = ""
        self.finished = False
        self.text = ""

    async def __call__(self, chunk: str | None) -> None:
        if chunk is None:
            self.buffer, self.text, self.finished = "", "", False
            await self.emit("assistant", {"text": ""})
            return
        if self.finished:
            return
        self.buffer += chunk
        if len(self.buffer) > 128_000:
            self.finished = True
            return
        match = _QUESTION.match(self.buffer)
        if not match:
            return
        value = self.buffer[match.end() :]
        try:
            question, _ = json.JSONDecoder().raw_decode(value)
            self.finished = True
        except ValueError:
            try:
                question = parse_partial_json('{"question":' + value).get("question", "")
            except (ValueError, AttributeError):
                return
        if not isinstance(question, str) or len(question) > 1000:
            return
        # 유니코드 이스케이프가 덜 도착했을 때 한쪽 서로게이트를 전송하지 않는다.
        if any(0xD800 <= ord(char) <= 0xDFFF for char in question):
            return
        if question != self.text:
            self.text = question
            await self.emit("assistant", {"text": question})
