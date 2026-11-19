"""Очередь доставки уведомлений (ТЗ п.14.3).

Требования:
* не терять уведомления при недоступности Telegram (очередь в PostgreSQL);
* повторять отправку при временных ошибках с backoff;
* ограниченное число попыток;
* вести журнал ошибок (``last_error``, ``attempts``);
* не создавать повторные уведомления после успешной отправки;
* при повторной отправке использовать уже сохранённый комментарий;
* хранить Telegram message ID.

Жизненный цикл: ``pending → sending → sent`` либо ``failed`` (после лимита).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import NotificationStatus, PostStatus
from app.db.models import Notification, Post, PostAnalysis
from app.telegram.formatter import NotificationMessage, build_notification
from app.telegram.sender import (
    TelegramError,
    TelegramPermanentError,
    TelegramSender,
    TelegramTransientError,
)

logger = get_logger(__name__)

#: Максимум попыток по умолчанию (ТЗ п.14.3)
DEFAULT_MAX_ATTEMPTS = 5
#: Базовая задержка backoff в секундах
BACKOFF_BASE = 60


@dataclass(slots=True)
class QueueStats:
    """Сводка по обработке очереди."""

    pending: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending": self.pending,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors or [],
        }


def compute_backoff(attempt: int, base: int = BACKOFF_BASE, cap: int = 3600) -> timedelta:
    """Экспоненциальная задержка: 1 мин, 2 мин, 4 мин … с потолком."""
    return timedelta(seconds=min(base * (2 ** max(0, attempt - 1)), cap))



async def enqueue_post(
    session: AsyncSession,
    post: Post,
    *,
    chat_id: str,
    comment: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Notification | None:
    """Ставит публикацию в очередь уведомлений.

    Возвращает ``None``, если уведомление уже создано или отправлено
    (ТЗ п.14.3: не создавать повторные уведомления).

    Комментарий берётся из аргумента либо из последнего успешного анализа
    (``post_analysis``, стадия ``comment_generation``), поэтому при повторной
    отправке LLM заново не вызывается.
    """
    existing = await session.scalar(
        select(Notification).where(
            Notification.post_id == post.id, Notification.chat_id == chat_id
        )
    )
    if existing is not None:
        logger.debug(
            "Уведомление для %s уже существует (статус %s)", post.key, existing.status
        )
        return None

    topics = await _load_topics(session, post.id)

    if comment is None:
        analysis = await session.scalar(
            select(PostAnalysis)
            .where(
                PostAnalysis.post_id == post.id,
                PostAnalysis.stage == "comment_generation",
                PostAnalysis.accepted.is_(True),
            )
            .order_by(PostAnalysis.id.desc())
            .limit(1)
        )
        if analysis is None or not analysis.reason:
            logger.warning("Для публикации %s нет готового комментария", post.key)
            return None
        comment = analysis.reason

    message = build_notification(
        post_text=post.text,
        comment=comment,
        topics=topics,
        post_url=post.post_url,
        author=post.author_name,
    )

    notification = Notification(
        post_id=post.id,
        chat_id=chat_id,
        comment_text=comment,
        message_text=message.text,
        status=NotificationStatus.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        next_attempt_at=datetime.now(UTC),
    )
    session.add(notification)
    post.status = PostStatus.QUEUED
    post.status_reason = "telegram: поставлено в очередь"
    await session.flush()
    logger.info("Публикация %s поставлена в очередь Telegram", post.key)
    return notification


async def _load_topics(session: AsyncSession, post_id: int) -> list[str]:
    """Тематики публикации из последнего успешного анализа."""
    analysis = await session.scalar(
        select(PostAnalysis)
        .where(
            PostAnalysis.post_id == post_id,
            PostAnalysis.accepted.is_(True),
            PostAnalysis.matched_topics.isnot(None),
        )
        .order_by(PostAnalysis.id.desc())
        .limit(1)
    )
    if analysis and analysis.matched_topics:
        return list(analysis.matched_topics)
    return []


class NotificationWorker:
    """Обрабатывает очередь уведомлений с повторами (ТЗ п.14.3)."""

    def __init__(
        self,
        sender: TelegramSender,
        *,
        sleep: Any = asyncio.sleep,
        max_batch: int = 20,
    ) -> None:
        self.sender = sender
        self._sleep = sleep
        self.max_batch = max_batch

    async def process_due(
        self, session: AsyncSession, *, now: datetime | None = None
    ) -> QueueStats:
        """Отправляет все уведомления, время которых наступило."""
        stats = QueueStats(errors=[])
        current = now or datetime.now(UTC)

        notifications = (
            await session.execute(
                select(Notification)
                .where(
                    Notification.status.in_(
                        [NotificationStatus.PENDING, NotificationStatus.SENDING]
                    ),
                    Notification.next_attempt_at <= current,
                )
                .order_by(Notification.next_attempt_at, Notification.id)
                .limit(self.max_batch)
            )
        ).scalars().all()

        stats.pending = len(notifications)
        for notification in notifications:
            await self._deliver(session, notification, stats)
        await session.flush()
        return stats

    async def _deliver(
        self, session: AsyncSession, notification: Notification, stats: QueueStats
    ) -> None:
        post = await session.get(Post, notification.post_id)
        message = NotificationMessage(
            text=notification.message_text,
            post_url=post.post_url if post else "",
            plain_text=notification.message_text,
        )

        notification.status = NotificationStatus.SENDING
        notification.attempts += 1
        await session.flush()

        try:
            result = await self.sender.send(notification.chat_id, message)
        except TelegramTransientError as exc:
            await self._schedule_retry(session, notification, str(exc), exc.retry_after)
            stats.errors.append(f"{notification.id}: {exc}")
            logger.warning(
                "Уведомление %s: временная ошибка (%s), попытка %s/%s",
                notification.id,
                exc,
                notification.attempts,
                notification.max_attempts,
            )
            return
        except TelegramPermanentError as exc:
            await self._fail(session, notification, str(exc))
            stats.failed += 1
            stats.errors.append(f"{notification.id}: {exc}")
            logger.error("Уведомление %s отклонено Telegram: %s", notification.id, exc)
            return
        except TelegramError as exc:
            await self._schedule_retry(session, notification, str(exc), 0)
            stats.errors.append(f"{notification.id}: {exc}")
            return

        notification.status = NotificationStatus.SENT
        notification.telegram_message_id = result.message_id
        notification.sent_at = datetime.now(UTC)
        notification.last_error = None
        notification.chat_id = result.chat_id or notification.chat_id
        stats.sent += 1

        if post is not None:
            post.status = PostStatus.SENT
            post.status_reason = f"telegram: отправлено (message_id={result.message_id})"

        logger.info(
            "Уведомление %s отправлено (message_id=%s)", notification.id, result.message_id
        )

    async def _schedule_retry(
        self,
        session: AsyncSession,
        notification: Notification,
        error: str,
        retry_after: float = 0.0,
    ) -> None:
        """Планирует повтор или помечает уведомление как failed."""
        notification.last_error = error[:2000]

        if notification.attempts >= notification.max_attempts:
            notification.status = NotificationStatus.FAILED
            post = await session.get(Post, notification.post_id)
            if post is not None:
                post.status = PostStatus.FAILED
                post.status_reason = f"telegram: не доставлено — {error[:200]}"
            logger.error(
                "Уведомление %s исчерпало попытки (%s): %s",
                notification.id,
                notification.max_attempts,
                error,
            )
            return

        delay = (
            timedelta(seconds=retry_after)
            if retry_after > 0
            else compute_backoff(notification.attempts)
        )
        notification.status = NotificationStatus.PENDING
        notification.next_attempt_at = datetime.now(UTC) + delay
        logger.info(
            "Уведомление %s будет повторено через %s", notification.id, delay
        )

    async def _fail(
        self, session: AsyncSession, notification: Notification, error: str
    ) -> None:
        notification.status = NotificationStatus.FAILED
        notification.last_error = error[:2000]
        post = await session.get(Post, notification.post_id)
        if post is not None:
            post.status = PostStatus.FAILED
            post.status_reason = f"telegram: отклонено — {error[:200]}"
