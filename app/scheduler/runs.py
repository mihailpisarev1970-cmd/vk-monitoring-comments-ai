"""История циклов мониторинга (ТЗ п.5.1).

Каждый запуск фиксируется в ``monitoring_runs``: время старта и окончания,
окно поиска, счётчики по этапам, ошибки и итоговый статус.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import RunStatus
from app.db.models import MonitoringRun

logger = get_logger(__name__)


@dataclass(slots=True)
class RunCounters:
    """Счётчики одного цикла."""

    queries_total: int = 0
    queries_failed: int = 0
    posts_found: int = 0
    posts_new: int = 0
    #: Отброшено при поиске как несвежие (старше ``monitoring.max_age_hours``)
    posts_stale: int = 0
    #: Пропущено перед LLM: истёк срок свежести или исчерпан суточный лимит
    posts_skipped: int = 0
    #: Удалено из БД при очистке отработанных публикаций
    posts_purged: int = 0
    posts_analyzed: int = 0
    posts_approved: int = 0
    posts_rejected: int = 0
    notifications_queued: int = 0
    notifications_sent: int = 0
    duplicates_skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queries_total": self.queries_total,
            "queries_failed": self.queries_failed,
            "posts_found": self.posts_found,
            "posts_new": self.posts_new,
            "posts_stale": self.posts_stale,
            "posts_skipped": self.posts_skipped,
            "posts_purged": self.posts_purged,
            "posts_analyzed": self.posts_analyzed,
            "posts_approved": self.posts_approved,
            "posts_rejected": self.posts_rejected,
            "notifications_queued": self.notifications_queued,
            "notifications_sent": self.notifications_sent,
            "duplicates_skipped": self.duplicates_skipped,
        }


async def start_run(
    session: AsyncSession,
    *,
    trigger: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> MonitoringRun:
    """Создаёт запись о начале цикла."""
    run = MonitoringRun(
        trigger=trigger,
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC),
        window_start=window_start,
        window_end=window_end,
    )
    session.add(run)
    await session.flush()
    logger.info("Цикл мониторинга #%s запущен (trigger=%s)", run.id, trigger)
    return run


async def finish_run(
    session: AsyncSession,
    run: MonitoringRun,
    *,
    counters: RunCounters,
    status: RunStatus | None = None,
    details: dict[str, Any] | None = None,
) -> MonitoringRun:
    """Завершает цикл: записывает счётчики, ошибки и статус."""
    run.finished_at = datetime.now(UTC)
    run.queries_total = counters.queries_total
    run.queries_failed = counters.queries_failed
    run.posts_found = counters.posts_found
    run.posts_new = counters.posts_new
    run.posts_analyzed = counters.posts_analyzed
    run.posts_approved = counters.posts_approved
    run.posts_rejected = counters.posts_rejected
    run.notifications_queued = counters.notifications_queued
    run.errors = counters.errors or None

    payload = counters.to_dict()
    if details:
        payload.update(details)
    run.details = payload

    if status is None:
        if counters.queries_failed and counters.queries_failed >= counters.queries_total:
            status = RunStatus.FAILED
        elif counters.queries_failed or counters.errors:
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.SUCCESS
    run.status = status

    await session.flush()
    logger.info(
        "Цикл #%s завершён: %s (%s новых, %s отправлено, %s ошибок)",
        run.id,
        run.status.value,
        counters.posts_new,
        counters.notifications_sent,
        counters.queries_failed,
    )
    return run


async def fail_run(
    session: AsyncSession, run: MonitoringRun, error: str, *, details: dict[str, Any] | None = None
) -> MonitoringRun:
    """Помечает цикл как проваленный (исключение верхнего уровня)."""
    run.finished_at = datetime.now(UTC)
    run.status = RunStatus.FAILED
    errors = list(run.errors or [])
    errors.append({"stage": "cycle", "error": error[:2000]})
    run.errors = errors
    if details:
        run.details = details
    await session.flush()
    logger.error("Цикл #%s провален: %s", run.id, error)
    return run
