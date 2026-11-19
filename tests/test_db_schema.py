"""Тесты схемы БД и базовых ограничений (интеграционные, требуют PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.enums import AdFlag, NotificationStatus, PostStatus, RunStatus
from app.db.models import MonitoringRun, Notification, Post, PostMarker

pytestmark = pytest.mark.integration


def _make_post(owner_id: int = -100, post_id: int = 1, **kwargs) -> Post:
    defaults = {
        "owner_id": owner_id,
        "post_id": post_id,
        "author_id": owner_id,
        "text": "Тестовая публикация про Python",
        "published_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "post_url": f"https://vk.com/wall{owner_id}_{post_id}",
        "raw_payload": {"id": post_id},
    }
    defaults.update(kwargs)
    return Post(**defaults)


async def test_enum_types_and_defaults(db_session) -> None:
    rows = await db_session.execute(
        text("select typname from pg_type where typname in "
             "('post_status','ad_flag','notification_status','run_status')")
    )
    assert {r[0] for r in rows} == {"post_status", "ad_flag", "notification_status", "run_status"}

    rows = await db_session.execute(
        text("select unnest(enum_range(null::post_status))::text")
    )
    values = {r[0] for r in rows}
    assert values == {s.value for s in PostStatus}
    assert {s.value for s in RunStatus} <= values | {s.value for s in RunStatus}


async def test_post_defaults_and_unique_owner_post(db_session) -> None:
    post = _make_post(owner_id=-555, post_id=42)
    db_session.add(post)
    await db_session.flush()
    await db_session.refresh(post)

    assert post.status == PostStatus.NEW
    assert post.ad_flag == AdFlag.UNKNOWN
    assert post.attempts == 0
    assert post.created_at is not None

    db_session.add(_make_post(owner_id=-555, post_id=42, text="Другой текст"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_post_markers_saved_for_multiple_markers(db_session) -> None:
    post = _make_post(owner_id=-556, post_id=7)
    db_session.add(post)
    await db_session.flush()

    db_session.add_all(
        [
            PostMarker(post_id=post.id, marker_name="Python", query="Python"),
            PostMarker(post_id=post.id, marker_name="IT", query="#IT"),
        ]
    )
    await db_session.flush()

    markers = (
        await db_session.execute(select(PostMarker).where(PostMarker.post_id == post.id))
    ).scalars().all()
    assert {m.marker_name for m in markers} == {"Python", "IT"}


async def test_text_hash_unique_only_when_not_null(db_session) -> None:
    db_session.add_all([_make_post(-600, 1), _make_post(-600, 2)])
    await db_session.flush()  # NULL text_hash не конфликтует

    db_session.add(_make_post(-601, 1, text_hash="abc"))
    await db_session.flush()

    db_session.add(_make_post(-601, 2, text_hash="abc"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_single_sent_notification_per_post(db_session) -> None:
    post = _make_post(owner_id=-700, post_id=1)
    db_session.add(post)
    await db_session.flush()

    db_session.add(
        Notification(
            post_id=post.id,
            chat_id="1",
            comment_text="ok",
            message_text="msg",
            status=NotificationStatus.SENT,
        )
    )
    await db_session.flush()

    db_session.add(
        Notification(
            post_id=post.id,
            chat_id="2",
            comment_text="ok",
            message_text="msg",
            status=NotificationStatus.SENT,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_monitoring_run_created(db_session) -> None:
    run = MonitoringRun(trigger="test")
    db_session.add(run)
    await db_session.flush()
    await db_session.refresh(run)
    assert run.status == RunStatus.RUNNING
    assert run.queries_total == 0
