"""Фильтр скрытой рекламы и коммерческих предложений (ТЗ п.9).

Отсекаются предложения разработки сайтов/ботов, автоматизации как услуги,
продвижение IT-агентств, лидогенерация, продажа консультаций, внедрение CRM,
партнёрские и реферальные программы (ТЗ п.9).

При этом обычные содержательные публикации о собственных проектах и опыте
**не** исключаются, если автор ничего не продаёт (ТЗ п.9, последний абзац).
"""

from __future__ import annotations

from app.filters.base import FilterContext, FilterResult
from app.filters.loader import SignalSettings, WordList
from app.vk.models import NormalizedPost
from app.vk.normalizer import clean_text


class CommercialFilter:
    """Скрытая реклама и коммерческие предложения."""

    name = "commercial"

    def __init__(
        self,
        settings_: SignalSettings,
        strong: WordList | None = None,
        soft: WordList | None = None,
        anchors: WordList | None = None,
        exclude_context: WordList | None = None,
        promo: WordList | None = None,
    ) -> None:
        self.settings = settings_
        self.enabled = True
        self.strong = strong or WordList(category="commercial.strong")
        self.soft = soft or WordList(category="commercial.soft")
        self.anchors = anchors or WordList(category="commercial.anchors")
        self.exclude_context = exclude_context or WordList(category="commercial.exclude")
        self.promo = promo or WordList(category="commercial.promo")

    def check(self, post: NormalizedPost, context: FilterContext | None = None) -> FilterResult:
        text = clean_text(post.text)
        if not text:
            return FilterResult.passthrough(self.name)

        strong_hits = self.strong.matches(text)
        anchor_hits = self.anchors.matches(text)
        soft_hits = self.soft.matches(text)
        exclude_hits = self.exclude_context.matches(text)
        promo_hits = self.promo.matches(text)

        # Контекст личного проекта/вопроса снимает подозрения
        if exclude_hits and not strong_hits:
            return FilterResult.passthrough(self.name)

        # Явный рекламный характер: промо-лексика + призыв к действию.
        # Требование заказчика: рекламные публикации не комментируем.
        # Порог по промо-сигналам (не меньше двух) не даёт отклонить обычный
        # пост, где просто упомянута скидка или новый релиз.
        if len(promo_hits) >= self.settings.min_signals_for_reject:
            return FilterResult.reject(
                self.name,
                "рекламная публикация",
                signals=[f"promo:{h}" for h in promo_hits[:3]]
                + [f"anchor:{h}" for h in anchor_hits[:2]]
                + [f"strong:{h}" for h in strong_hits[:2]],
                details={
                    "promo_signals": len(promo_hits),
                    "anchor_signals": len(anchor_hits),
                },
            )

        # Явное коммерческое предложение + призыв к контакту/заявке
        if strong_hits and (anchor_hits or len(strong_hits) >= self.settings.min_signals_for_reject):
            return FilterResult.reject(
                self.name,
                "скрытая реклама или коммерческое предложение",
                signals=[f"strong:{h}" for h in strong_hits[:3]]
                + [f"anchor:{h}" for h in anchor_hits[:2]],
                details={
                    "strong_signals": len(strong_hits),
                    "anchor_signals": len(anchor_hits),
                },
            )

        # Услуги + отраслевая лексика — на усмотрение LLM
        if strong_hits or (len(soft_hits) >= 2 and anchor_hits):
            return FilterResult.suspect(
                self.name,
                "признаки коммерческого предложения — требуется проверка контекста",
                signals=[f"strong:{h}" for h in strong_hits[:2]]
                + [f"soft:{h}" for h in soft_hits[:2]],
                details={"signals": len(strong_hits) + len(soft_hits)},
            )

        return FilterResult.passthrough(self.name)
