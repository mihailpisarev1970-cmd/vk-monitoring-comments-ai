"""Тесты конфигурации."""

from __future__ import annotations

import pytest

from app.core.config import Settings


def test_settings_loads_from_env_file() -> None:
    s = Settings()
    assert s.vk_api_version.startswith("5.")
    assert s.timezone == "Europe/Moscow"
    assert s.database_url.startswith("postgresql+asyncpg://")


def test_secrets_are_not_leaked_in_repr() -> None:
    s = Settings(vk_access_token="supersecret", aidostup_api_key="sk-test")
    assert "supersecret" not in repr(s)
    assert s.vk_access_token.get_secret_value() == "supersecret"


def test_comment_model_fallback() -> None:
    s = Settings(aidostup_model="main-model", aidostup_comment_model="")
    assert s.comment_model == "main-model"

    s2 = Settings(aidostup_model="main-model", aidostup_comment_model="comment-model")
    assert s2.comment_model == "comment-model"


def test_database_url_requires_async_driver() -> None:
    with pytest.raises(ValueError):
        Settings(database_url="postgresql://user:pass@host:5432/db")


def test_sync_database_url() -> None:
    s = Settings(database_url="postgresql+asyncpg://u:p@h:5432/d")
    assert s.sync_database_url == "postgresql://u:p@h:5432/d"


def test_log_level_uppercased() -> None:
    assert Settings(log_level="debug").log_level == "DEBUG"
