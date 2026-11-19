"""Очистка базы от уже отработанных публикаций.

Требование заказчика: база данных не должна раздуваться. Публикация
считается полностью отработанной, если:

* она **терминальная** — LLM и Telegram для неё больше не работают
  (``sent``, все виды ``rejected_*``, ``failed``);
* с момента её обработки прошло не меньше ``db.cleanup_after_hours``
  часов — запас нужен, чтобы:
  - отложенные уведомления успели дойти при сбоях Telegram;
  - тексты оставались в контексте дедупликации
    (:func:`app.scheduler.watermark.load_seen_texts`), иначе одни и те же
    рекламные тексты снова пошли бы в LLM.

Удаление каскадное: связанные записи ``post_analysis``, ``post_markers``
и ``notifications`` уходят вместе с публикацией (см. ``ondelete="CASCADE"``
в моделях), поэтому отдельных запросов на них не требуется.

Публикации в статусе ``new`` (ждут LLM) и ``approved``/``queued``
(ждут отправки) не удаляются никогда — это незавершённая работа.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import PostStatus
from app.db.models import Notification, Post

logger = get_logger(__name__)

#: Статусы публикаций, по которым работа завершена окончательно
FINISHED_STATUSES: tuple[PostStatus, ...] = (
    PostStatus.SENT,
    PostStatus.REJECTED_AD,
    PostStatus.REJECTED_FILTER,
    PostStatus.REJECTED_LLM,
    PostStatus.REJECTED_COMMENT,
    PostStatus.FAILED,
)

#: Максимум строк за один DELETE: не держим долгую транзакцию
DEFAULT_BATCH_SIZE = 500

#: Сколько часов хранить проверенные публикации даже после завершения
#: (нужно для контекста дедупликации и разбора спорных отклонений)
DEFAULT_RETENTION_HOURS = 24


@dataclass(slots=True)
class CleanupStats:
    """Итог очистки."""

    deleted_posts: int = 0
    deleted_notifications: int = 0
    remaining_posts: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "deleted_posts": self.deleted_posts,
            "deleted_notifications": self.deleted_notifications,
            "remaining_posts": self.remaining_posts,
        }


async def purge_processed_posts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    retention_hours: int = DEFAULT_RETENTION_HOURS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    owner_ids: list[int] | None = None,
) -> CleanupStats:
    """Удаляет отработанные публикации и связанные с ними записи.

    ``owner_ids`` ограничивает очистку конкретными сообществами/авторами —
    используется в тестах, чтобы не затрагивать данные, уже лежащие в базе.

    Возвращает статистику: сколько удалено и сколько публикаций осталось.
    Очистка идемпотентна — повторный вызов на чистой базе ничего не делает.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = now - timedelta(hours=max(0, retention_hours))

    # Граница выбирается по времени завершения работы: updated_at меняется
    # при каждой обработке. Для отправленных берём sent_at уведомления —
    # более точный сигнал, но updated_at публикации обновляется в том же
    # цикле, поэтому достаточно его.
    conditions = [
        Post.status.in_(FINISHED_STATUSES),
        Post.updated_at <= cutoff,
    ]
    if owner_ids:
        conditions.append(Post.owner_id.in_(owner_ids))

    stale_ids = select(Post.id).where(*conditions).order_by(Post.id).limit(batch_size)

    stats = CleanupStats()
    while True:
        ids = list((await session.execute(stale_ids)).scalars().all())
        if not ids:
            break

        # Уведомления удаляются явно: так статистика понятна в логах,
        # а на случай отключённого каскада в БД очистка остаётся полной.
        notifications = await session.execute(
            delete(Notification).where(Notification.post_id.in_(ids))
        )
        deleted_notifications = int(notifications.rowcount or 0)

        result = await session.execute(delete(Post).where(Post.id.in_(ids)))
        deleted_posts = int(result.rowcount or 0)
        stats.deleted_posts += deleted_posts
        stats.deleted_notifications += deleted_notifications

        await session.flush()
        if deleted_posts < batch_size:
            break

    total_stmt = select(func.count(Post.id))
    if owner_ids:
        total_stmt = total_stmt.where(Post.owner_id.in_(owner_ids))
    stats.remaining_posts = int((await session.execute(total_stmt)).scalar_one())

    if stats.deleted_posts:
        logger.info(
            "Очистка БД: удалено публикаций %s (с уведомлениями %s), старше %s ч; "
            "осталось %s",
            stats.deleted_posts,
            stats.deleted_notifications,
            retention_hours,
            stats.remaining_posts,
        )
    return stats
