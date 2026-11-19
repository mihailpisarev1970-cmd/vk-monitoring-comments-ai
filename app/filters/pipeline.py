"""Оркестрация фильтрации (ТЗ раздел 7).

Пайплайн выполняет фильтры в фиксированном порядке, останавливается на первом
безусловном отказе и возвращает итоговое решение с полной диагностикой:

* ``REJECTED`` — публикация отклонена (``rejected_ad`` / ``rejected_filter``);
* ``SUSPECT``  — есть сигналы, окончательное решение за LLM (этап 4);
* ``PASSED``   — технические фильтры пройдены.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.db.enums import PostStatus
from app.filters.ad_filter import AdFilter
from app.filters.base import FilterContext, FilterResult, Verdict
from app.filters.commercial_filter import CommercialFilter
from app.filters.course_filter import CourseFilter
from app.filters.duplicate_filter import DuplicateFilter
from app.filters.loader import FiltersConfigBundle, load_config
from app.filters.quality_filter import QualityFilter
from app.filters.spam_filter import SpamFilter
from app.vk.models import NormalizedPost

logger = get_logger(__name__)


class PipelineOutcome(str, enum.Enum):
    """Итог технической фильтрации."""

    PASSED = "passed"
    SUSPECT = "suspect"
    REJECTED = "rejected"


@dataclass(slots=True)
class PipelineResult:
    """Результат прохождения всех фильтров."""

    outcome: PipelineOutcome
    status: PostStatus
    reason: str
    all_results: list[FilterResult] = field(default_factory=list)
    suspicion_signals: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return self.outcome is PipelineOutcome.REJECTED

    @property
    def has_suspicion(self) -> bool:
        return bool(self.suspicion_signals)

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "status": self.status.value,
            "reason": self.reason,
            "suspicion_signals": self.suspicion_signals,
            "filters": {
                result.filter_name: {
                    "verdict": result.verdict.value,
                    "reason": result.reason,
                    "signals": result.signals,
                }
                for result in self.all_results
            },
        }


class FilterPipeline:
    """Последовательный запуск фильтров."""

    def __init__(self, bundle: FiltersConfigBundle | None = None) -> None:
        self.bundle = bundle or load_config()
        words = self.bundle.words
        config = self.bundle.filters

        self.ad = AdFilter()
        self.duplicate = DuplicateFilter(config.duplicate)
        self.course = CourseFilter(
            config.course,
            strong=words.course.get("strong"),
            soft=words.course.get("soft"),
            anchors=words.course.get("anchors"),
            personal_context=words.course.get("personal_context"),
        )
        self.commercial = CommercialFilter(
            config.commercial,
            strong=words.commercial.get("strong"),
            soft=words.commercial.get("soft"),
            anchors=words.commercial.get("anchors"),
            exclude_context=words.commercial.get("exclude_context"),
            promo=words.commercial.get("promo"),
        )
        self.spam = SpamFilter(config.spam, categories=words.spam)
        self.quality = QualityFilter(
            config.quality,
            topics=words.topics,
            clickbait=words.quality.get("clickbait"),
            meaningless=words.quality.get("meaningless"),
            off_topic=words.quality.get("off_topic"),
        )
        self.switches = config.filters

    def is_enabled(self, name: str) -> bool:
        """Официальную рекламу отключать нельзя (ТЗ п.8)."""
        if name == "ad":
            return True
        return self.switches.is_enabled(name)

    def run(self, post: NormalizedPost, context: FilterContext | None = None) -> PipelineResult:
        """Прогоняет публикацию через все активные фильтры."""
        context = context or FilterContext()
        results: list[FilterResult] = []
        suspicions: list[str] = []

        checks = (
            ("ad", self.ad),
            ("duplicate", self.duplicate),
            ("course", self.course),
            ("commercial", self.commercial),
            ("spam", self.spam),
            ("quality", self.quality),
        )

        for name, flt in checks:
            if not self.is_enabled(name):
                continue
            result = self._safe_check(flt, post, context)
            results.append(result)

            if result.verdict is Verdict.REJECT:
                status = PostStatus.REJECTED_AD if name == "ad" else PostStatus.REJECTED_FILTER
                reason = f"{name}: {result.reason}"
                logger.info("Публикация %s отклонена — %s", post.key, reason)
                return PipelineResult(
                    outcome=PipelineOutcome.REJECTED,
                    status=status,
                    reason=reason,
                    all_results=results,
                    suspicion_signals=suspicions,
                )

            if result.verdict is Verdict.SUSPECT:
                suspicions.extend(result.signals or [f"{name}:suspect"])

        outcome = PipelineOutcome.SUSPECT if suspicions else PipelineOutcome.PASSED
        reason = (
            "требуется проверка LLM: " + ", ".join(suspicions[:5])
            if suspicions
            else "все технические фильтры пройдены"
        )
        return PipelineResult(
            outcome=outcome,
            status=PostStatus.NEW,
            reason=reason,
            all_results=results,
            suspicion_signals=suspicions,
        )

    @staticmethod
    def _safe_check(flt, post: NormalizedPost, context: FilterContext) -> FilterResult:
        """Выполняет фильтр, консервативно обрабатывая его ошибки.

        Исключение внутри фильтра не должно пропускать публикацию дальше:
        результат становится ``SUSPECT`` и уходит на проверку LLM.
        """
        name = getattr(flt, "name", "unknown")
        try:
            return flt.check(post, context)
        except Exception as exc:  # noqa: BLE001 - защита конвейера
            logger.exception("Ошибка фильтра %s: %s", name, exc)
            return FilterResult.suspect(
                name,
                f"внутренняя ошибка фильтра: {exc.__class__.__name__}",
                signals=[f"{name}:error"],
            )
