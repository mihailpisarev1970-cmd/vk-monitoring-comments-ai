"""Тесты форматтера уведомлений (ТЗ п.14.2)."""

from __future__ import annotations

from app.telegram.formatter import (
    BUTTON_TEXT,
    SAFE_LIMIT,
    TITLE,
    build_notification,
    clean_for_telegram,
    escape,
    shorten,
    topic_label,
)

COMMENT = (
    "aiogram точнее работает с асинхронностью, медиагруппы там поддерживаются "
    "из коробки, а обработчики можно типизировать."
)


def test_escape_html() -> None:
    assert escape("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"


def test_escape_none() -> None:
    assert escape(None) == ""


def test_shorten_by_word_boundary() -> None:
    text = "Слово " * 100
    result = shorten(text, 50)
    assert len(result) <= 51
    assert result.endswith("…")


def test_shorten_keeps_short_text() -> None:
    assert shorten("Короткий текст", 100) == "Короткий текст"


def test_clean_removes_zero_width_and_extra_lines() -> None:
    assert clean_for_telegram("a\u200b\n\n\n\nb") == "a\n\nb"


def test_topic_label() -> None:
    assert topic_label(["Python", "Программирование"]) == "Python / Программирование"
    assert topic_label(["Python", "Python"]) == "Python"
    assert topic_label([]) == "—"
    assert topic_label(None, "IT") == "IT"


def test_notification_structure() -> None:
    message = build_notification(
        post_text="Текст публикации",
        comment=COMMENT,
        topics=["Python", "Программирование"],
        post_url="https://vk.com/wall-1_2",
    )
    assert TITLE in message.text
    assert "Тема: Python / Программирование" in message.text
    assert "<b>Текст публикации:</b>" in message.text
    assert "<b>Подготовленный комментарий:</b>" in message.text
    assert f"<pre>{COMMENT}</pre>" in message.text
    assert "https://vk.com/wall-1_2" in message.text


def test_keyboard_has_button() -> None:
    message = build_notification(
        post_text="Текст",
        comment=COMMENT,
        topics=["IT"],
        post_url="https://vk.com/wall-1_2",
    )
    keyboard = message.keyboard()
    button = keyboard["inline_keyboard"][0][0]
    assert button["text"] == BUTTON_TEXT
    assert button["url"] == "https://vk.com/wall-1_2"


def test_post_html_is_escaped() -> None:
    message = build_notification(
        post_text="<script>alert(1)</script> & <b>тег</b>",
        comment=COMMENT,
        topics=["IT"],
        post_url="https://vk.com/wall-1_2",
    )
    assert "<script>" not in message.text
    assert "&lt;script&gt;" in message.text


def test_long_post_is_truncated() -> None:
    long_text = "Очень длинный текст публикации. " * 200
    message = build_notification(
        post_text=long_text,
        comment=COMMENT,
        topics=["IT"],
        post_url="https://vk.com/wall-1_2",
    )
    assert message.post_truncated is True
    assert len(message.text) <= SAFE_LIMIT
    # Комментарий не пострадал при сокращении публикации
    assert COMMENT in message.text


def test_comment_never_truncated() -> None:
    long_text = "Слово " * 3000
    message = build_notification(
        post_text=long_text,
        comment=COMMENT,
        topics=["IT"],
        post_url="https://vk.com/wall-1_2",
    )
    assert COMMENT in message.text
    assert len(message.text) <= SAFE_LIMIT


def test_author_included() -> None:
    message = build_notification(
        post_text="Текст",
        comment=COMMENT,
        topics=["IT"],
        post_url="https://vk.com/wall-1_2",
        author="Python Daily",
    )
    assert "Автор: Python Daily" in message.text


def test_plain_text_contains_all_parts() -> None:
    message = build_notification(
        post_text="Текст публикации",
        comment=COMMENT,
        topics=["Python"],
        post_url="https://vk.com/wall-1_2",
    )
    assert TITLE in message.plain_text
    assert COMMENT in message.plain_text
    assert "https://vk.com/wall-1_2" in message.plain_text
    assert "<b>" not in message.plain_text
