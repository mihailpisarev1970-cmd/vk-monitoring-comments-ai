"""Тесты логирования."""

from __future__ import annotations

import logging

from app.core import logging as app_logging


def test_setup_logging_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_logging, "LOGS_DIR", tmp_path)
    app_logging.setup_logging(level="DEBUG", log_to_file=False)
    root = logging.getLogger()
    count_first = len(root.handlers)

    app_logging.setup_logging(level="INFO", log_to_file=False)
    assert len(logging.getLogger().handlers) == count_first
    assert count_first >= 1
    assert logging.getLogger().level == logging.INFO


def test_logger_writes_to_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_logging, "LOGS_DIR", tmp_path)
    app_logging.setup_logging(level="INFO", log_to_file=True)
    logger = app_logging.get_logger("test.file")
    logger.info("hello-from-test")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "hello-from-test" in content
