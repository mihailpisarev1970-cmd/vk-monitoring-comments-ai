"""Тесты суточного лимита LLM и отбора свежих публикаций.

Требование заказчика: в базу и в LLM попадают только публикации не старше
``monitoring.max_age_hours`` (4 часа), а LLM обрабатывает не более
``llm.max_posts_per_cycle`` (25) публикаций в сутки.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.scheduler import quota
from app.scheduler.monitoring import MonitoringCycle
from app.vk.models import NormalizedPost, SearchResult, SearchWindow
from app.vk.search import filter_fresh_posts

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def make_norm(offset_hours: float, post_id: int = 1) -> NormalizedPost:
    return NormalizedPost(
        owner_id=-100,
        post_id=post_id,
        text="Тестовая публикация про искусственный интеллект в школе.",
        published_at=NOW - timedelta(hours=offset_hours),
        post_url=f"https://vk.com/wall-100_{post_id}",
    )


def make_post(offset_hours: float) -> SimpleNamespace:
    """Минимальный объект с ``published_at`` вместо ORM-записи."""
    return SimpleNamespace(published_at=NOW - timedelta(hours=offset_hours))


# ------------------------------------------------------- свежесть постов
def test_filter_fresh_posts_keeps_only_recent() -> None:
    window = SearchWindow(start=NOW - timedelta(hours=4), end=NOW)
    result = SearchResult(
        posts=[
            make_norm(0.5, 1),
            make_norm(3.9, 2),
            make_norm(4.1, 3),
            make_norm(10, 4),
        ]
    )

    dropped = filter_fresh_posts(result, now=NOW, max_age_hours=4, window=window)

    assert dropped == 2
    assert {post.post_id for post in result.posts} == {1, 2}


def test_filter_fresh_posts_drops_future_posts() -> None:
    """Дата «из будущего» — ошибка источника или часового пояса."""
    window = SearchWindow(start=NOW - timedelta(hours=4), end=NOW)
    result = SearchResult(posts=[make_norm(-1, 9)])

    dropped = filter_fresh_posts(result, now=NOW, max_age_hours=4, window=window)

    assert dropped == 1
    assert result.posts == []


def test_filter_fresh_posts_empty_result() -> None:
    window = SearchWindow(start=NOW - timedelta(hours=4), end=NOW)
    assert filter_fresh_posts(SearchResult(), now=NOW, max_age_hours=4, window=window) == 0


# ------------------------------------------------- проверка перед LLM
def test_is_stale_marks_old_posts() -> None:
    fresh = make_post(2)
    old = make_post(5)
    naive = SimpleNamespace(published_at=(NOW - timedelta(hours=2)).replace(tzinfo=None))

    assert MonitoringCycle._is_stale(fresh, now=NOW, max_age_hours=4) is False
    assert MonitoringCycle._is_stale(old, now=NOW, max_age_hours=4) is True
    assert MonitoringCycle._is_stale(naive, now=NOW, max_age_hours=4) is False


def test_is_stale_detects_future_dates() -> None:
    future = make_post(-3)
    assert MonitoringCycle._is_stale(future, now=NOW, max_age_hours=4) is True


def test_drop_stale_partitions_queue() -> None:
    posts = [make_post(hours) for hours in (1, 2, 5, 6)]

    kept, dropped = MonitoringCycle._drop_stale(posts, now=NOW, max_age_hours=4)

    assert dropped == 2
    assert [p.published_at for p in kept] == [
        NOW - timedelta(hours=1),
        NOW - timedelta(hours=2),
    ]


# ------------------------------------------------------- суточный лимит
class FakeRedis:
    """Минимальная эмуляция Redis: get/incr/decr/expire/exists."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def incr(self, key: str) -> int:
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def decr(self, key: str) -> int:
        self.store[key] = int(self.store.get(key, 0)) - 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis = FakeRedis()
    monkeypatch.setattr("app.db.redis_client.get_redis", lambda: redis)
    return redis


def test_usage_key_depends_on_local_date() -> None:
    msk_evening = datetime(2026, 11, 19, 21, 30, tzinfo=UTC)  # 00:30 МСК 20 ноября
    key = quota.usage_key(msk_evening, "Europe/Moscow")
    assert key == "llm:daily_usage:2026-11-20"


def test_seconds_until_reset_positive() -> None:
    assert 0 < quota.seconds_until_reset(NOW, "Europe/Moscow") <= 86400


@pytest.mark.asyncio
async def test_reserve_blocks_after_limit(fake_redis: FakeRedis) -> None:
    limit = 3
    slots = [await quota.reserve(NOW, "Europe/Moscow", limit) for _ in range(4)]

    assert slots == [1, 2, 3, 0]
    assert await quota.daily_used(NOW, "Europe/Moscow") == 3


@pytest.mark.asyncio
async def test_reserve_sets_ttl_for_new_day(fake_redis: FakeRedis) -> None:
    await quota.reserve(NOW, "Europe/Moscow", 25)

    assert len(fake_redis.ttls) == 1
    assert next(iter(fake_redis.ttls.values())) > 0


@pytest.mark.asyncio
async def test_release_returns_slot(fake_redis: FakeRedis) -> None:
    await quota.reserve(NOW, "Europe/Moscow", 2)
    await quota.reserve(NOW, "Europe/Moscow", 2)
    assert await quota.reserve(NOW, "Europe/Moscow", 2) == 0

    await quota.release(NOW, "Europe/Moscow")

    assert await quota.reserve(NOW, "Europe/Moscow", 2) == 2


@pytest.mark.asyncio
async def test_limit_resets_on_next_day(fake_redis: FakeRedis) -> None:
    await quota.reserve(NOW, "Europe/Moscow", 1)
    assert await quota.reserve(NOW, "Europe/Moscow", 1) == 0

    tomorrow = NOW + timedelta(days=1)
    assert await quota.reserve(tomorrow, "Europe/Moscow", 1) == 1
