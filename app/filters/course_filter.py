"""Фильтр курсов и образовательных продуктов (ТЗ п.10).

Критерий отказа — **рекламирование, продажа или продвижение** образовательного
продукта, а не наличие слов «курс» или «обучение» (ТЗ п.10).

Алгоритм:
1. Сильный сигнал продвижения («набираю группу на курс», «запись открыта»,
   «наставничество», «интенсив») + рекламный якорь (цена, запись, оплата)
   → ``REJECT``;
2. Сигнал продвижения без якоря → ``SUSPECT`` (решение передаётся LLM);
3. Личный контекст («прошёл курс», «учусь на курсе») → ``PASS``,
   даже если встречается «курс».
"""

from __future__ import annotations

from app.filters.base import FilterContext, FilterResult
from app.filters.loader import SignalSettings, WordList
from app.vk.models import NormalizedPost
from app.vk.normalizer import clean_text


class CourseFilter:
    """Реклама обучения и образовательных продуктов."""

    name = "course"

    def __init__(
        self,
        settings_: SignalSettings,
        strong: WordList | None = None,
        soft: WordList | None = None,
        anchors: WordList | None = None,
        personal_context: WordList | None = None,
    ) -> None:
        self.settings = settings_
        self.enabled = True
        self.strong = strong or WordList(category="course.strong")
        self.soft = soft or WordList(category="course.soft")
        self.anchors = anchors or WordList(category="course.anchors")
        self.personal_context = personal_context or WordList(category="course.personal")

    def check(self, post: NormalizedPost, context: FilterContext | None = None) -> FilterResult:
        text = clean_text(post.text)
        if not text:
            return FilterResult.passthrough(self.name)

        strong_hits = self.strong.matches(text)
        anchor_hits = self.anchors.matches(text)
        soft_hits = self.soft.matches(text)
        personal_hits = self.personal_context.matches(text)

        # Личный опыт изучения — не реклама (ТЗ п.10)
        if personal_hits and not strong_hits:
            return FilterResult.passthrough(self.name)

        # Сильное продвижение + рекламный якорь — отказ
        if strong_hits and anchor_hits:
            return FilterResult.reject(
                self.name,
                "реклама или продажа образовательного продукта",
                signals=[f"strong:{h}" for h in strong_hits[:3]]
                + [f"anchor:{h}" for h in anchor_hits[:3]],
                details={
                    "strong_signals": len(strong_hits),
                    "anchor_signals": len(anchor_hits),
                },
            )

        # Сигнал продвижения есть, но без явной продажи — решение за LLM
        total = len(strong_hits) + len(anchor_hits)
        if strong_hits or (len(soft_hits) >= 2 and anchor_hits):
            return FilterResult.suspect(
                self.name,
                "признаки продвижения обучения — требуется проверка контекста",
                signals=[f"strong:{h}" for h in strong_hits[:2]]
                + [f"anchor:{h}" for h in anchor_hits[:2]]
                + ([f"soft:{h}" for h in soft_hits[:2]] if soft_hits else []),
                details={"signals": total},
            )

        return FilterResult.passthrough(self.name)
