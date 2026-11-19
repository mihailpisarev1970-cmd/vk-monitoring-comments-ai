"""Тесты Redis-клиента и распределённой блокировки (требуют Redis)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core.config import settings
from app.db.redis_client import (
    build_lock_key,
    close_redis,
    cycle_lock,
    extend_lock,
    ping_redis,
    redis_lock,
)

pytestmark = pytest.mark.integration

_RELEASE_LUA = (
    'if redis.call("get", KEYS[1]) == ARGV[1] then '
    'return redis.call("del", KEYS[1]) else return 0 end'
)


@pytest_asyncio.fixture(autouse=True)
async def _reset_redis_singleton() -> AsyncIterator[None]:
    """Сбрасывает singleton-клиент Redis вокруг каждого теста.

    Клиент хранит соединения, привязанные к event loop: без сброса тест,
    использующий ``get_redis()``, падал с «Event loop is closed» после
    теста, который закрыл соединение в предыдущем loop.
    """
    try:
        yield
    finally:
        await close_redis()


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[aioredis.Redis]:
    """Свой клиент на каждый тест — соединения закрываются в активном loop."""
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


async def test_ping_redis(redis_client: aioredis.Redis) -> None:
    assert await ping_redis() is True


async def test_set_nx_is_exclusive(redis_client: aioredis.Redis) -> None:
    key = build_lock_key("test", "exclusive")
    try:
        assert await redis_client.set(key, "token-1", nx=True, px=5000) is True
        assert await redis_client.set(key, "token-2", nx=True, px=5000) is None
        await redis_client.eval(_RELEASE_LUA, 1, key, "token-1")
        assert await redis_client.set(key, "token-3", nx=True, px=5000) is True
        await redis_client.eval(_RELEASE_LUA, 1, key, "token-3")
    finally:
        await redis_client.delete(key)


async def test_redis_lock_exclusive(redis_client: aioredis.Redis) -> None:
    """Не допускает одновременный запуск двух циклов (ТЗ п.5.1)."""
    key = build_lock_key("test", "ctx")
    await redis_client.delete(key)
    async with redis_lock(key, ttl_seconds=5) as first:
        assert first is True
        async with redis_lock(key, ttl_seconds=5) as second:
            assert second is False
    async with redis_lock(key, ttl_seconds=5) as again:
        assert again is True


async def test_lock_key_has_environment_prefix() -> None:
    key = build_lock_key("monitoring")
    assert key.startswith("vkmon:")
    assert key.endswith(":lock:monitoring")


async def test_extend_lock_refreshes_only_own_token(redis_client: aioredis.Redis) -> None:
    """Продление работает только для своего токена.

    Без этого долгий цикл терял блокировку по TTL, и второй цикл
    стартовал параллельно (ТЗ п.5.1 — параллельные запуски запрещены).
    """
    key = build_lock_key("test", "extend")
    await redis_client.delete(key)
    try:
        await redis_client.set(key, "token-mine", nx=True, px=1000)
        assert await extend_lock(key, "token-mine", 30, client=redis_client) is True
        ttl = await redis_client.pttl(key)
        assert ttl > 20_000

        # Чужой токен не может продлить блокировку
        assert await extend_lock(key, "token-other", 30, client=redis_client) is False
    finally:
        await redis_client.delete(key)


async def test_cycle_lock_is_exclusive_and_released(redis_client: aioredis.Redis) -> None:
    """cycle_lock не допускает параллельный запуск и освобождается сам."""
    key = build_lock_key("test", "cycle-lock")
    await redis_client.delete(key)
    async with cycle_lock(key, ttl_seconds=60, heartbeat=False) as first:
        assert first is True
        async with cycle_lock(key, ttl_seconds=60, heartbeat=False) as second:
            assert second is False
    # После выхода блокировка снята — следующий цикл проходит
    async with cycle_lock(key, ttl_seconds=60, heartbeat=False) as again:
        assert again is True


async def test_cycle_lock_heartbeat_extends_ttl(redis_client: aioredis.Redis) -> None:
    """Heartbeat держит блокировку живой дольше исходного TTL.

    Без продления цикл, идущий дольше TTL, терял блокировку и второй
    запуск стартовал параллельно (ТЗ п.5.1).
    """
    key = build_lock_key("test", "heartbeat")
    await redis_client.delete(key)
    async with cycle_lock(key, ttl_seconds=2, interval=0.5) as acquired:
        assert acquired is True
        await asyncio.sleep(2.5)  # исходный TTL истёк бы без продления
        ttl_ms = await redis_client.pttl(key)
        assert ttl_ms > 0
        # Блокировка всё ещё наша: параллельный захват невозможен
        assert await redis_client.set(key, "other", nx=True, px=5000) is None

    # После выхода блокировка снята
    assert await redis_client.exists(key) == 0


async def test_cycle_lock_ttl_is_short_enough_for_stale_recovery(
    redis_client: aioredis.Redis,
) -> None:
    """Блокировка убитого процесса не должна мешать следующему циклу.

    Если процесс убит без graceful shutdown, ключ остаётся в Redis.
    При TTL в час плановый цикл пропускался бы; heartbeat продлевает
    TTL только пока процесс действительно жив, поэтому базовый TTL
    держим коротким (<= 15 минут).
    """
    from app.scheduler.monitoring import LOCK_TTL_SECONDS

    assert LOCK_TTL_SECONDS <= 900

    key = build_lock_key("test", "killed")
    await redis_client.delete(key)
    await redis_client.set(key, "dead-process-token", nx=True, px=800)
    await asyncio.sleep(1.0)
    # Ключ истёк — следующий цикл может стартовать
    assert await redis_client.exists(key) == 0
