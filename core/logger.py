"""
core/logger.py — PDFWala Enterprise V13.0
Structured JSON logging with automatic job_id injection.

Usage:
    from core.logger import log
    
    log.info("compress_start", job_id="abc123", file_size_mb=5.2)
    log.error("ghostscript_failed", job_id="abc123", rc=1, stderr="...")

Every log entry is a JSON object with:
    - timestamp (ISO 8601 UTC)
    - level (INFO/WARNING/ERROR/DEBUG)
    - message (human-readable)
    - job_id (if provided)
    - any extra kwargs as top-level fields
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class StructuredLogger:
    """JSON-structured logger wrapping Python's logging module."""

    def __init__(self, name: str = "pdfwala", level: int = logging.INFO):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        self._logger.propagate = False

        # Console handler — JSON to stdout (Docker/Gunicorn captures this)
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(level)
            handler.setFormatter(_JSONFormatter())
            self._logger.addHandler(handler)

    def _log(self, level: str, message: str, job_id: Optional[str] = None, **extra):
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        }
        if job_id:
            entry["job_id"] = job_id
        if extra:
            entry.update(extra)

        log_fn = getattr(self._logger, level.lower())
        log_fn(json.dumps(entry, default=str))

    def debug(self, message: str, job_id: str = None, **extra):
        self._log("DEBUG", message, job_id, **extra)

    def info(self, message: str, job_id: str = None, **extra):
        self._log("INFO", message, job_id, **extra)

    def warning(self, message: str, job_id: str = None, **extra):
        self._log("WARNING", message, job_id, **extra)

    def error(self, message: str, job_id: str = None, **extra):
        self._log("ERROR", message, job_id, **extra)

    def exception(self, message: str, job_id: str = None, **extra):
        """Log an ERROR with full traceback."""
        import traceback
        extra["traceback"] = traceback.format_exc()
        self._log("ERROR", message, job_id, **extra)


class _JSONFormatter(logging.Formatter):
    """Minimal formatter — the StructuredLogger already builds JSON.
       This just ensures raw log calls also get structured output."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "job_id"):
            entry["job_id"] = record.job_id
        return json.dumps(entry, default=str)


# ── Module-level singleton ─────────────────────────────────────────────────
log = StructuredLogger("pdfwala")


# ── Convenience: direct function calls ─────────────────────────────────────
def info(msg: str, job_id: str = None, **extra):
    log.info(msg, job_id, **extra)


def warning(msg: str, job_id: str = None, **extra):
    log.warning(msg, job_id, **extra)


def error(msg: str, job_id: str = None, **extra):
    log.error(msg, job_id, **extra)


def exception(msg: str, job_id: str = None, **extra):
    log.exception(msg, job_id, **extra)
