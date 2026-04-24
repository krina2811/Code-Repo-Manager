"""
Centralized logging configuration.

Usage in any module:
    from core.logger import get_logger
    logger = get_logger(__name__)
"""

import logging
import logging.handlers
import sys
from pathlib import Path


_initialized = False


def setup_logging(log_level: str = "INFO", log_file: str = "./logs/app.log") -> None:
    """
    Configure root logger once at application startup.
    Safe to call multiple times — only initializes on the first call.

    In production (LOG_TO_FILE=false), logs go to stdout only so Docker
    captures them via `docker logs` — no disk filling, no lost logs on restart.
    In development, logs also write to a rotating file for easy inspection.
    """
    import os
    global _initialized
    if _initialized:
        return
    _initialized = True

    level = getattr(logging, log_level.upper(), logging.INFO)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — always on (Docker captures stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console_handler)

    # File handler — only in development (LOG_TO_FILE=true in .env)
    # In production on EC2, keep LOG_TO_FILE unset or false
    if os.getenv("LOG_TO_FILE", "false").lower() == "true":
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level)
        root.addHandler(file_handler)

    # Silence noisy third-party loggers
    for noisy in ("uvicorn.access", "watchdog", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. setup_logging() must be called before first use."""
    return logging.getLogger(name)
