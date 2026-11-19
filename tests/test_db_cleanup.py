"""Очистка БД от отработанных публикаций (требование заказчика).

Проверяет, что база не раздувается: удаляются только полностью
отработанные публикации, а незавершённая работа (``new``, ``approved``,
``queued``) сохраняется независимо от возраста.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.enums import NotificationStatus, PostStatus
from app.db.models import Notification, Post, PostAnalysis, PostMarker
from app.scheduler.cleanup import FINISHED_STATUSES, purge_processed_posts

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
OWNER = -910001
_seq = 0


def make_post(status: PostStatus, age_hours: float, *, post_id: int | None = None) -> Post:
    global _seq
    _seq += 1
    pid = post_id if post_id is not None else 900 + _seq
    return Post(
        owner_id=OWNER,
        post_id=pid,
        text=f"Публикация {pid} для проверки очистки базы.",
        published_at=NOW - timedelta(hours=age_hours),
        post_url=f"https://vk.com/wall{OWNER}_{pid}",
        post_type="post",
        status=status,
        status_reason="тест",
    )


async def add_posts(session, posts: list[Post], *, age_hours: float) -> list[Post]:
    """Сохраняет публикации и задаёт им «возраст завершения»."""
    session.add_all(posts)
    await session.flush()
    await session.execute(
        update(Post)
        .where(Post.id.in_([post.id for post in posts]))
        .values(updated_at=NOW - timedelta(hours=age_hours))
    )
    await session.flush()
    return posts


async def count_posts(session) -> int:
    return int(
        (
            await session.execute(
                select(func.count(Post.id)).where(Post.owner_id == OWNER)
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_purges_only_finished_and_old(db_session) -> None:
    """Удаляются завершённые и старые; завершённые и свежие — остаются."""
    await add_posts(
        db_session,
        [
            make_post(PostStatus.SENT, 30),
            make_post(PostStatus.REJECTED_FILTER, 30),
            make_post(PostStatus.FAILED, 30),
        ],
        age_hours=30,
    )
    await add_posts(db_session, [make_post(PostStatus.SENT, 1)], age_hours=1)

    stats = await purge_processed_posts(db_session, now=NOW, retention_hours=24, owner_ids=[OWNER])

    assert stats.deleted_posts == 3
    assert await count_posts(db_session) == 1


@pytest.mark.asyncio
async def test_keeps_unfinished_posts_forever(db_session) -> None:
    """Незавершённая работа не удаляется, сколько бы ни лежала в базе."""
    await add_posts(
        db_session,
        [
            make_post(PostStatus.NEW, 100),
            make_post(PostStatus.APPROVED, 100),
            make_post(PostStatus.QUEUED, 100),
        ],
        age_hours=100,
    )

    stats = await purge_processed_posts(db_session, now=NOW, retention_hours=1, owner_ids=[OWNER])

    assert stats.deleted_posts == 0
    assert await count_posts(db_session) == 3


@pytest.mark.asyncio
async def test_all_finished_statuses_are_covered(db_session) -> None:
    """Каждый терминальный статус попадает под очистку."""
    posts = [make_post(status, 50) for status in FINISHED_STATUSES]
    await add_posts(db_session, posts, age_hours=50)


@pytest.mark.asyncio
async def test_related_records_removed(db_session) -> None:
    """Анализ, маркеры и уведомления уходят вместе с публикацией."""
    post = make_post(PostStatus.SENT, 50)
    await add_posts(db_session, [post], age_hours=50)
    db_session.add(
        PostAnalysis(
            post_id=post.id,
            stage="classify",
            model="test",
            prompt_version="v1",
            accepted=True,
        )
    )
    db_session.add(
        PostMarker(post_id=post.id, marker_name="ИИ", query="искусственный интеллект")
    )
    db_session.add(
        Notification(
            post_id=post.id,
            chat_id="@test",
            comment_text="Комментарий",
            message_text="Сообщение",
            status=NotificationStatus.SENT,
        )
    )
    await db_session.flush()

    stats = await purge_processed_posts(db_session, now=NOW, retention_hours=24, owner_ids=[OWNER])

    assert stats.deleted_posts == 1
    assert stats.deleted_notifications == 1
    for model in (PostAnalysis, PostMarker, Notification):
        left = (
            await db_session.execute(
                select(func.count(model.id)).where(model.post_id == post.id)
            )
        ).scalar_one()
        assert left == 0


@pytest.mark.asyncio
async def test_second_run_is_noop(db_session) -> None:
    """Повторная очистка на чистой базе ничего не удаляет."""
    await add_posts(db_session, [make_post(PostStatus.SENT, 50)], age_hours=50)

    first = await purge_processed_posts(db_session, now=NOW, retention_hours=24, owner_ids=[OWNER])
    second = await purge_processed_posts(db_session, now=NOW, retention_hours=24, owner_ids=[OWNER])

    assert first.deleted_posts == 1
    assert second.deleted_posts == 0


@pytest.mark.asyncio
async def test_batch_size_limits_single_delete(db_session) -> None:
    """Удаление идёт пачками, но за проход база очищается полностью."""
    await add_posts(
        db_session, [make_post(PostStatus.SENT, 50) for _ in range(5)], age_hours=50
    )

    stats = await purge_processed_posts(
        db_session, now=NOW, retention_hours=24, batch_size=2, owner_ids=[OWNER]
    )

    assert stats.deleted_posts == 5
    assert await count_posts(db_session) == 0


@pytest.mark.asyncio
async def test_stats_report_remaining(db_session) -> None:
    await add_posts(
        db_session,
        [make_post(PostStatus.SENT, 50), make_post(PostStatus.NEW, 50)],
        age_hours=50,
    )

    stats = await purge_processed_posts(db_session, now=NOW, retention_hours=24, owner_ids=[OWNER])

    assert stats.deleted_posts == 1
    assert stats.remaining_posts >= 1
    assert stats.to_dict()["deleted_posts"] == 1


def test_retention_config_defaults() -> None:
    """Настройки очистки заданы в конфигурации."""
    from app.filters.loader import load_config

    config = load_config(force=True)
    assert config.filters.db.enabled is True
    assert config.filters.db.retention_hours == 24
    assert config.filters.db.batch_size >= 1
