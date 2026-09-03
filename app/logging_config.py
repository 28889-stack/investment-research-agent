from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Settings


_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|token|authorization|password|secret|cookie)[\"']?"
    r"\s*[:=]\s*)(?:Bearer\s+)?(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;}]+)"
)
_AUTH_HEADER = re.compile(r"(?i)(authorization\s*:\s*)(?:Bearer\s+)?([^\s,;]+)")
_URL_PASSWORD = re.compile(r"(://[^\s:/@]+:)([^\s@]+)(@)")


def redact_sensitive(value: object) -> str:
    text = str(value)
    text = _AUTH_HEADER.sub(r"\1[REDACTED]", text)
    text = _ASSIGNMENT.sub(r"\1[REDACTED]", text)
    return _URL_PASSWORD.sub(r"\1[REDACTED]\3", text)


class StructuredFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        inferred = self.component
        for marker, component in (
            (".runtime", "runtime"),
            (".technical", "technical"),
            (".fundamental", "fundamental"),
            (".tool", "tool"),
            (".ops", "backup"),
        ):
            if marker in record.name:
                inferred = component
                break
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "component": redact_sensitive(getattr(record, "component", inferred)),
            "message": redact_sensitive(record.getMessage()),
            "run_id": _safe_extra(record, "run_id"),
            "workflow": _safe_extra(record, "workflow"),
            "node": _safe_extra(record, "node"),
            "execution_id": _safe_extra(record, "execution_id"),
            "duration_ms": getattr(record, "duration_ms", None),
            "status": _safe_extra(record, "status"),
            "error_type": _safe_extra(record, "error_type"),
            "diagnostic": _safe_extra(record, "diagnostic"),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _safe_extra(record: logging.LogRecord, name: str):
    value = getattr(record, name, None)
    return None if value is None else redact_sensitive(value)


def configure_logging(
    settings: Settings,
    component: str,
    *,
    logger: logging.Logger | None = None,
) -> logging.Logger:
    target = logger or logging.getLogger()
    target.setLevel(getattr(logging, settings.log_level, logging.INFO))
    for handler in list(target.handlers):
        if getattr(handler, "_financial_agent_handler", False):
            target.removeHandler(handler)
            handler.close()
    formatter = StructuredFormatter(component)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._financial_agent_handler = True  # type: ignore[attr-defined]
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(settings.logs_dir) / f"{component}.log"
    rotating = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    rotating.setFormatter(formatter)
    rotating._financial_agent_handler = True  # type: ignore[attr-defined]
    target.addHandler(console)
    target.addHandler(rotating)
    return target
