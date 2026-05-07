"""
core/logging_setup.py — VALUE / QEEMA v22.5 — logging configuration
=============================================================
Centralized logging configuration.

[Features]
- Structured JSON logs to file (parseable by log aggregators)
- Pretty colored output to console
- Episode/stage context injection via LoggerAdapter
- Log rotation (10MB × 5 files)
- Optional fallback if python-json-logger missing
"""
from __future__ import annotations

import logging
import logging.config
import os
import sys
from pathlib import Path
from typing import Any, Mapping


# ════════════════════════════════════════════════════════════════
# JSON logger detection
# ════════════════════════════════════════════════════════════════
try:
    from pythonjsonlogger import jsonlogger  # type: ignore
    _HAS_JSON_LOGGER: bool = True
except ImportError:
    _HAS_JSON_LOGGER = False


# ════════════════════════════════════════════════════════════════
# LoggerAdapter for stage/episode context
# ════════════════════════════════════════════════════════════════
class ContextLogAdapter(logging.LoggerAdapter):
    """
    Injects extra context (episode_number, stage) into every log record.

    Usage:
        log = ContextLogAdapter(
            logging.getLogger(__name__),
            {"episode_number": 5, "stage": "audio"},
        )
        log.info("synthesizing intro")
        # Output: [ep005|audio] synthesizing intro
    """

    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        ep = self.extra.get("episode_number") if self.extra else None
        stage = self.extra.get("stage") if self.extra else None
        prefix_parts = []
        if ep is not None:
            prefix_parts.append(f"ep{ep:03d}")
        if stage:
            prefix_parts.append(stage)
        prefix = f"[{('|').join(prefix_parts)}] " if prefix_parts else ""
        return f"{prefix}{msg}", kwargs


# ════════════════════════════════════════════════════════════════
# Setup function
# ════════════════════════════════════════════════════════════════
def setup_logging(
    logs_dir: Path,
    *,
    level: str = "INFO",
    use_json_file: bool = True,
) -> None:
    """
    Configure root logger.

    Args:
        logs_dir: directory for log files (will be created if missing)
        level: root logger level (DEBUG, INFO, WARNING, ERROR)
        use_json_file: if True and python-json-logger is installed,
                       file handler emits JSON
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(logs_dir, os.W_OK):
        raise RuntimeError(f"Logs directory not writable: {logs_dir}")

    formatters: dict[str, Any] = {
        "console": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "file_text": {
            "format": (
                "%(asctime)s [%(levelname)s] %(name)s "
                "(%(filename)s:%(lineno)d) - %(message)s"
            ),
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    }
    file_formatter_name = "file_text"

    if use_json_file and _HAS_JSON_LOGGER:
        formatters["json"] = {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": (
                "%(asctime)s %(levelname)s %(name)s "
                "%(message)s %(filename)s %(lineno)d %(process)d"
            ),
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
            "rename_fields": {
                "levelname": "level",
                "asctime": "timestamp",
                "name": "logger",
                "filename": "file",
                "lineno": "line",
            },
        }
        file_formatter_name = "json"

    config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": formatters,
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": level,
                "formatter": "console",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "DEBUG",
                "formatter": file_formatter_name,
                "filename": str(logs_dir / "pipeline.log"),
                "maxBytes": 10 * 1024 * 1024,  # 10 MB
                "backupCount": 5,
                "encoding": "utf-8",
            },
        },
        "root": {
            "level": level,
            "handlers": ["console", "file"],
        },
        # Quiet noisy libraries
        "loggers": {
            "urllib3": {"level": "WARNING"},
            "asyncio": {"level": "WARNING"},
            "playwright": {"level": "WARNING"},
            "google_auth_httplib2": {"level": "WARNING"},
        },
    }

    try:
        logging.config.dictConfig(config)
    except Exception as e:
        # Fallback to plaintext if JSON config fails
        if file_formatter_name == "json":
            config["handlers"]["file"]["formatter"] = "file_text"
            config["formatters"].pop("json", None)
            logging.config.dictConfig(config)
            logging.getLogger(__name__).warning(
                f"⚠️ JSON logger failed to load; falling back to text. Error: {e}"
            )
        else:
            print(f"❌ Logging setup failed: {e}", file=sys.stderr)
            raise


def with_context(
    logger: logging.Logger,
    **context: Any,
) -> ContextLogAdapter:
    """Convenience: wrap logger with context dict."""
    return ContextLogAdapter(logger, context)
