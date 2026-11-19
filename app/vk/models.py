"""Внутренние модели нормализованных публикаций (ТЗ п.6.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.db.enums import AdFlag


class MarkerQuery(BaseModel):
    """Один поисковый запрос из config/markers.json."""

    model_config = ConfigDict(frozen=True)

    marker_name: str = Field(min_length=1, max_length=255)
    query: str = Field(min_length=1, max_length=255)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.marker_name}:{self.query}"


class AuthorInfo(BaseModel):
    """Автор публикации (сообщество или пользователь)."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str | None = None
    screen_name: str | None = None
    is_group: bool = False


class NormalizedPost(BaseModel):
    """Публикация, приведённая к единому внутреннему формату.

    Соответствует требованиям ТЗ п.6.2: id, owner_id, author_id, text,
    published_at, post_url, метрики, данные автора, признаки рекламы,
    найденный маркер и исходный payload.
    """

    model_config = ConfigDict(extra="ignore")

    owner_id: int
    post_id: int
    author_id: int | None = None
    author_name: str | None = None
    text: str = ""
    published_at: datetime
    post_url: str

    comments_count: int | None = None
    likes_count: int | None = None
    reposts_count: int | None = None
    views_count: int | None = None

    # Признаки официальной рекламы (ТЗ п.8)
    marked_as_ads: bool | None = None
    ad_flag: AdFlag = AdFlag.UNKNOWN
    post_type: str | None = None
    ad_details: dict[str, Any] = Field(default_factory=dict)

    # Дедупликация (ТЗ п.11.4)
    text_hash: str | None = None

    raw_payload: dict[str, Any] = Field(default_factory=dict)
    matched_markers: list[MarkerQuery] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def key(self) -> str:
        """Составной ключ публикации: `owner_id_post_id`."""
        return f"{self.owner_id}_{self.post_id}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_official_ad(self) -> bool:
        """Официальная реклама VK — безусловное исключение (ТЗ п.8)."""
        return self.ad_flag is AdFlag.YES

    def add_marker(self, marker: MarkerQuery) -> None:
        """Добавляет маркер без дублирования пары (name, query)."""
        if marker not in self.matched_markers:
            self.matched_markers.append(marker)

    @property
    def marker_names(self) -> list[str]:
        """Уникальные названия тематик, по которым найдена публикация."""
        seen: list[str] = []
        for marker in self.matched_markers:
            if marker.marker_name not in seen:
                seen.append(marker.marker_name)
        return seen


@dataclass(slots=True)
class SearchWindow:
    """Временное окно поиска (rolling window).

    Так как `start_from` недоступен с сервисным ключом (см.
    docs/vk_api_check.md), выборка выполняется окнами с перекрытием.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError("SearchWindow: начало окна должно быть раньше окончания")

    @property
    def start_ts(self) -> int:
        return int(self.start.timestamp())

    @property
    def end_ts(self) -> int:
        return int(self.end.timestamp())

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(slots=True)
class SearchResult:
    """Результат сбора: публикации и диагностика по каждому запросу."""

    posts: list[NormalizedPost] = field(default_factory=list)
    queries_total: int = 0
    queries_failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    total_received: int = 0
    duplicates_skipped: int = 0
