"""Базовые контракты фильтров (ТЗ раздел 7).

Каждый фильтр получает публикацию и контекст (настройки, история текстов)
и возвращает :class:`FilterResult`. Фильтр **никогда** не бросает исключения
наружу: любая ошибка трактуется консервативно — публикация не пропускается.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol

from app.vk.models import NormalizedPost


class Verdict(str, enum.Enum):
    """Результат прохождения одного фильтра."""

    PASS = "pass"          # фильтр не нашёл проблем
    REJECT = "reject"      # безусловный отказ
    SUSPECT = "suspect"    # сигнал есть, но решение остаётся за LLM


@dataclass(slots=True)
class FilterResult:
    """Результат проверки публикации фильтром."""

    filter_name: str
    verdict: Verdict
    reason: str | None = None
    #: Категории и сработавшие сигналы — сохраняются для диагностики
    signals: list[str] = field(default_factory=list)
    #: Дополнительные метрики (например, близость текста при дедупликации)
    details: dict[str, float | int | str | None] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return self.verdict is Verdict.REJECT

    @classmethod
    def passthrough(cls, filter_name: str) -> FilterResult:
        return cls(filter_name=filter_name, verdict=Verdict.PASS)

    @classmethod
    def reject(cls, filter_name: str, reason: str, **kwargs) -> FilterResult:
        return cls(
            filter_name=filter_name,
            verdict=Verdict.REJECT,
            reason=reason,
            signals=kwargs.pop("signals", []),
            details=kwargs.pop("details", {}),
        )

    @classmethod
    def suspect(cls, filter_name: str, reason: str, **kwargs) -> FilterResult:
        return cls(
            filter_name=filter_name,
            verdict=Verdict.SUSPECT,
            reason=reason,
            signals=kwargs.pop("signals", []),
            details=kwargs.pop("details", {}),
        )


@dataclass(slots=True)
class FilterContext:
    """Контекст, доступный фильтрам.

    ``seen_texts`` — нормализованные тексты уже обработанных публикаций,
    используется дедупликацией похожих текстов (ТЗ п.11.4). Передаётся
    списком, чтобы фильтр оставался чистым и легко тестируемым.
    """

    seen_texts: list[str] = field(default_factory=list)
    #: Соответствие текста его автору — для выявления массовых рассылок
    seen_authors: list[int] = field(default_factory=list)


class BaseFilter(Protocol):
    """Интерфейс фильтра."""

    name: str
    enabled: bool

    def check(self, post: NormalizedPost, context: FilterContext) -> FilterResult:
        """Проверяет публикацию и возвращает результат."""
        ...
