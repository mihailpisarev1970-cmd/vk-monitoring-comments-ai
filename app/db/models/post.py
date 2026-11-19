"""Таблица posts — найденные публикации ВКонтакте (ТЗ п.15.1)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, enum_column
from app.db.enums import AdFlag, PostStatus

if TYPE_CHECKING:  # pragma: no cover
    from app.db.models.analysis import PostAnalysis
    from app.db.models.marking import PostMarker
    from app.db.models.notification import Notification


class Post(Base, TimestampMixin):
    """Публикация ВКонтакте.

    Уникальность публикации определяется парой (owner_id, post_id) — ТЗ п.6.3.
    """

    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("owner_id", "post_id", name="uq_posts_owner_post"),
        # Точный дубликат текста среди публикаций (ТЗ п.11.4).
        # Именованное ограничение — чтобы ON CONFLICT ON CONSTRAINT работал.
        Index(
            "uq_posts_text_hash_not_null",
            "text_hash",
            unique=True,
            postgresql_where=text("text_hash IS NOT NULL"),
        ),
        Index("ix_posts_status", "status"),
        Index("ix_posts_published_at", "published_at"),
        Index("ix_posts_author_id", "author_id"),
        Index("ix_posts_text_hash", "text_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    post_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    post_url: Mapped[str] = mapped_column(String(512), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # Метрики (если доступны)
    comments_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reposts_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Признаки официальной рекламы VK (ТЗ п.8)
    marked_as_ads: Mapped[bool | None] = mapped_column(nullable=True)
    ad_flag: Mapped[AdFlag] = mapped_column(
        enum_column(AdFlag, "ad_flag"),
        nullable=False,
        default=AdFlag.UNKNOWN,
        server_default=AdFlag.UNKNOWN.value,
    )
    post_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Хеш нормализованного текста для дедупликации (ТЗ п.11.4)
    text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[PostStatus] = mapped_column(
        enum_column(PostStatus, "post_status"),
        nullable=False,
        default=PostStatus.NEW,
        server_default=PostStatus.NEW.value,
    )
    # Человекочитаемая причина текущего статуса
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Счётчик неудачных попыток обработки (защита от бесконечных ретраев)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    markers: Mapped[list[PostMarker]] = relationship(
        back_populates="post", cascade="all, delete-orphan", lazy="selectin"
    )
    analyses: Mapped[list[PostAnalysis]] = relationship(
        back_populates="post", cascade="all, delete-orphan", lazy="selectin"
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="post", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def key(self) -> str:
        """Составной ключ публикации: ``owner_id_post_id``."""
        return f"{self.owner_id}_{self.post_id}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Post owner={self.owner_id} id={self.post_id} status={self.status.value}>"
