"""Тесты очереди доставки уведомлений (ТЗ п.14.3)."""

from __future__ import annotations

import json as jsonlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.enums import AdFlag, NotificationStatus, PostStatus
from app.db.models import Notification, Post, PostAnalysis
from app.telegram.queue import NotificationWorker, compute_backoff, enqueue_post
from app.telegram.sender import TelegramSender

pytestmark = pytest.mark.integration

COMMENT = (
    "aiogram точнее работает с асинхронностью, медиагруппы там поддерживаются "
    "из коробки, а обработчики можно типизировать."
)


def ok(message_id: int = 321) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "result": {"message_id": message_id, "chat": {"id": 111, "type": "private"}},
        },
    )


def err(code: int, description: str = "error", retry_after: int | None = None) -> httpx.Response:
    body: dict = {"ok": False, "error_code": code, "description": description}
    if retry_after:
        body["parameters"] = {"retry_after": retry_after}
    return httpx.Response(200, json=body)


class Stub:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if self.responses else ok()
        if isinstance(item, Exception):
            raise item
        return item


def sender_with(stub: Stub) -> TelegramSender:
    return TelegramSender(token="1:a", transport=httpx.MockTransport(stub))


def make_post() -> Post:
    return Post(
        owner_id=-300,
        post_id=11,
        text="Подробный текст публикации про Python, aiogram и асинхронность",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        post_url="https://vk.com/wall-300_11",
        raw_payload={},
        ad_flag=AdFlag.NO,
        status=PostStatus.APPROVED,
    )


async def add_analysis(session, post: Post, comment: str = COMMENT) -> None:
    session.add(
        PostAnalysis(
            post_id=post.id,
            stage="comment_generation",
            accepted=True,
            reason=comment,
            matched_topics=["Python", "Программирование"],
        )
    )
    await session.flush()


def test_backoff_grows() -> None:
    assert compute_backoff(1).total_seconds() == 60
    assert compute_backoff(2).total_seconds() == 120
    assert compute_backoff(3).total_seconds() == 240
    assert compute_backoff(20).total_seconds() == 3600


async def test_enqueue_creates_notification(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()
    await add_analysis(db_session, post)

    notification = await enqueue_post(db_session, post, chat_id="111")

    assert notification is not None
    assert notification.status is NotificationStatus.PENDING
    assert notification.attempts == 0
    assert COMMENT in notification.message_text
    assert post.status is PostStatus.QUEUED


async def test_enqueue_is_idempotent(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()
    await add_analysis(db_session, post)

    first = await enqueue_post(db_session, post, chat_id="111")
    duplicate = await enqueue_post(db_session, post, chat_id="111")

    assert first is not None
    assert duplicate is None
    count = len(
        (
            await db_session.execute(
                select(Notification.id).where(Notification.post_id == post.id)
            )
        ).all()
    )
    assert count == 1


async def test_worker_sends_and_stores_message_id(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()
    await add_analysis(db_session, post)
    notification = await enqueue_post(db_session, post, chat_id="111")

    stub = Stub([ok(777)])
    sender = sender_with(stub)
    try:
        stats = await NotificationWorker(sender).process_due(db_session)
    finally:
        await sender.close()

    assert stats.sent == 1
    assert notification.status is NotificationStatus.SENT
    assert notification.telegram_message_id == 777
    assert notification.sent_at is not None
    assert post.status is PostStatus.SENT


async def test_transient_error_schedules_retry(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()
    await add_analysis(db_session, post)
    notification = await enqueue_post(db_session, post, chat_id="111")

    stub = Stub([err(429, "Too Many Requests", retry_after=30)])
    sender = sender_with(stub)
    try:
        stats = await NotificationWorker(sender).process_due(db_session)
    finally:
        await sender.close()

    assert stats.sent == 0
    assert notification.status is NotificationStatus.PENDING
    assert notification.attempts == 1
    assert notification.last_error is not None
    assert notification.next_attempt_at > datetime.now(UTC)
    assert post.status is PostStatus.QUEUED


async def test_permanent_error_fails_immediately(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()
    await add_analysis(db_session, post)
    notification = await enqueue_post(db_session, post, chat_id="111")

    stub = Stub([err(403, "bot was blocked by the user")])
    sender = sender_with(stub)
    try:
        stats = await NotificationWorker(sender).process_due(db_session)
    finally:
        await sender.close()

    assert stats.failed == 1
    assert notification.status is NotificationStatus.FAILED
    assert post.status is PostStatus.FAILED


async def test_retries_limited(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()
    await add_analysis(db_session, post)
    notification = await enqueue_post(db_session, post, chat_id="111", max_attempts=2)

    stub = Stub([err(500), err(500)])
    sender = sender_with(stub)
    worker = NotificationWorker(sender)
    try:
        await worker.process_due(db_session)
        notification.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.flush()
        await worker.process_due(db_session)
    finally:
        await sender.close()

    assert notification.attempts == 2
    assert notification.status is NotificationStatus.FAILED


async def test_retry_uses_stored_comment(db_session) -> None:
    """При повторной отправке используется сохранённый комментарий (ТЗ п.14.3)."""
    post = make_post()
    db_session.add(post)
    await db_session.flush()
    await add_analysis(db_session, post)
    notification = await enqueue_post(db_session, post, chat_id="111")
    stored_text = notification.message_text

    stub = Stub([err(503), ok(900)])
    sender = sender_with(stub)
    worker = NotificationWorker(sender)
    try:
        await worker.process_due(db_session)
        notification.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.flush()
        await worker.process_due(db_session)
    finally:
        await sender.close()

    assert notification.status is NotificationStatus.SENT
    sent_text = jsonlib.loads(stub.requests[-1].content)["text"]
    assert sent_text == stored_text
    assert notification.telegram_message_id == 900


async def test_no_duplicate_after_sent(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()
    await add_analysis(db_session, post)
    await enqueue_post(db_session, post, chat_id="111")

    stub = Stub([ok()])
    sender = sender_with(stub)
    try:
        await NotificationWorker(sender).process_due(db_session)
    finally:
        await sender.close()

    assert await enqueue_post(db_session, post, chat_id="111") is None
    count = len(
        (
            await db_session.execute(
                select(Notification.id).where(Notification.post_id == post.id)
            )
        ).all()
    )
    assert count == 1


async def test_enqueue_requires_comment(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()

    assert await enqueue_post(db_session, post, chat_id="111") is None
