"""Настройка логирования (единый формат для консоли и файла)."""

from __future__ import annotations

import contextlib
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOGS_DIR = settings.base_dir / "logs"


def setup_logging(level: str | None = None, log_to_file: bool = True) -> None:
    """Инициализирует корневой логгер.

    Идемпотентна: повторный вызов не добавляет дублирующиеся обработчики.
    """
    # UTF-8 в stdout (Windows) — чтобы кириллица в логах не превращалась в кракозябры
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")

    root = logging.getLogger()
    log_level = getattr(logging, (level or settings.log_level).upper(), logging.INFO)
    root.setLevel(log_level)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_to_file:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            Path(LOGS_DIR) / "app.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Шумные библиотеки — только предупреждения
    for noisy in ("httpx", "httpcore", "aiogram.event", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Удобный хелпер для получения логгера модуля."""
    return logging.getLogger(name)
