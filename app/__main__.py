"""Точка входа сервиса VK Monitor Assistant.

Режимы запуска:

* ``python -m app`` — полноценный сервис: проверка окружения, восстановление
  пропущенных окон, планировщик (поиск публикаций в 10:00, 11:00, 18:00,
  19:00 MSK и отправка очереди уведомлений);
* ``python -m app --once`` — один цикл мониторинга и выход (для проверки);
* ``python -m app --check`` — только проверка окружения.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.redis_client import close_redis, ping_redis
from app.db.session import engine

logger = get_logger(__name__)


async def _preflight() -> bool:
    """Проверка доступности БД, Redis, миграций и обязательных ключей."""
    from sqlalchemy import text

    ok = True
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
            if not revision:
                logger.error("Миграции не применены. Выполните: alembic upgrade head")
                ok = False
            else:
                logger.info("PostgreSQL доступна, revision=%s", revision)
    except Exception as exc:  # noqa: BLE001
        logger.error("PostgreSQL недоступна: %s", exc)
        ok = False

    if not await ping_redis():
        logger.error("Redis недоступен: %s", settings.redis_url)
        ok = False

    if not settings.vk_access_token.get_secret_value():
        logger.error("VK_ACCESS_TOKEN не задан")
        ok = False
    if not settings.aidostup_api_key.get_secret_value():
        logger.error("AIDOSTUP_API_KEY не задан")
        ok = False
    if not settings.telegram_bot_token.get_secret_value() or not settings.telegram_chat_id:
        logger.error("Telegram не настроен (token/chat_id)")
        ok = False

    return ok


async def run_once() -> int:
    """Один цикл мониторинга (для отладки и ручного запуска)."""
    from app.scheduler.monitoring import MonitoringCycle

    result = await MonitoringCycle().run(trigger="manual")
    logger.info("Результат цикла: %s", result.to_dict())
    return 0 if result.status in {"success", "partial", "skipped"} else 1


async def run_service() -> int:
    """Основной режим: планировщик и восстановление пропусков."""
    from app.scheduler.runner import SCHEDULE_HOURS, build_scheduler, run_missed_intervals

    await run_missed_intervals()

    scheduler = build_scheduler()
    scheduler.start()
    hours = ", ".join(f"{hour:02d}:00" for hour in SCHEDULE_HOURS)
    logger.info(
        "%s запущен. Поиск публикаций: %s (часовой пояс %s)",
        settings.app_name,
        hours,
        settings.timezone,
    )
    for job in scheduler.get_jobs():
        logger.info("Задача «%s»: следующее выполнение %s", job.name, job.next_run_time)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Остановка по сигналу")
    finally:
        scheduler.shutdown(wait=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="VK Monitor Assistant")
    parser.add_argument("--once", action="store_true", help="выполнить один цикл и выйти")
    parser.add_argument("--check", action="store_true", help="только проверить окружение")
    args = parser.parse_args()

    setup_logging()

    async def _main() -> int:
        logger.info(
            "%s стартует (env=%s, tz=%s)",
            settings.app_name,
            settings.app_env,
            settings.timezone,
        )
        if not await _preflight():
            return 1
        try:
            if args.check:
                logger.info("Проверка окружения пройдена")
                return 0
            if args.once:
                return await run_once()
            return await run_service()
        finally:
            await close_redis()
            await engine.dispose()

    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")
        return 0


if __name__ == "__main__":
    sys.exit(main())
