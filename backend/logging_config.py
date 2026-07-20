import json
import logging
import os
from datetime import datetime, timezone


_EXTRA_FIELDS = (
    "request_id",
    "trace_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "workspace_id",
    "worker_id",
    "job_id",
    "change_request_id",
)
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def logging_configuration_status() -> dict:
    environment = os.getenv("SRE_ENVIRONMENT", "development").strip().lower()
    default_format = "json" if environment == "production" else "text"
    log_format = os.getenv("SRE_LOG_FORMAT", default_format).strip().lower()
    level = os.getenv("SRE_LOG_LEVEL", "INFO").strip().upper()
    return {
        "format": log_format,
        "level": level,
        "valid": log_format in {"json", "text"} and level in _VALID_LEVELS,
    }


def configure_logging() -> None:
    status = logging_configuration_status()
    level = status["level"] if status["level"] in _VALID_LEVELS else "INFO"
    handler = logging.StreamHandler()
    if status["format"] == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
