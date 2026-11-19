"""Конфигурация приложения.

Все значения читаются из переменных окружения (.env в корне проекта).
Секреты и идентификаторы (токены, модели, DSN) не зашиваются в код —
ТЗ п.3.1, п.12.1, п.14.1, п.15.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из .env."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- Application ----------
    app_name: str = "VK Monitor Assistant"
    app_env: str = "local"
    log_level: str = "INFO"
    timezone: str = "Europe/Moscow"

    # ---------- VK ----------
    vk_app_id: int = 0
    vk_access_token: SecretStr = SecretStr("")
    vk_api_version: str = "5.199"

    # ---------- LLM (AiDostup) ----------
    aidostup_base_url: str = "https://aidostup.site/v1"
    aidostup_api_key: SecretStr = SecretStr("")
    aidostup_model: str = ""
    aidostup_comment_model: str = ""

    # ---------- Telegram ----------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    # ---------- PostgreSQL ----------
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str = "vk_monitor"
    postgres_user: str = "vk_monitor"
    postgres_password: SecretStr = SecretStr("vk_monitor_pass")
    database_url: str = "postgresql+asyncpg://vk_monitor:vk_monitor_pass@127.0.0.1:5432/vk_monitor"

    # ---------- Redis ----------
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ---------- Paths ----------
    base_dir: Path = Field(default=BASE_DIR)
    config_dir: Path = Field(default=BASE_DIR / "config")

    # ---------- Derived ----------
    @property
    def comment_model(self) -> str:
        """Модель для генерации комментариев (fallback на основную модель)."""
        return self.aidostup_comment_model or self.aidostup_model

    @property
    def sync_database_url(self) -> str:
        """Синхронный DSN для Alembic (без async-драйвера)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")

    @property
    def is_test(self) -> bool:
        return self.app_env.lower() in {"test", "testing", "ci"}

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("database_url")
    @classmethod
    def _require_async_dsn(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL должен использовать async-драйвер: "
                "postgresql+asyncpg://user:pass@host:port/db"
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает singleton настроек."""
    return Settings()


settings = get_settings()
