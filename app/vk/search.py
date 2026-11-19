"""Сбор публикаций по маркерам через `newsfeed.search` (ТЗ п.4, п.6.1, п.6.3).

Ключевые ограничения VK (проверены экспериментально, см. docs/vk_api_check.md):
* пагинация (`start_from`) с сервисным ключом недоступна;
* один запрос отдаёт не более 200 последних публикаций в окне;
* порядок выдачи между вызовами нестабилен.

Поэтому используется rolling window ([start, end]) и дедупликация по
составному ключу (owner_id, post_id) — ТЗ п.6.3.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.vk.client import VKClient
from app.vk.exceptions import VKError
from app.vk.models import MarkerQuery, SearchResult, SearchWindow
from app.vk.normalizer import normalize_response

logger = get_logger(__name__)

DEFAULT_COUNT = 200


def filter_fresh_posts(
    result: SearchResult,
    *,
    now: datetime,
    max_age_hours: int,
    window: SearchWindow,
) -> int:
    """Оставляет только свежие публикации (требование заказчика).

    Публикация считается свежей, если она опубликована не более
    ``max_age_hours`` назад и не позже текущего момента. Отброшенные
    публикации не попадают в PostgreSQL, а значит и в LLM.

    Возвращает количество отброшенных публикаций.
    """
    if not result.posts:
        return 0

    now = now.astimezone(UTC)
    threshold = now - timedelta(hours=max_age_hours)
    fresh = [
        post
        for post in result.posts
        if threshold <= post.published_at.astimezone(UTC) <= now
    ]
    dropped = len(result.posts) - len(fresh)
    result.posts = fresh
    if dropped:
        logger.info(
            "Отброшено несвежих публикаций: %s (порог %s, окно %s..%s)",
            dropped,
            threshold.isoformat(),
            window.start.isoformat(),
            window.end.isoformat(),
        )
    return dropped


def build_window(
    now: datetime | None = None, *, window_minutes: int = 120, overlap_minutes: int = 0
) -> SearchWindow:
    """Строит окно поиска `[now - window, now]` с запасом по времени.

    `now` приводится к UTC. Перекрытие добавляется на этап раньше, чтобы
    не потерять публикации между циклами (ТЗ п.5.1).
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now - timedelta(minutes=window_minutes + overlap_minutes)
    return SearchWindow(start=start, end=now)


def merge_posts(result: SearchResult, posts: list, marker: MarkerQuery) -> None:
    """Добавляет партию публикаций в результат с дедупликацией по ключу."""
    index = {post.key: post for post in result.posts}
    for post in posts:
        existing = index.get(post.key)
        if existing is not None:
            # Публикация уже встречена — сохраняем все маркеры (ТЗ п.6.3)
            existing.add_marker(marker)
            result.duplicates_skipped += 1
        else:
            result.posts.append(post)
            index[post.key] = post


async def search_markers(
    client: VKClient,
    markers: list[MarkerQuery],
    window: SearchWindow,
    *,
    count: int = DEFAULT_COUNT,
    stop_on_auth_error: bool = True,
) -> SearchResult:
    """Выполняет поиск по каждому маркеру в заданном окне.

    Ошибки отдельных запросов фиксируются, но не прерывают сбор целиком;
    при ошибке авторизации работа прекращается.
    """
    result = SearchResult()
    for marker in markers:
        result.queries_total += 1
        try:
            payload = await client.newsfeed_search(
                query=marker.query,
                start_time=window.start_ts,
                end_time=window.end_ts,
                count=count,
            )
        except VKError as exc:
            result.queries_failed += 1
            result.errors.append(
                {
                    "query": marker.query,
                    "marker_name": marker.marker_name,
                    "error_code": getattr(exc, "error_code", None),
                    "error": str(exc),
                    "retryable": exc.is_retryable,
                }
            )
            logger.error(
                "Поиск по %r (%s) не удался: %s", marker.query, marker.marker_name, exc
            )
            if stop_on_auth_error and not exc.is_retryable:
                from app.vk.exceptions import VKAccessError, VKAuthError

                if isinstance(exc, VKAuthError | VKAccessError):
                    logger.error("Прерывание сбора: проблема с доступом к VK API")
                    break
            continue

        posts = normalize_response(payload, marker)
        result.total_received += len(posts)
        merge_posts(result, posts, marker)
        logger.info(
            "Запрос %r: получено %s публикаций (count=%s), новых после дедупликации: %s",
            marker.query,
            len(posts),
            payload.get("count"),
            len(result.posts),
        )
    return result


async def fetch_posts(
    client: VKClient,
    markers: list[MarkerQuery],
    *,
    now: datetime | None = None,
    window_minutes: int = 120,
    overlap_minutes: int = 0,
    count: int = DEFAULT_COUNT,
) -> SearchResult:
    """Высокоуровневый сбор: окно + поиск по всем маркерам."""
    if not markers:
        logger.warning("Нет активных маркеров — поиск не выполняется")
        return SearchResult()
    window = build_window(now, window_minutes=window_minutes, overlap_minutes=overlap_minutes)
    logger.info(
        "Старт поиска: %s маркеров, окно %s..%s (UTC)",
        len(markers),
        window.start.isoformat(),
        window.end.isoformat(),
    )
    return await search_markers(client, markers, window, count=count)


def posts_to_summary(result: SearchResult) -> dict[str, Any]:
    """Компактная сводка для логов и monitoring_runs.details."""
    return {
        "queries_total": result.queries_total,
        "queries_failed": result.queries_failed,
        "total_received": result.total_received,
        "unique_posts": len(result.posts),
        "duplicates_skipped": result.duplicates_skipped,
        "official_ads": sum(1 for post in result.posts if post.is_official_ad),
        "errors": copy.deepcopy(result.errors),
    }
