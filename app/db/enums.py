"""Перечисления статусов и категорий, используемые в БД."""

from __future__ import annotations

import enum


class PostStatus(str, enum.Enum):
    """Жизненный цикл публикации.

    new              — найден и сохранён, ещё не обработан
    rejected_ad      — официальная реклама VK (ТЗ п.8)
    rejected_filter  — отклонён техническими фильтрами (ТЗ п.7–11)
    rejected_llm     — отклонён LLM-классификацией (ТЗ п.12.5)
    rejected_comment — комментарий не прошёл проверку (ТЗ п.13.2)
    approved         — прошёл все проверки, готов к отправке
    queued           — поставлен в очередь уведомлений
    sent             — успешно отправлен в Telegram
    failed           — исчерпаны попытки обработки/отправки
    """

    NEW = "new"
    REJECTED_AD = "rejected_ad"
    REJECTED_FILTER = "rejected_filter"
    REJECTED_LLM = "rejected_llm"
    REJECTED_COMMENT = "rejected_comment"
    APPROVED = "approved"
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


class AdFlag(str, enum.Enum):
    """Состояние рекламного признака официальной рекламы VK (ТЗ п.8).

    yes     — метка рекламы присутствует (marked_as_ads / post_ads / author_ad)
    no      — метка явно отсутствует в ответе API
    unknown — поле отсутствует в ответе, нельзя сделать вывод
    """

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class NotificationStatus(str, enum.Enum):
    """Статус доставки уведомления в Telegram (ТЗ п.14.3)."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class RunStatus(str, enum.Enum):
    """Статус цикла мониторинга (ТЗ п.5.1)."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
