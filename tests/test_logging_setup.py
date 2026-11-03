"""로깅 설정 — 앱 로거가 실제로 출력되고, json 형식에 원문·스택이 들어가지 않는다."""

from __future__ import annotations

import json
import logging

from task_spec.logging_setup import JsonFormatter, configure_logging


def test_app_logger_emits_at_info(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging("INFO", "text")
    logging.getLogger("task_spec.test").info("node done run_id=r1 node=draft_spec ms=10")
    logging.getLogger("httpx").info("HTTP Request: POST https://example.invalid")  # 조용히
    err = capsys.readouterr().err
    assert "node done run_id=r1" in err and "example.invalid" not in err


def test_json_format_has_no_exception_text() -> None:
    try:
        raise ValueError("가상 원문 조각")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            "task_spec.x", logging.ERROR, __file__, 1, "run failed", (), sys.exc_info()
        )
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "run failed" and out["exc"] == "ValueError"
    assert "가상 원문" not in json.dumps(out, ensure_ascii=False)
