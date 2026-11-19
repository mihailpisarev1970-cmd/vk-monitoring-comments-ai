"""Правило принятия решения по результату LLM (ТЗ п.12.5).

Публикация допускается к генерации комментария **только** при одновременном
выполнении всех условий:

* ``relevant = true``;
* все запрещающие признаки ``false`` (реклама, курсы, коммерция, спам,
  низкое качество, кликбейт, накрутка, подозрительное);
* ``has_information_value = true``;
* ``confidence >= порога`` из конфигурации (по умолчанию 0.85).

Любое сомнение трактуется консервативно: публикация не отправляется.
Белые списки терминов или авторов не отменяют жёсткие запреты.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.llm.schemas import ClassificationResult

#: Человекочитаемые названия причин отказа
REASON_LABELS = {
    "relevant": "публикация не относится к тематикам проекта",
    "is_advertising": "признана рекламой",
    "is_course_offer": "предложение обучения или курсов",
    "is_commercial_promotion": "коммерческое предложение",
    "is_spam": "признана спамом",
    "is_low_quality": "низкое качество текста",
    "is_clickbait": "кликбейт",
    "is_engagement_bait": "искусственное привлечение активности",
    "is_suspicious": "подозрительное содержание",
    "has_information_value": "нет информационной ценности",
    "confidence": "недостаточная уверенность модели",
}


@dataclass(slots=True)
class Decision:
    """Итоговое решение по публикации."""

    accepted: bool
    rejection_reasons: list[str] = field(default_factory=list)
    explanation: str = ""

    @property
    def rejected(self) -> bool:
        return not self.accepted


def decide(result: ClassificationResult, confidence_threshold: float = 0.85) -> Decision:
    """Применяет правила ТЗ п.12.5 к результату классификации."""
    reasons: list[str] = []

    if not result.relevant:
        reasons.append("relevant")
    reasons.extend(result.forbidden_flags)
    if not result.has_information_value:
        reasons.append("has_information_value")
    if result.confidence < confidence_threshold:
        reasons.append("confidence")

    if not reasons:
        return Decision(accepted=True, explanation="публикация допущена к генерации комментария")

    labels = [REASON_LABELS.get(reason, reason) for reason in reasons]
    return Decision(
        accepted=False,
        rejection_reasons=reasons,
        explanation="отклонено: " + ", ".join(labels),
    )
