"""Формирование уведомления для Telegram (ТЗ п.14.2).

Требования:
* для каждой публикации — отдельное сообщение;
* заголовок «Найдена новая публикация», тема, текст публикации, комментарий;
* inline-кнопка «Перейти к посту»;
* корректное экранирование HTML и обрезка длинных текстов, чтобы Telegram
  не отклонял сообщение из-за формата или лимита длины;
* полный текст публикации остаётся в БД, в сообщении — сокращённый;
* комментарий показывается в блоке для удобного копирования.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

#: Лимит длины сообщения Telegram
TELEGRAM_MAX_LENGTH = 4096
#: Запас под разметку и служебный текст
SAFE_LIMIT = TELEGRAM_MAX_LENGTH - 200
#: Сколько символов публикации показывать максимум
DEFAULT_POST_PREVIEW = 1200

BUTTON_TEXT = "Перейти к посту"
TITLE = "Найдена новая публикация"

_LINK_RE = re.compile(r"https?://\S+")


def escape(value: str | None) -> str:
    """Экранирует текст для parse_mode=HTML."""
    return html.escape(value or "", quote=False)


def shorten(text: str, limit: int) -> str:
    """Обрезает текст по границе слова, добавляя многоточие."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space_index = cut.rfind(" ")
    if space_index > limit * 0.6:
        cut = cut[:space_index]
    return cut.rstrip(" ,.;:—-") + "…"


def clean_for_telegram(text: str) -> str:
    """Убирает невидимые символы и нормализует переводы строк."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def topic_label(topics: list[str] | None, fallback: str = "—") -> str:
    """Формирует строку «Тема: Python / Программирование»."""
    if not topics:
        return fallback
    unique = list(dict.fromkeys(topic.strip() for topic in topics if topic and topic.strip()))
    return " / ".join(unique) if unique else fallback



@dataclass(slots=True)
class NotificationMessage:
    """Готовое к отправке сообщение."""

    text: str
    post_url: str
    plain_text: str
    post_truncated: bool = False

    def keyboard(self) -> dict[str, Any]:
        """Inline-кнопка «Перейти к посту» (ТЗ п.14.2)."""
        return {"inline_keyboard": [[{"text": BUTTON_TEXT, "url": self.post_url}]]}


def build_notification(
    *,
    post_text: str,
    comment: str,
    topics: list[str] | None,
    post_url: str,
    author: str | None = None,
    post_preview_limit: int = DEFAULT_POST_PREVIEW,
) -> NotificationMessage:
    """Собирает текст уведомления по формату ТЗ п.14.2.

    При переполнении сначала сокращается публикация, комментарий (главная
    ценность сообщения) остаётся целиком.
    """
    clean_post = clean_for_telegram(post_text)
    clean_comment = clean_for_telegram(comment)
    truncated = len(clean_post) > post_preview_limit
    preview = shorten(clean_post, post_preview_limit) if truncated else clean_post

    topic = topic_label(topics)

    def render(post_body: str) -> str:
        lines = [f"<b>{escape(TITLE)}</b>", f"Тема: {escape(topic)}"]
        if author:
            lines.append(f"Автор: {escape(author)}")
        lines.extend(
            [
                "",
                "<b>Текст публикации:</b>",
                escape(post_body),
                "",
                "<b>Подготовленный комментарий:</b>",
                f"<pre>{escape(clean_comment)}</pre>",
                "",
                f"Ссылка: {escape(post_url)}",
            ]
        )
        return "\n".join(lines)

    text = render(preview)

    # Если сообщение всё ещё слишком длинное — сокращаем публикацию сильнее
    while len(text) > SAFE_LIMIT and len(preview) > 200:
        preview = shorten(preview, max(200, len(preview) // 2))
        truncated = True
        text = render(preview)

    plain = f"{TITLE}\nТема: {topic}\n\n{preview}\n\n{clean_comment}\n\n{post_url}"
    return NotificationMessage(
        text=text, post_url=post_url, plain_text=plain, post_truncated=truncated
    )
