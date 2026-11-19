"""Таблица post_analysis — результаты LLM-проверки (ТЗ п.15.1, п.12.4)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:  # pragma: no cover
    from app.db.models.post import Post


class PostAnalysis(Base):
    """Результат семантической проверки публикации.

    Хранится каждая попытка: тип этапа (llm:classify / llm:comment_generation),
    сырой ответ модели, распарсенные поля и итоговое решение с причиной.
    """

    __tablename__ = "post_analysis"
    __table_args__ = (
        Index("ix_post_analysis_post_id", "post_id"),
        Index("ix_post_analysis_created_at", "created_at"),
        Index("ix_post_analysis_accepted", "accepted"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    # stage: classify | comment_generation | comment_validation
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    relevant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_advertising: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_course_offer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_commercial_promotion: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_spam: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_low_quality: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_clickbait: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_engagement_bait: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_suspicious: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_information_value: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    matched_topics: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    spam_categories: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Итоговое решение по правилам ТЗ п.12.5
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reasons: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    post: Mapped[Post] = relationship(back_populates="analyses")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PostAnalysis post={self.post_id} stage={self.stage} accepted={self.accepted}>"
