"""Фильтр официальной рекламы ВКонтакте (ТЗ п.8).

Правила:
* ``marked_as_ads = 1``, ``post_type = post_ads``, ``marked_as_author_ad``,
  непустой ``author_ad`` — безусловное исключение (``rejected_ad``);
* поля отсутствуют в ответе — состояние ``unknown``, публикация **не**
  считается «не рекламой» и проходит дальше с флагом подозрения;
* фильтр нельзя отключить: ТЗ п.8 требует безусловного исключения.
"""

from __future__ import annotations

from app.db.enums import AdFlag
from app.filters.base import FilterContext, FilterResult
from app.vk.models import NormalizedPost


class AdFilter:
    """Проверка рекламных меток VK."""

    name = "ad"
    #: Официальная реклама исключается всегда, независимо от конфигурации
    enabled = True
    disabled_reason = "Фильтр официальной рекламы отключить нельзя (ТЗ п.8)"

    def check(self, post: NormalizedPost, context: FilterContext | None = None) -> FilterResult:
        if post.ad_flag is AdFlag.YES:
            signals = [
                f"{key}={value}"
                for key, value in post.ad_details.items()
                if value not in ("absent", "empty", False, None)
            ]
            return FilterResult.reject(
                self.name,
                "официальная реклама ВКонтакте",
                signals=signals or ["marked_as_ads"],
                details={
                    "marked_as_ads": post.marked_as_ads,
                    "post_type": post.post_type,
                },
            )

        if post.ad_flag is AdFlag.UNKNOWN:
            # Отсутствие поля не подтверждает отсутствие рекламы (ТЗ п.8)
            return FilterResult.suspect(
                self.name,
                "рекламные метки отсутствуют в ответе API — нужна проверка контентом",
                signals=["ad_flag_unknown"],
            )

        return FilterResult.passthrough(self.name)
