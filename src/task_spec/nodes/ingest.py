"""② 입력 처리 — 텍스트 추출(PDF 텍스트층 → 스캔본만 OCR) + 칸 분리.

가명화 이전 단계: LLM·임베딩 import 금지 (CLAUDE.md 불변식 2, 훅이 막는다).
TODO(document-ingest): 실제 추출 구현. 지금은 문서 참조를 그대로 넘긴다.
"""

from __future__ import annotations

from typing import Any

from task_spec.schemas import RunState


async def ingest(state: RunState) -> dict[str, Any]:
    return {}
