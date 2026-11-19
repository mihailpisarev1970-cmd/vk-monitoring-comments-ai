"""Загрузка и валидация конфигурации фильтров (ТЗ п.4.1, п.11.5).

Требования:
* изменения в ``config/*.json`` подхватываются без перезапуска;
* при некорректном JSON выдаётся понятная ошибка, и поиск **не выполняется**
  по случайно сформированным запросам;
* регулярные выражения компилируются один раз и переиспользуются.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import settings
from app.core.logging import get_logger
from app.vk.models import MarkerQuery

logger = get_logger(__name__)


class ConfigError(Exception):
    """Ошибка конфигурации: файл отсутствует, некорректен или противоречив."""


# ---------------------------------------------------------------- markers.json
class MarkerEntry(BaseModel):
    """Одна тематика со списком поисковых запросов."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=255)
    queries: list[str] = Field(min_length=1)
    enabled: bool = True

    @field_validator("queries")
    @classmethod
    def _clean_queries(cls, value: list[str]) -> list[str]:
        cleaned = [q.strip() for q in value if q and q.strip()]
        if not cleaned:
            raise ValueError("у маркера должен быть хотя бы один непустой запрос")
        return cleaned


class MarkersConfig(BaseModel):
    """Структура config/markers.json."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    version: int = 1
    markers: list[MarkerEntry] = Field(default_factory=list)

    @field_validator("markers")
    @classmethod
    def _unique_names(cls, value: list[MarkerEntry]) -> list[MarkerEntry]:
        names = [entry.name for entry in value]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"повторяющиеся названия тематик: {sorted(duplicates)}")
        return value

    def active_queries(self) -> list[MarkerQuery]:
        """Плоский список активных пар (тематика, запрос)."""
        if not self.enabled:
            return []
        queries: list[MarkerQuery] = []
        for entry in self.markers:
            if not entry.enabled:
                continue
            for query in entry.queries:
                queries.append(MarkerQuery(marker_name=entry.name, query=query))
        return queries


# --------------------------------------------------------------- filters.json
class FilterSwitches(BaseModel):
    """Включение и выключение отдельных фильтров (ТЗ п.11.5)."""

    model_config = ConfigDict(extra="ignore")

    ad: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})
    duplicate: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})
    course: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})
    commercial: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})
    spam: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})
    quality: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})
    comments: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})

    def is_enabled(self, name: str) -> bool:
        block = getattr(self, name, None)
        return bool(block.get("enabled", True)) if isinstance(block, dict) else True


class MonitoringSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    window_minutes: int = Field(default=120, ge=15, le=1440)
    overlap_minutes: int = Field(default=0, ge=0, le=120)
    #: Максимальный возраст публикации (часы). Публикации, опубликованные
    #: раньше ``now - max_age_hours``, не сохраняются в БД и не передаются
    #: в LLM (требование заказчика: только свежие посты).
    max_age_hours: int = Field(default=4, ge=1, le=168)
    count_per_query: int = Field(default=200, ge=1, le=200)


class DuplicateSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    similarity_threshold: float = Field(default=0.94, ge=0.5, le=1.0)
    shingle_size: int = Field(default=3, ge=1, le=6)
    min_length_for_similarity: int = Field(default=120, ge=0)


class SignalSettings(BaseModel):
    """Общие параметры словарных фильтров."""

    model_config = ConfigDict(extra="ignore")

    reject_on_strong_signal: bool = True
    min_signals_for_reject: int = Field(default=2, ge=1, le=5)


class SpamSettings(SignalSettings):
    engagement_bait_reject: bool = True
    #: Порог сигналов для отклонения предложений работы. Выше общего,
    #: потому что «резюме» и «вакансия» встречаются в обычных рассуждениях.
    job_offer_min_signals: int = Field(default=3, ge=1, le=6)


class QualitySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    min_text_length: int = Field(default=40, ge=0)
    max_hashtag_ratio: float = Field(default=0.6, ge=0, le=1)
    max_link_ratio: float = Field(default=0.7, ge=0, le=1)
    min_words: int = Field(default=8, ge=0)
    clickbait_reject: bool = False
    require_topic_link: bool = True
    topic_terms_min_matches: int = Field(default=1, ge=0)

    #: Отклонять публикации без текстовой информации (только медиа/эмодзи/ссылки).
    #: Значение ``min_text_length`` применяется как порог «информативности».
    reject_non_text: bool = True
    #: Минимальное число предложений. Пост из одного предложения и короче
    #: отклоняется (требование заказчика).
    min_sentences: int = Field(default=2, ge=0)


class CommentsSettings(BaseModel):
    """Проверка доступности комментирования (требование заказчика).

    VK API не отдаёт признак ``can_post`` для сервисного ключа, поэтому
    проверка выполняется в браузере по маркеру ``closed_comments`` на
    странице публикации. Проверяются только публикации, уже одобренные
    LLM, — это экономит время и не тормозит основной цикл.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    #: Таймаут загрузки страницы публикации, секунды
    timeout_seconds: int = Field(default=20, ge=5, le=60)
    #: Сколько публикаций проверять за цикл (защита от зависаний)
    max_checks_per_cycle: int = Field(default=25, ge=1, le=100)
    #: Перепроверять уже проверенные публикации
    recheck_after_hours: int = Field(default=0, ge=0, le=720)


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    confidence_threshold: float = Field(default=0.85, ge=0, le=1)
    max_retries: int = Field(default=2, ge=0, le=5)
    #: Предел публикаций в LLM-этапе **за сутки**. Порог общий для всех
    #: циклов суток и учитывает уже обработанные публикации.
    max_posts_per_cycle: int = Field(default=25, ge=1)


class CommentSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    min_length: int = Field(default=200, ge=50)
    max_length: int = Field(default=300, ge=51)
    max_regeneration_attempts: int = Field(default=2, ge=0, le=5)


class CleanupSettings(BaseModel):
    """Очистка БД от отработанных публикаций (требование заказчика)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    #: Сколько часов хранить отработанную публикацию перед удалением.
    #: Запас нужен для контекста дедупликации и повторных попыток отправки.
    retention_hours: int = Field(default=24, ge=1, le=720)
    #: Сколько строк удалять за один запрос (защита от долгой транзакции)
    batch_size: int = Field(default=500, ge=1, le=5000)


class FiltersConfig(BaseModel):
    """Структура config/filters.json."""

    model_config = ConfigDict(extra="ignore")

    version: int = 1
    filters: FilterSwitches = Field(default_factory=FilterSwitches)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    duplicate: DuplicateSettings = Field(default_factory=DuplicateSettings)
    course: SignalSettings = Field(default_factory=SignalSettings)
    commercial: SignalSettings = Field(default_factory=SignalSettings)
    spam: SpamSettings = Field(default_factory=SpamSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)
    comments: CommentsSettings = Field(default_factory=CommentsSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    comment: CommentSettings = Field(default_factory=CommentSettings)
    db: CleanupSettings = Field(default_factory=CleanupSettings)


class WordList(BaseModel):
    """Скомпилированный список регулярных выражений одной категории."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    category: str
    patterns: list[tuple[str, re.Pattern[str]]] = Field(default_factory=list)

    @classmethod
    def compile(cls, category: str, raw: list[str]) -> WordList:
        compiled: list[tuple[str, re.Pattern[str]]] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(f"{category}: пустое выражение в списке")
            try:
                compiled.append((item, re.compile(item, re.IGNORECASE | re.UNICODE)))
            except re.error as exc:
                raise ConfigError(f"{category}: некорректное выражение {item!r}: {exc}") from exc
        return cls(category=category, patterns=compiled)

    def matches(self, text: str) -> list[str]:
        """Возвращает список сработавших выражений."""
        return [raw for raw, pattern in self.patterns if pattern.search(text)]

    def count(self, text: str) -> int:
        return len(self.matches(text))


class SpamWordsConfig(BaseModel):
    """Структура config/spam_words.json со скомпилированными выражениями."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    version: int = 1
    course: dict[str, WordList] = Field(default_factory=dict)
    commercial: dict[str, WordList] = Field(default_factory=dict)
    spam: dict[str, WordList] = Field(default_factory=dict)
    quality: dict[str, WordList] = Field(default_factory=dict)
    topics: WordList = Field(default_factory=lambda: WordList(category="topics"))

    def group(self, name: str) -> dict[str, WordList]:
        return getattr(self, name, {})


def _compile_group(name: str, raw: Any) -> dict[str, WordList]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{name}: ожидался объект с категориями")
    compiled: dict[str, WordList] = {}
    for category, items in raw.items():
        if category == "comment":
            continue
        if not isinstance(items, list):
            raise ConfigError(f"{name}.{category}: ожидался массив строк")
        compiled[category] = WordList.compile(f"{name}.{category}", items)
    return compiled


def parse_spam_words(raw: dict[str, Any]) -> SpamWordsConfig:
    """Компилирует словари сигналов из JSON."""
    topics_raw = raw.get("topics") or {}
    terms = topics_raw.get("terms") if isinstance(topics_raw, dict) else None
    return SpamWordsConfig(
        version=int(raw.get("version", 1)),
        course=_compile_group("course", raw.get("course")),
        commercial=_compile_group("commercial", raw.get("commercial")),
        spam=_compile_group("spam", raw.get("spam")),
        quality=_compile_group("quality", raw.get("quality")),
        topics=WordList.compile("topics", terms or []),
    )


# ------------------------------------------------------------------ загрузчик
@dataclass(slots=True)
class FiltersConfigBundle:
    """Актуальный набор конфигураций фильтров."""

    markers: MarkersConfig
    filters: FiltersConfig
    words: SpamWordsConfig
    signature: str = ""

    @property
    def queries(self) -> list[MarkerQuery]:
        return self.markers.active_queries()


class ConfigLoader:
    """Загружает JSON-конфигурации и перечитывает их при изменении файлов.

    Кеш сравнивает mtime и размер файлов, поэтому корректная конфигурация
    переиспользуется без повторного парсинга. Ошибка в файле **не** заменяет
    действующую конфигурацию, но прерывает цикл поиска (ТЗ п.4.1).
    """

    def __init__(self, config_dir: Path | None = None) -> None:
        self._dir = Path(config_dir or settings.config_dir)
        self._lock = threading.Lock()
        self._cached: FiltersConfigBundle | None = None
        self._stamp: tuple[tuple[str, float, int], ...] | None = None
        self.last_error: ConfigError | None = None

    @property
    def markers_path(self) -> Path:
        return self._dir / "markers.json"

    @property
    def filters_path(self) -> Path:
        return self._dir / "filters.json"

    @property
    def spam_words_path(self) -> Path:
        return self._dir / "spam_words.json"

    def load(self, force: bool = False) -> FiltersConfigBundle:
        """Возвращает актуальный набор конфигураций.

        Raises:
            ConfigError: файл отсутствует, содержит синтаксическую ошибку или
                противоречив. Это сигнал циклу мониторинга — поиск не выполнять.
        """
        stamp = self._current_stamp()
        with self._lock:
            if not force and self._cached is not None and stamp == self._stamp:
                return self._cached

            try:
                bundle = self._read_all()
            except ConfigError as exc:
                self.last_error = exc
                logger.error("Ошибка конфигурации фильтров: %s", exc)
                raise

            if self._cached is not None and bundle.signature != self._cached.signature:
                logger.info("Конфигурация фильтров изменилась — применены новые настройки")
            self.last_error = None
            self._cached = bundle
            self._stamp = stamp
            logger.debug(
                "Конфигурация фильтров загружена: %s активных запросов", len(bundle.queries)
            )
            return bundle

    def try_reload(self) -> FiltersConfigBundle | None:
        """Перечитывает конфигурацию, не бросая исключений (None при ошибке)."""
        try:
            return self.load()
        except ConfigError:
            return None

    def _current_stamp(self) -> tuple[tuple[str, float, int], ...]:
        stamp: list[tuple[str, float, int]] = []
        for path in (self.markers_path, self.filters_path, self.spam_words_path):
            try:
                stat = path.stat()
                stamp.append((path.name, stat.st_mtime, stat.st_size))
            except FileNotFoundError:
                stamp.append((path.name, 0.0, 0))
        return tuple(stamp)

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise ConfigError(f"Файл конфигурации не найден: {path.name}")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Не удалось прочитать {path.name}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path.name}: некорректный JSON (строка {exc.lineno}, "
                f"позиция {exc.colno}): {exc.msg}"
            ) from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path.name}: корневой элемент должен быть объектом")
        return data

    def _read_all(self) -> FiltersConfigBundle:
        markers_raw = self._read_json(self.markers_path)
        filters_raw = self._read_json(self.filters_path)
        words_raw = self._read_json(self.spam_words_path)

        try:
            markers = MarkersConfig.model_validate(markers_raw)
            filters = FiltersConfig.model_validate(filters_raw)
        except ValidationError as exc:
            raise ConfigError(f"Некорректная конфигурация: {exc}") from exc

        words = parse_spam_words(words_raw)

        if markers.enabled and not markers.active_queries():
            raise ConfigError(
                "markers.json: нет активных тематик — поиск выполняться не будет. "
                "Включите хотя бы один маркер или установите enabled: false."
            )

        signature = hashlib.sha1(
            json.dumps(
                [markers_raw, filters_raw, words_raw], sort_keys=True, ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()

        return FiltersConfigBundle(
            markers=markers, filters=filters, words=words, signature=signature
        )


_loader: ConfigLoader | None = None


def get_loader() -> ConfigLoader:
    """Singleton-загрузчик конфигурации."""
    global _loader
    if _loader is None:
        _loader = ConfigLoader()
    return _loader


def reset_loader() -> None:
    """Сбрасывает singleton (используется в тестах)."""
    global _loader
    _loader = None


def load_config(force: bool = False) -> FiltersConfigBundle:
    """Возвращает актуальную конфигурацию фильтров."""
    return get_loader().load(force=force)
