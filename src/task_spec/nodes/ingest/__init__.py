"""② 입력 처리 (document-ingest 스킬). 이 패키지는 LLM·임베딩·외부 API 를 import 하지 않는다 (불변식 2)."""

from __future__ import annotations

from task_spec.nodes.ingest.node import FileIngestor, Ingestor, make_ingest, stub_ingestor

__all__ = ["FileIngestor", "Ingestor", "make_ingest", "stub_ingestor"]
