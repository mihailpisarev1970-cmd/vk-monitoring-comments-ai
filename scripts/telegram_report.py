"""Реальная доставка уведомлений в Telegram (ТЗ п.14).

Запуск:
    python -m scripts.telegram_report --preview    # отправит 1 тестовое сообщение
    python -m scripts.telegram_report              # отправит очередь approved-публикаций

Флаг ``--dry-run`` формирует сообщения, но не отправляет их.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.enums import PostStatus
from app.db.models import Notification, Post
from app.db.session import engine, session_scope
from app.telegram.formatter import build_notification
from app.telegram.queue import NotificationWorker, enqueue_post
from app.telegram.sender import TelegramSender


async def send_preview(sender: TelegramSender) -> int:
    """Отправляет одно демонстрационное сообщение в настроенный чат."""
    chat_id = settings.telegram_chat_id
    if not chat_id:
        print("TELEGRAM_CHAT_ID не задан")
        return 1

    message = build_notification(
        post_text=(
            "Подскажите, какую библиотеку Python лучше использовать для создания "
            "Telegram-бота? Смотрю в сторону aiogram и pyTelegramBotAPI."
        ),
        comment=(
            "aiogram точнее работает с асинхронностью, медиагруппы там поддерживаются "
            "из коробки, а обработчики можно типизировать. pyTelegramBotAPI проще для "
            "маленьких ботов, но при росте нагрузки начнёт мешать синхронная модель."
        ),
        topics=["Python", "Программирование"],
        post_url="https://vk.com/wall-238001933_6304",
        author="Python Daily",
    )
    result = await sender.send(chat_id, message)
    print(f"Тестовое сообщение отправлено: message_id={result.message_id}")
    return 0


async def deliver_queue(sender: TelegramSender, limit: int, dry_run: bool) -> int:
    """Ставит approved-публикации в очередь и отправляет их."""
    async with session_scope() as session:
        posts = (
            await session.execute(
                select(Post)
                .where(Post.status == PostStatus.APPROVED)
                .order_by(Post.id)
                .limit(limit)
            )
        ).scalars().all()
        already = set(
            (
                await session.execute(
                    select(Notification.post_id).where(Notification.chat_id == settings.telegram_chat_id)
                )
            ).scalars().all()
        )

        queued = 0
        for post in posts:
            if post.id in already:
                continue
            notification = await enqueue_post(
                session, post, chat_id=settings.telegram_chat_id
            )
            if notification is not None:
                queued += 1
                print(f"  в очередь: {post.key}")
        print(f"\nПоставлено в очередь: {queued} из {len(posts)} approved")

        if dry_run:
            rows = (
                await session.execute(
                    select(Notification).where(Notification.status == "pending")
                )
            ).scalars().all()
            for notification in rows:
                print("\n--- сообщение ---")
                print(notification.message_text[:800])
            return 0

        worker = NotificationWorker(sender)
        stats = await worker.process_due(session)
        print(f"\n=== Итог доставки ===\n  {stats.to_dict()}")
    return 0 if not stats.errors else 0


async def main(args: argparse.Namespace) -> int:
    setup_logging(log_to_file=False)
    async with TelegramSender() as sender:
        health = await sender.health()
        bot = (health["bot"].get("result") or {})
        print(
            f"Бот: @{bot.get('username')}, "
            f"чат: {settings.telegram_chat_id or '—'}, "
            f"доступен: {health['chat_ok']}"
        )
        if args.preview:
            code = await send_preview(sender)
        else:
            code = await deliver_queue(sender, args.limit, args.dry_run)
    await engine.dispose()
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Проверка доставки в Telegram")
    parser.add_argument("--preview", action="store_true", help="отправить тестовое сообщение")
    parser.add_argument("--dry-run", action="store_true", help="не отправлять сообщения")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))
