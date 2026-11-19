"""Планировщик APScheduler (ТЗ раздел 5).

Требования:
* мониторинг запускается в двух окнах: **10:00–12:00** и **18:00–20:00**
  (запуски в 10:00, 11:00, 18:00, 19:00 по ``Europe/Moscow``);
* часовой пояс — ``Europe/Moscow`` (корректная обработка переходов времени);
* исключается одновременный запуск двух циклов (Redis-блокировка + ``max_instances=1``);
* после восстановления работы проверяются пропущенные интервалы;
* каждая итерация очереди уведомлений выполняется чаще, чтобы ретраи
  отправлялись без ожидания следующего окна.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.scheduler.monitoring import CycleResult, MonitoringCycle, reap_stale_runs
from app.scheduler.watermark import load_watermark, missed_intervals
from app.telegram.queue import NotificationWorker
from app.telegram.sender import TelegramSender

logger = get_logger(__name__)

#: Часы запуска мониторинга (Europe/Moscow).
#: Задано заказчиком: два окна в сутки — утреннее 10:00–12:00 и вечернее
#: 17:00–19:00 (по запуску в начале каждого часа).
#: На этапе поиска в базу отбираются только публикации не старше
#: ``monitoring.max_age_hours`` (4 часа), поэтому каждое окно охватывает
#: предшествующие 4 часа и пропусков не остаётся.
SCHEDULE_HOURS = [10, 11, 12, 17, 18, 19]
#: Период проверки очереди уведомлений
QUEUE_INTERVAL_MINUTES = 5


def build_scheduler(cycle: MonitoringCycle | None = None) -> AsyncIOScheduler:
    """Создаёт планировщик с задачами мониторинга и доставки."""
    tz = ZoneInfo(settings.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)
    monitoring = cycle or MonitoringCycle()

    scheduler.add_job(
        partial(_safe_run, monitoring),
        trigger=CronTrigger(hour=",".join(str(h) for h in SCHEDULE_HOURS), minute=0, timezone=tz),
        id="monitoring",
        name="Поиск публикаций ВКонтакте",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        replace_existing=True,
    )

    scheduler.add_job(
        _flush_queue,
        trigger=IntervalTrigger(minutes=QUEUE_INTERVAL_MINUTES, timezone=tz),
        id="telegram_queue",
        name="Отправка очереди уведомлений",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    return scheduler


async def _safe_run(cycle: MonitoringCycle) -> CycleResult:
    """Запуск цикла, который не выбрасывает исключения наружу."""
    try:
        return await cycle.run(trigger="schedule")
    except Exception as exc:  # noqa: BLE001 - планировщик не должен останавливаться
        logger.exception("Необработанная ошибка цикла мониторинга: %s", exc)
        return CycleResult(status="failed", reason=str(exc))


async def _flush_queue() -> None:
    """Досылает очередь уведомлений (ретраи по backoff)."""
    try:
        async with session_scope() as session, TelegramSender() as sender:
            stats = await NotificationWorker(sender).process_due(session)
        if stats.sent or stats.failed or stats.errors:
            logger.info("Очередь Telegram: %s", stats.to_dict())
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось обработать очередь уведомлений: %s", exc)


async def run_missed_intervals(cycle: MonitoringCycle | None = None) -> list[CycleResult]:
    """Проверяет пропущенные плановые интервалы после простоя (ТЗ п.5.1).

    Вызывается один раз при старте сервиса.
    """
    monitoring = cycle or MonitoringCycle()
    async with session_scope() as session:
        # Цикл, убитый посреди работы, оставил бы запись в статусе running
        await reap_stale_runs(session)
        watermark = await load_watermark(session)

    intervals = missed_intervals(
        watermark,
        now=datetime.now(UTC),
        schedule_hours=SCHEDULE_HOURS,
        timezone_name=settings.timezone,
        window_minutes=120,
        overlap_minutes=15,
    )
    if not intervals:
        return []

    logger.warning(
        "Обнаружены пропущенные интервалы: %s", [(a.isoformat(), b.isoformat()) for a, b in intervals]
    )
    results: list[CycleResult] = []
    for _start, _end in intervals[-3:]:  # не более трёх последних окон за раз
        results.append(await _safe_run(monitoring))
    return results
