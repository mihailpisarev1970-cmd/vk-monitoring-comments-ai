"""Суточный лимит обработки публикаций LLM (требование заказчика).

LLM должна обрабатывать не более ``llm.max_posts_per_cycle`` публикаций
**в сутки**. Ограничение общее для всех циклов суток, поэтому счётчик
хранится в Redis с TTL до конца суток (Europe/Moscow): при смене даты
ключ истекает и лимит автоматически возобновляется.

Счётчик ведётся в Redis, а не в PostgreSQL, чтобы:
* не зависеть от того, сколько записей ``post_analysis`` появилось
  сегодня (публикация может обрабатываться повторно);
* работать при нескольких процессах (атомарный ``INCR``).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Префикс ключа суточного счётчика
USAGE_KEY_PREFIX = "llm:daily_usage"


def usage_key(now: datetime, timezone_name: str) -> str:
    """Ключ счётчика на текущую дату в часовом поясе сервиса."""
    local = now.astimezone(ZoneInfo(timezone_name))
    return f"{USAGE_KEY_PREFIX}:{local.date().isoformat()}"


def seconds_until_reset(now: datetime, timezone_name: str) -> int:
    """Сколько секунд остаётся до начала следующих суток (минимум 1)."""
    tz = ZoneInfo(timezone_name)
    local = now.astimezone(tz)
    tomorrow = datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=tz)
    return max(1, int((tomorrow - local).total_seconds()))


async def daily_used(now: datetime, timezone_name: str) -> int:
    """Сколько публикаций LLM уже обработала за текущие сутки."""
    from app.db.redis_client import get_redis

    try:
        value = await get_redis().get(usage_key(now, timezone_name))
    except Exception as exc:  # noqa: BLE001 - сбой Redis не должен блокировать цикл
        logger.warning("Не удалось прочитать суточный счётчик LLM: %s", exc)
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def reserve(now: datetime, timezone_name: str, limit: int) -> int:
    """Резервирует место под очередную публикацию.

    Возвращает новое значение счётчика, если лимит не исчерпан, иначе 0.
    Резерв освобождается через :func:`release`, если публикация так и не
    была отправлена в LLM (ошибка на этапе фильтров и т. п.).
    """
    from app.db.redis_client import get_redis

    key = usage_key(now, timezone_name)
    ttl = seconds_until_reset(now, timezone_name)
    try:
        redis = get_redis()
        used = await redis.incr(key)
        if used == 1:
            await redis.expire(key, ttl)
        if used > limit:
            await redis.decr(key)
            return 0
        return int(used)
    except Exception as exc:  # noqa: BLE001
        # Redis недоступен — считаем, что лимит не исчерпан: лучше отправить
        # уведомление, чем молча пропустить публикацию.
        logger.warning("Не удалось учесть суточный лимит LLM: %s", exc)
        return -1


async def release(now: datetime, timezone_name: str) -> None:
    """Возвращает зарезервированное место (публикация не ушла в LLM)."""
    from app.db.redis_client import get_redis

    try:
        redis = get_redis()
        key = usage_key(now, timezone_name)
        if await redis.exists(key):
            await redis.decr(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось освободить место в суточном лимите LLM: %s", exc)
