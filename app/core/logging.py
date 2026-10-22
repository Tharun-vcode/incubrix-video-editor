from __future__ import annotations

from contextvars import ContextVar
import json
import logging
from datetime import UTC, datetime
from typing import Any


correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")
job_id_var: ContextVar[str] = ContextVar("job_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "job_id": getattr(record, "job_id", job_id_var.get()),
            "x_correlation_id": getattr(
                record, "correlation_id", correlation_id_var.get()
            ),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key in ("event", "session_id", "status_code"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
