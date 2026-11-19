"""Служебные таблицы: история циклов мониторинга и watermarks (ТЗ п.5.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, enum_column
from app.db.enums import RunStatus


class MonitoringRun(Base):
    """История запусков цикла мониторинга.

    Фиксирует время старта/финиша, результат, счётчики и ошибки.
    """

    __tablename__ = "monitoring_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="schedule")
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus, "run_status"),
        nullable=False,
        default=RunStatus.RUNNING,
        server_default=RunStatus.RUNNING.value,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    queries_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    queries_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    posts_found: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    posts_new: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    posts_analyzed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    posts_approved: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    posts_rejected: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    notifications_queued: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    errors: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MonitoringRun id={self.id} status={self.status.value}>"


class SchedulerState(Base):
    """Контрольные точки и состояние планировщика (watermark).

    Ключ-значение: последнее успешно обработанное время, восстановление
    пропущенных интервалов и пр.
    """

    __tablename__ = "scheduler_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
