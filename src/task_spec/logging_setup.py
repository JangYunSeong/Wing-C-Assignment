"""로깅 설정 — 앱 로거(task_spec.*)를 표준 에러로 내보낸다 (podman logs 에 보이게).

규칙 (CLAUDE.md 코딩 규칙): 로그에는 id·해시·길이·코드·시간만. 원문·가명화 매핑·키 값을 넣지 않는다.
LOG_LEVEL(기본 INFO), LOG_FORMAT(text|json). 요청 URL 을 찍는 httpx·openai 로거는 WARNING 으로 낮춘다.
"""

from __future__ import annotations

import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any, Literal

LogFormat = Literal["text", "json"]
_QUIET = ("httpx", "httpcore", "openai", "langchain", "langsmith")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = (
                record.exc_info[0].__name__ if record.exc_info[0] else None
            )  # 메시지·스택 원문은 남기지 않음
        return json.dumps(out, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: LogFormat = "text") -> None:
    formatter: dict[str, Any] = (
        {"()": JsonFormatter}
        if fmt == "json"
        else {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}
    )
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,  # uvicorn 로거는 그대로 둔다
            "formatters": {"app": formatter},
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "formatter": "app",
                    "stream": "ext://sys.stderr",
                }
            },
            "loggers": {
                "task_spec": {"handlers": ["stderr"], "level": level.upper(), "propagate": False},
                **{name: {"level": "WARNING"} for name in _QUIET},
            },
        }
    )
