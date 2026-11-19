"""Антиспам и выявление искусственного вовлечения (ТЗ п.11).

Категории сигналов (ТЗ п.11.1, п.11.2):
* ``job_offer`` — предложение работы/вакансия (требование заказчика: такие
  публикации не годятся для содержательного комментария по теме проекта);
* ``engagement_bait`` — призывы ставить «+», репостить, отмечать друзей;
* ``finance_schemes`` — гарантированная доходность, пассивный доход;
* ``crypto`` — подозрительные криптовалютные предложения;
* ``mlm`` — сетевой маркетинг и «структуры»;
* ``phishing_suspicious`` — попытки получить данные или перейти по ссылке;
* ``mass_mailing`` — признаки массовой рассылки.

Не считаются спамом автоматически (ТЗ п.11.3): эмодзи, несколько хэштегов,
ссылки на GitHub, технические ссылки, использование ИИ для текста,
небольшой содержательный вопрос.
"""

from __future__ import annotations

from app.filters.base import FilterContext, FilterResult
from app.filters.loader import SpamSettings, WordList
from app.vk.models import NormalizedPost
from app.vk.normalizer import clean_text

#: Категории, при которых публикация не подходит для комментария
_OFF_TOPIC_CATEGORIES = ("job_offer",)


class SpamFilter:
    """Многоуровневый антиспам."""

    name = "spam"

    def __init__(
        self,
        settings_: SpamSettings,
        categories: dict[str, WordList] | None = None,
    ) -> None:
        self.settings = settings_
        self.enabled = True
        self.categories = categories or {}

    def check(self, post: NormalizedPost, context: FilterContext | None = None) -> FilterResult:
        text = clean_text(post.text)
        if not text:
            return FilterResult.passthrough(self.name)

        hits: dict[str, list[str]] = {}
        for category, word_list in self.categories.items():
            matched = word_list.matches(text)
            if matched:
                hits[category] = matched

        if not hits:
            return FilterResult.passthrough(self.name)

        signals = [f"{category}:{hit}" for category, items in hits.items() for hit in items[:2]]

        # Предложение работы — публикация не подходит для тематического
        # комментария. Порог выше общего: слова «резюме» и «вакансия»
        # встречаются в обычных рассуждениях о карьере. Найм «ищу/ищем»
        # вместе с контактным призывом — достаточное основание.
        for category in _OFF_TOPIC_CATEGORIES:
            matched = hits.get(category, [])
            strong_hiring = any(
                "ищу\\s+" in hit or "ищем\\s+" in hit or "требуется\\s+" in hit
                for hit in matched
            )
            threshold = (
                self.settings.job_offer_min_signals - 1
                if strong_hiring
                else self.settings.job_offer_min_signals
            )
            if len(matched) >= threshold:
                return FilterResult.reject(
                    self.name,
                    "предложение работы или вакансия",
                    signals=[f"{category}:{hit}" for hit in matched[:3]],
                    details={"job_signals": len(matched)},
                )

        # Накрутка активности — безусловный отказ (ТЗ п.11.2)
        if "engagement_bait" in hits and self.settings.engagement_bait_reject:
            return FilterResult.reject(
                self.name,
                "искусственное привлечение активности",
                signals=signals,
                details={"categories": len(hits)},
            )

        # Мошеннические и финансовые схемы — при нескольких признаках сразу отказ
        risky = [name for name in ("finance_schemes", "crypto", "mlm", "phishing_suspicious")
                 if name in hits]
        if len(risky) >= 2:
            return FilterResult.reject(
                self.name,
                "подозрительная или мошенническая схема",
                signals=signals,
                details={"categories": len(hits), "risky_categories": len(risky)},
            )

        total = sum(len(items) for items in hits.values())
        if risky or total >= self.settings.min_signals_for_reject:
            return FilterResult.suspect(
                self.name,
                "признаки спама — требуется проверка контекста",
                signals=signals,
                details={"categories": len(hits), "signals": total},
            )

        return FilterResult.passthrough(self.name)
