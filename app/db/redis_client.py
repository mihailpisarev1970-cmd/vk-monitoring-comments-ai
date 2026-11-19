"""Асинхронный клиент Redis: блокировки циклов и конкурентная работа.

Используется для:
* межпроцессной блокировки запуска цикла мониторинга (ТЗ п.5.1);
* распределённой блокировки обработки публикации;
* лёгкого кэша (idempotency-ключи).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from redis.asyncio import Redis, from_url

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_redis: Redis | None = None


def get_redis() -> Redis:
    """Возвращает singleton-клиент Redis."""
    global _redis
    if _redis is None:
        _redis = from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
        )
    return _redis


async def close_redis() -> None:
    """Закрывает соединения Redis."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        logger.info("Redis connection closed")


async def ping_redis() -> bool:
    """Проверка доступности Redis."""
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001 - диагностика должна не падать
        logger.warning("Redis ping failed: %s", exc)
        return False


_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


@asynccontextmanager
async def redis_lock(
    key: str,
    ttl_seconds: int = 3600,
    blocking_timeout: float = 0.0,
    poll_interval: float = 0.5,
) -> AsyncIterator[bool]:
    """Распределённая блокировка на базе Redis SET NX PX.

    yield True — блокировка получена, False — занята другим процессом.
    Пример: async with redis_lock("lock:monitoring") as acquired: ...
    """
    client = get_redis()
    token = uuid.uuid4().hex
    deadline = asyncio.get_event_loop().time() + blocking_timeout

    acquired = False
    while True:
        acquired = bool(await client.set(key, token, nx=True, px=max(1, ttl_seconds) * 1000))
        if acquired or asyncio.get_event_loop().time() >= deadline:
            break
        await asyncio.sleep(poll_interval)

    try:
        yield acquired
    finally:
        if acquired:
            try:
                await client.eval(_RELEASE_LUA, 1, key, token)
            except Exception as exc:  # noqa: BLE001
                # Блокировка истечёт по TTL — логируем и продолжаем
                logger.warning("Failed to release redis lock %s: %s", key, exc)


_EXTEND_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


async def extend_lock(
    key: str,
    token: str,
    ttl_seconds: int,
    *,
    client: Redis | None = None,
) -> bool:
    """Продлевает блокировку, если она всё ещё принадлежит нам.

    Возвращает False, если блокировку уже перехватил другой процесс
    (например, после истечения TTL во время долгого цикла).
    """
    client = client or get_redis()
    try:
        result = await client.eval(_EXTEND_LUA, 1, key, token, max(1, ttl_seconds) * 1000)
    except Exception as exc:  # noqa: BLE001 - продление не критично
        logger.warning("Failed to extend redis lock %s: %s", key, exc)
        return False
    return bool(result)


class _LockHeartbeat:
    """Фоновая задача, продлевающая TTL блокировки во время работы цикла.

    Без продления цикл, идущий дольше TTL (по умолчанию 3600 с), терял
    блокировку — и следующий запуск мог стартовать параллельно.
    """

    def __init__(self, key: str, token: str, ttl_seconds: int, interval: float) -> None:
        self._key = key
        self._token = token
        self._ttl = ttl_seconds
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self.lost = False

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if not await extend_lock(self._key, self._token, self._ttl):
                self.lost = True
                logger.error("Блокировка %s потеряна — TTL не продлён", self._key)
                return

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None


@asynccontextmanager
async def cycle_lock(
    key: str,
    ttl_seconds: int = 3600,
    *,
    heartbeat: bool = True,
    interval: float | None = None,
) -> AsyncIterator[bool]:
    """Блокировка цикла с автоматическим продлением TTL.

    yield True — блокировка получена (и продлевается фоном),
    False — занята другим процессом.
    """
    client = get_redis()
    token = uuid.uuid4().hex
    acquired = bool(await client.set(key, token, nx=True, px=max(1, ttl_seconds) * 1000))
    period = interval if interval is not None else max(1.0, ttl_seconds / 3)
    beat = _LockHeartbeat(key, token, ttl_seconds, interval=period)
    try:
        if acquired and heartbeat:
            beat.start()
        yield acquired
    finally:
        await beat.stop()
        if acquired:
            try:
                await client.eval(_RELEASE_LUA, 1, key, token)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to release redis lock %s: %s", key, exc)


def build_lock_key(*parts: str) -> str:
    """Собирает ключ блокировки с префиксом окружения."""
    env = os.getenv("APP_ENV", settings.app_env)
    return ":".join(["vkmon", env, "lock", *parts])
