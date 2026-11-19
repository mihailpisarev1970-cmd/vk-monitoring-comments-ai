"""Telegram-бот на aiogram 3 (ТЗ п.14).

Бот **не публикует комментарии во ВКонтакте** (ТЗ п.14). Его задача —
доставлять уведомления в чат пользователя. Модуль также поддерживает команды
диагностики:

* ``/start`` — краткая справка;
* ``/id`` — узнать chat_id текущего чата (удобно для настройки .env);
* ``/status`` — состояние очереди уведомлений.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import NotificationStatus
from app.db.models import Notification
from app.db.session import session_scope

logger = get_logger(__name__)

router = Router(name="vk_monitor")

HELP_TEXT = (
    "VK Monitor Assistant\n\n"
    "Бот присылает найденные публикации ВКонтакте и подготовленный комментарий.\n"
    "Комментарий публикуется вручную: скопируйте текст и вставьте в пост.\n\n"
    "Команды:\n"
    "/id — показать chat_id\n"
    "/status — состояние очереди уведомлений"
)


def create_bot(token: str | None = None) -> Bot:
    """Создаёт экземпляр бота с HTML по умолчанию."""
    bot_token = token or settings.telegram_bot_token.get_secret_value()
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в конфигурации")
    return Bot(
        token=bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    """Диспетчер с обработчиками команд."""
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("id"))
async def handle_id(message: Message) -> None:
    chat_id = message.chat.id
    configured = settings.telegram_chat_id
    hint = "совпадает с TELEGRAM_CHAT_ID" if str(chat_id) == configured else "не совпадает с .env"
    await message.answer(f"chat_id: <code>{chat_id}</code>\n({hint})")


@router.message(Command("status"))
async def handle_status(message: Message) -> None:
    try:
        stats = await queue_stats()
    except Exception as exc:  # noqa: BLE001 - диагностика не должна падать
        await message.answer(f"Не удалось получить состояние: {exc}")
        return
    lines = ["<b>Очередь уведомлений</b>"]
    for status, count in stats.items():
        lines.append(f"{status}: {count}")
    await message.answer("\n".join(lines))


async def queue_stats() -> dict[str, Any]:
    """Сводка по статусам очереди."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Notification.status, func.count(Notification.id)).group_by(
                    Notification.status
                )
            )
        ).all()
    result = {status.value: 0 for status in NotificationStatus}
    for status, count in rows:
        result[status.value] = int(count)
    return result
