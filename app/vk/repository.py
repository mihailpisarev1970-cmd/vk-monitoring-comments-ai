"""Репозиторий публикаций: сохранение и дедупликация (ТЗ п.6.3, п.15.1)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Post, PostMarker
from app.vk.models import NormalizedPost

logger = get_logger(__name__)


def _post_key(owner_id: int, post_id: int) -> tuple[int, int]:
    return owner_id, post_id


async def find_existing_keys(
    session: AsyncSession, pairs: list[tuple[int, int]]
) -> set[tuple[int, int]]:
    """Возвращает ключи (owner_id, post_id), уже присутствующие в БД."""
    if not pairs:
        return set()
    unique = list({_post_key(o, p) for o, p in pairs})
    rows = await session.execute(
        select(Post.owner_id, Post.post_id).where(
            tuple_(Post.owner_id, Post.post_id).in_(unique)
        )
    )
    return {(row[0], row[1]) for row in rows}


async def save_post(session: AsyncSession, post: NormalizedPost) -> tuple[Post, bool]:
    """Сохраняет публикацию; возвращает (запись, создана_ли_новая).

    Конфликт по (owner_id, post_id) или по точному дубликату текста
    (`uq_posts_text_hash_not_null`) не приводит к ошибке: существующая запись
    переиспользуется, а маркеры добавляются к ней.
    """
    values: dict[str, Any] = {
        "owner_id": post.owner_id,
        "post_id": post.post_id,
        "author_id": post.author_id,
        "author_name": post.author_name,
        "text": post.text,
        "published_at": post.published_at,
        "post_url": post.post_url,
        "raw_payload": post.raw_payload,
        "comments_count": post.comments_count,
        "likes_count": post.likes_count,
        "reposts_count": post.reposts_count,
        "views_count": post.views_count,
        "marked_as_ads": post.marked_as_ads,
        "ad_flag": post.ad_flag,
        "post_type": post.post_type,
        "text_hash": post.text_hash,
    }
    stmt = (
        pg_insert(Post)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["owner_id", "post_id"])
        .returning(Post.id)
    )
    new_id = await session.scalar(stmt)
    created = new_id is not None

    if created:
        record = await session.get(Post, new_id)
    else:
        record = await session.scalar(
            select(Post).where(
                (Post.owner_id == post.owner_id) & (Post.post_id == post.post_id)
            )
        )
        if record is None and post.text_hash:
            # Публикация не вставлена по уникальности хеша текста —
            # связываем её с уже известной записью (ТЗ п.11.4)
            record = await session.scalar(
                select(Post).where(Post.text_hash == post.text_hash)
            )
    if record is None:
        raise RuntimeError(
            f"Не удалось сохранить публикацию {post.owner_id}_{post.post_id}"
        )
    return record, created


async def add_markers(session: AsyncSession, post_db_id: int, post: NormalizedPost) -> int:
    """Сохраняет все маркеры публикации без дубликатов. Возвращает число добавленных."""
    if not post.matched_markers:
        return 0
    values = [
        {
            "post_id": post_db_id,
            "marker_name": marker.marker_name,
            "query": marker.query,
        }
        for marker in post.matched_markers
    ]
    stmt = (
        pg_insert(PostMarker)
        .values(values)
        .on_conflict_do_nothing(constraint="uq_post_markers_post_marker_query")
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def save_posts(
    session: AsyncSession, posts: list[NormalizedPost]
) -> tuple[list[Post], int]:
    """Сохраняет партию публикаций, помечая дубликаты.

    Возвращает (список новых записей, количество уже известных публикаций).

    Ограничение уникальности text_hash (ТЗ п.11.4) защищает от дублей, но не
    должно терять публикации: если такой текст уже есть у другой записи,
    новая сохраняется с пустым хешем, а similarity-анализ выполняется фильтром.
    """
    new_records: list[Post] = []
    known = 0
    duplicate_texts = 0

    # Хеши текстов, уже занятые в БД, и занятые внутри самой партии
    incoming_hashes = {p.text_hash for p in posts if p.text_hash}
    taken_hashes: set[str] = set()
    if incoming_hashes:
        rows = await session.execute(
            select(Post.text_hash).where(Post.text_hash.in_(incoming_hashes))
        )
        taken_hashes = {row[0] for row in rows if row[0]}
    logger.info(
        "Проверка дублей: входящих хешей %s, уже занято в БД %s",
        len(incoming_hashes),
        len(taken_hashes),
    )

    existing = await find_existing_keys(session, [(_p.owner_id, _p.post_id) for _p in posts])
    for post in posts:
        same_publication = _post_key(post.owner_id, post.post_id) in existing
        if same_publication:
            known += 1  # публикация уже обрабатывалась (ТЗ п.6.3)

        if post.text_hash is not None:
            if post.text_hash in taken_hashes:
                duplicate_texts += 1
                post.text_hash = None
            else:
                taken_hashes.add(post.text_hash)

        record, created = await save_post(session, post)
        await add_markers(session, record.id, post)
        if created and not same_publication:
            new_records.append(record)

    await session.flush()
    logger.info(
        "Сохранено публикаций: новых %s, уже известных %s, дублей текста %s (в партии %s)",
        len(new_records),
        known,
        duplicate_texts,
        len(posts),
    )
    return new_records, known
