"""Таблица notifications — очередь доставки уведомлений в Telegram (ТЗ п.14.3)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, enum_column
from app.db.enums import NotificationStatus

if TYPE_CHECKING:  # pragma: no cover
    from app.db.models.post import Post


class Notification(Base):
    """Уведомление о публикации, подготовленное для отправки в Telegram.

    Хранит уже сгенерированный комментарий и тело сообщения, чтобы при
    повторной отправке не обращаться к LLM заново (ТЗ п.14.3).
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # Одна запись уведомления на публикацию для конкретного чата
        UniqueConstraint("post_id", "chat_id", name="uq_notifications_post_chat"),
        Index("ix_notifications_status", "status"),
        Index("ix_notifications_next_attempt_at", "next_attempt_at"),
        Index("ix_notifications_post_id", "post_id"),
        # Не создавать повторные уведомления после успешной отправки (ТЗ п.14.3)
        Index(
            "notification_post_id_uq",
            "post_id",
            unique=True,
            postgresql_where=text("status = 'sent'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    # Одно активное уведомление на публикацию (гарантия через unique-индекс post_id)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)

    comment_text: Mapped[str] = mapped_column(Text, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
        enum_column(NotificationStatus, "notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    post: Mapped[Post] = relationship(back_populates="notifications")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Notification post={self.post_id} status={self.status.value}>"
