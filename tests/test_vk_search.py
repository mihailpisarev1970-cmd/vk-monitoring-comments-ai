"""Тесты сбора публикаций и дедупликации (ТЗ п.6.1, п.6.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from app.vk.client import VKClient
from app.vk.models import MarkerQuery, SearchWindow
from app.vk.search import build_window, fetch_posts, posts_to_summary, search_markers


def make_item(owner_id: int, post_id: int, text: str = "Python пост", ads: int = 0) -> dict:
    return {
        "id": post_id,
        "owner_id": owner_id,
        "from_id": owner_id,
        "date": 1794908089,
        "text": text,
        "marked_as_ads": ads,
        "post_type": "post",
        "comments": {"count": 0},
        "likes": {"count": 0},
        "reposts": {"count": 0},
    }


class FakeVK:
    """Эмулирует VK API на уровне HTTP-транспорта (реальные ответы по формату)."""

    def __init__(self, items_by_query: dict[str, list[dict]], error: dict | None = None):
        self.items_by_query = items_by_query
        self.error = error
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        query = ""
        for part in body.split("&"):
            if part.startswith("q="):
                from urllib.parse import unquote_plus

                query = unquote_plus(part[2:])
        self.calls.append(query)
        if self.error:
            return httpx.Response(200, json={"error": self.error})
        items = self.items_by_query.get(query, [])
        return httpx.Response(
            200,
            json={
                "response": {
                    "count": len(items),
                    "total_count": len(items),
                    "items": items,
                    "profiles": [],
                    "groups": [],
                }
            },
        )


def make_client(handler: FakeVK) -> VKClient:
    async def no_sleep(_delay: float) -> None:
        return None

    return VKClient(
        access_token="t",
        api_version="5.199",
        min_interval=0,
        max_retries=1,
        transport=httpx.MockTransport(handler),
        sleep=no_sleep,
    )


WINDOW = SearchWindow(
    start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    end=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
)


def test_build_window_has_overlap() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    window = build_window(now, window_minutes=120, overlap_minutes=15)
    assert window.end == now
    assert window.start == now - timedelta(minutes=135)


def test_build_window_naive_datetime_becomes_utc() -> None:
    window = build_window(datetime(2026, 1, 1, 12, 0), window_minutes=60, overlap_minutes=0)
    assert window.end.tzinfo == UTC


async def test_deduplicates_across_queries() -> None:
    """Одна публикация из разных запросов сохраняется один раз со всеми маркерами."""
    shared = make_item(-100, 1, "Python и IT")
    handler = FakeVK({"Python": [shared, make_item(-100, 2)], "IT": [shared]})
    client = make_client(handler)
    markers = [
        MarkerQuery(marker_name="Python", query="Python"),
        MarkerQuery(marker_name="IT", query="IT"),
    ]
    try:
        result = await search_markers(client, markers, WINDOW)
    finally:
        await client.close()

    assert len(result.posts) == 2
    assert result.total_received == 3
    assert result.duplicates_skipped == 1
    assert result.queries_total == 2
    assert result.queries_failed == 0

    shared_post = next(p for p in result.posts if p.post_id == 1)
    assert shared_post.marker_names == ["Python", "IT"]
    assert len(shared_post.matched_markers) == 2


async def test_query_error_recorded_and_others_continue() -> None:
    """Ошибка одного запроса не прерывает сбор (кроме авторизации)."""
    handler = FakeVK(
        {}, error={"error_code": 100, "error_msg": "invalid count"}
    )
    client = make_client(handler)
    markers = [MarkerQuery(marker_name="Python", query="Python")]
    try:
        result = await search_markers(client, markers, WINDOW)
    finally:
        await client.close()
    assert result.queries_failed == 1
    assert result.posts == []
    assert result.errors[0]["error_code"] == 100
    assert result.errors[0]["retryable"] is False


async def test_auth_error_stops_collection() -> None:
    handler = FakeVK({}, error={"error_code": 5, "error_msg": "auth failed"})
    client = make_client(handler)
    markers = [
        MarkerQuery(marker_name="Python", query="Python"),
        MarkerQuery(marker_name="IT", query="IT"),
    ]
    try:
        result = await search_markers(client, markers, WINDOW)
    finally:
        await client.close()
    assert result.queries_failed == 1
    assert len(handler.calls) == 1  # второй запрос не выполняется


async def test_official_ads_are_marked_but_kept() -> None:
    """Публикации с рекламной меткой сохраняются, но помечаются для rejected_ad."""
    handler = FakeVK({"Python": [make_item(-100, 1, ads=1), make_item(-100, 2, ads=0)]})
    client = make_client(handler)
    try:
        result = await search_markers(
            client, [MarkerQuery(marker_name="Python", query="Python")], WINDOW
        )
    finally:
        await client.close()
    ads_posts = [p for p in result.posts if p.is_official_ad]
    assert len(ads_posts) == 1
    assert posts_to_summary(result)["official_ads"] == 1




async def test_partial_url_sharing_does_not_create_duplicates() -> None:
    """Похожие тексты с общим URL должны иметь разные хеши.

    Регрессия из реальной выборки: разные сообщества публикуют текст с общим
    URL (https://habr.com/ru/articles/11088756/). Если хеши совпадают,
    все записи, кроме первой, сохраняются без хеша и в следующий цикл повторно
    уходят в LLM.
    """
    url = "https://habr.com/ru/articles/11088756/"
    first = make_item(-200, 1, f"Очередь постов для Telegram на Python. Читать далее {url}")
    second = make_item(-201, 1, f"Очередь постов для Telegram на Python, разбор. Подробнее: {url}")
    handler = FakeVK({"Python": [first, second]})
    client = make_client(handler)
    try:
        result = await search_markers(
            client, [MarkerQuery(marker_name="Python", query="Python")], WINDOW
        )
    finally:
        await client.close()

    assert len(result.posts) == 2
    hashes = {post.text_hash for post in result.posts}
    assert len(hashes) == 2, "хеши текстов должны различаться"


async def test_identical_text_from_different_sources_same_hash() -> None:
    """Полностью идентичный текст даёт одинаковый хеш (это не регрессия).

    Такие публикации сохраняются обе, но хеш остаётся только у первой —
    дальше их различает duplicate_filter (ТЗ п.11.4).
    """
    text = "Одинаковый рекламный текст без ссылок"
    handler = FakeVK({"Python": [make_item(-210, 1, text), make_item(-211, 1, text)]})
    client = make_client(handler)
    try:
        result = await search_markers(
            client, [MarkerQuery(marker_name="Python", query="Python")], WINDOW
        )
    finally:
        await client.close()
    hashes = {post.text_hash for post in result.posts}
    assert len(hashes) == 1

async def test_fetch_posts_without_markers() -> None:
    handler = FakeVK({})
    client = make_client(handler)
    try:
        result = await fetch_posts(client, [])
    finally:
        await client.close()
    assert result.posts == []
    assert handler.calls == []


async def test_fetch_posts_uses_window() -> None:
    handler = FakeVK({"Python": [make_item(-100, 1)]})
    client = make_client(handler)
    try:
        result = await fetch_posts(
            client,
            [MarkerQuery(marker_name="Python", query="Python")],
            now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            window_minutes=60,
            overlap_minutes=0,
        )
    finally:
        await client.close()
    assert len(result.posts) == 1
    assert result.posts[0].matched_markers[0].query == "Python"


def test_summary_counts() -> None:
    from app.vk.models import SearchResult

    result = SearchResult(queries_total=2, queries_failed=1, total_received=5, duplicates_skipped=2)
    summary = posts_to_summary(result)
    assert summary["queries_total"] == 2
    assert summary["queries_failed"] == 1
    assert summary["duplicates_skipped"] == 2
    assert summary["unique_posts"] == 0

