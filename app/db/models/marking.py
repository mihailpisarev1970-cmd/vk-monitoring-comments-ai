"""Таблица post_markers — маркеры, по которым найдена публикация (ТЗ п.15.1)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:  # pragma: no cover
    from app.db.models.post import Post


class PostMarker(Base):
    """Связка публикации с поисковым маркером и запросом.

    Одна публикация может быть найдена несколькими маркерами — сохраняем все,
    но не дублируем одну и ту же пару (post, marker_name, query).
    """

    __tablename__ = "post_markers"
    __table_args__ = (
        UniqueConstraint(
            "post_id", "marker_name", "query", name="uq_post_markers_post_marker_query"
        ),
        Index("ix_post_markers_post_id", "post_id"),
        Index("ix_post_markers_marker_name", "marker_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    marker_name: Mapped[str] = mapped_column(String(255), nullable=False)
    query: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    post: Mapped[Post] = relationship(back_populates="markers")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PostMarker post={self.post_id} marker={self.marker_name!r} query={self.query!r}>"
