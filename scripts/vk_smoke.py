"""Диагностика модуля VK: реальный сбор по маркерам + сохранение в БД.

Запуск:  python -m scripts.vk_smoke
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.logging import setup_logging
from app.db.models import Post, PostMarker
from app.db.session import engine, session_scope
from app.vk.client import VKClient
from app.vk.models import MarkerQuery
from app.vk.repository import save_posts
from app.vk.search import fetch_posts, posts_to_summary


async def main() -> int:
    setup_logging(log_to_file=False)
    markers = [
        MarkerQuery(marker_name="Python", query="Python"),
        MarkerQuery(marker_name="Чат-боты", query="чат-бот"),
        MarkerQuery(marker_name="Программирование", query="#программирование"),
    ]

    async with VKClient() as client:
        result = await fetch_posts(client, markers, window_minutes=120, overlap_minutes=15)

    summary = posts_to_summary(result)
    print("\n=== Сводка сбора ===")
    for key, value in summary.items():
        if key != "errors":
            print(f"  {key}: {value}")
    for err in summary["errors"]:
        print(f"  ERROR: {err}")

    print("\n=== Примеры публикаций ===")
    for post in result.posts[:5]:
        print(
            f"  [{post.key}] ads={post.ad_flag.value} markers={post.marker_names} "
            f"chars={len(post.text)} url={post.post_url}"
        )
        print(f"      {post.text[:110].replace(chr(10), ' ')}")

    async with session_scope() as session:
        new_records, known = await save_posts(session, result.posts)
        print(f"\n=== Сохранение ===\n  новых: {len(new_records)}, известных: {known}")

    async with session_scope() as session:
        total_posts = len((await session.execute(select(Post.id))).all())
        total_markers = len((await session.execute(select(PostMarker.id))).all())
        ads = len(
            (await session.execute(select(Post.id).where(Post.marked_as_ads.is_(True)))).all()
        )
    print(f"  в БД: posts={total_posts}, post_markers={total_markers}, marked_as_ads={ads}")

    await engine.dispose()
    return 0 if not result.errors else (0 if len(result.errors) < result.queries_total else 1)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
