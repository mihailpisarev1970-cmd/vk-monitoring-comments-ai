"""Интеграционные тесты репозитория публикаций (требуют PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.enums import AdFlag, PostStatus
from app.db.models import Post, PostMarker
from app.vk.models import MarkerQuery, NormalizedPost
from app.vk.normalizer import compute_text_hash
from app.vk.repository import find_existing_keys, save_posts

pytestmark = pytest.mark.integration


def make_post(
    owner_id: int = -9001,
    post_id: int = 1,
    text: str = "Уникальный текст",
    *,
    markers: tuple[tuple[str, str], ...] = (("Python", "Python"),),
) -> NormalizedPost:
    post = NormalizedPost(
        owner_id=owner_id,
        post_id=post_id,
        author_id=owner_id,
        author_name="Тестовое сообщество",
        text=text,
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        post_url=f"https://vk.com/wall{owner_id}_{post_id}",
        comments_count=1,
        likes_count=2,
        reposts_count=3,
        marked_as_ads=False,
        ad_flag=AdFlag.NO,
        post_type="post",
        raw_payload={"id": post_id, "owner_id": owner_id},
    )
    for name, query in markers:
        post.add_marker(MarkerQuery(marker_name=name, query=query))
    return post


async def test_save_posts_creates_records(db_session) -> None:
    posts = [make_post(-9001, 1, "Первый текст"), make_post(-9001, 2, "Второй текст")]
    new_records, known = await save_posts(db_session, posts)

    assert known == 0
    assert len(new_records) == 2
    assert all(record.status == PostStatus.NEW for record in new_records)
    assert new_records[0].post_url == "https://vk.com/wall-9001_1"


async def test_save_posts_deduplicates_by_owner_and_post(db_session) -> None:
    first, _ = await save_posts(db_session, [make_post(-9002, 1, "Текст А")])
    assert len(first) == 1

    again, known = await save_posts(db_session, [make_post(-9002, 1, "Текст А")])
    assert again == []
    assert known == 1

    count = await db_session.scalar(
        select(Post).where(Post.owner_id == -9002, Post.post_id == 1)
    )
    assert count is not None


async def test_markers_are_saved_and_merged(db_session) -> None:
    await save_posts(db_session, [make_post(-9003, 1, "Маркеры", markers=(("Python", "Python"),))])
    # Повторная находка тем же постом по другому маркеру
    post = make_post(-9003, 1, "Маркеры", markers=(("IT", "#IT"),))
    await save_posts(db_session, [post])

    record = await db_session.scalar(
        select(Post).where(Post.owner_id == -9003, Post.post_id == 1)
    )
    markers = (
        await db_session.execute(select(PostMarker).where(PostMarker.post_id == record.id))
    ).scalars().all()
    assert {(m.marker_name, m.query) for m in markers} == {("Python", "Python"), ("IT", "#IT")}


async def test_find_existing_keys(db_session) -> None:
    await save_posts(db_session, [make_post(-9004, 1, "Первый")])
    keys = await find_existing_keys(db_session, [(-9004, 1), (-9004, 2)])
    assert keys == {(-9004, 1)}


async def test_official_ad_flag_saved(db_session) -> None:
    post = make_post(-9005, 1, "Реклама")
    post.marked_as_ads = True
    post.ad_flag = AdFlag.YES
    records, _ = await save_posts(db_session, [post])
    assert records[0].ad_flag is AdFlag.YES
    assert records[0].marked_as_ads is True


async def test_duplicate_text_from_different_authors_is_not_lost(db_session) -> None:
    """Одинаковый текст у разных авторов: обе публикации сохраняются (регрессия).

    Реальная ситуация из продакшн-выборки — один рекламный текст, разосланный
    разными сообществами. Уникальный индекс хеша не должен ломать пакет.
    """
    unique_text = f"Одинаковый рекламный текст {uuid4().hex[:8]}"
    first = make_post(-9006, 1, unique_text)
    second = make_post(-9007, 1, unique_text)
    first.text_hash = second.text_hash = compute_text_hash(unique_text)
    assert first.text_hash == second.text_hash

    records, known = await save_posts(db_session, [first, second])

    assert known == 0
    assert len(records) == 2
    stored = (
        await db_session.execute(
            select(Post).where(Post.owner_id.in_([-9006, -9007]), Post.post_id == 1)
        )
    ).scalars().all()
    assert len(stored) == 2
    assert {record.owner_id for record in stored} == {-9006, -9007}
    # Первый сохранил хеш, второй — нет (иначе нарушилось бы ограничение)
    assert sum(1 for record in stored if record.text_hash is None) == 1


async def test_known_post_marker_added(db_session) -> None:
    """Повторно найденная публикация не дублируется, но маркеры сохраняются."""
    await save_posts(db_session, [make_post(-9008, 1, "Пост с маркером")])
    records, known = await save_posts(
        db_session, [make_post(-9008, 1, "Пост с маркером", markers=(("IT", "#IT"),))]
    )
    assert records == []
    assert known == 1

    record = await db_session.scalar(
        select(Post).where(Post.owner_id == -9008, Post.post_id == 1)
    )
    markers = (
        await db_session.execute(select(PostMarker).where(PostMarker.post_id == record.id))
    ).scalars().all()
    assert {m.query for m in markers} == {"Python", "#IT"}
