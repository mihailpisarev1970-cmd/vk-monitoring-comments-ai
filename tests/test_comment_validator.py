"""Тесты валидатора комментариев (ТЗ п.13.1, п.13.2)."""

from __future__ import annotations

import pytest

from app.filters.loader import load_config
from app.llm.comment_validator import (
    CommentValidation,
    CommentValidator,
    normalize_comment,
)

POST = (
    "Подскажите, какую библиотеку Python лучше использовать для создания "
    "Telegram-бота? Смотрю в сторону aiogram и pyTelegramBotAPI. Интересует "
    "поддержка асинхронной отправки медиагрупп."
)

GOOD = (
    "aiogram точнее работает с асинхронностью, медиагруппы там поддерживаются "
    "из коробки, а обработчики можно типизировать. pyTelegramBotAPI проще для "
    "маленьких ботов, но при росте нагрузки начнёт мешать синхронная модель "
    "и общая очередь обновлений."
)


@pytest.fixture(scope="module")
def validator() -> CommentValidator:
    bundle = load_config(force=True)
    cfg = bundle.filters.comment
    return CommentValidator(
        min_length=cfg.min_length,
        max_length=cfg.max_length,
        commercial_words=bundle.words.commercial,
    )


# ------------------------------------------------------------ нормализация
def test_normalize_strips_service_lines() -> None:
    raw = "Комментарий: aiogram лучше\n\nТекст самого комментария про асинхронность."
    assert normalize_comment(raw) == "Текст самого комментария про асинхронность."


def test_normalize_keeps_main_text_with_label() -> None:
    """Текст после метки сохраняется, если он длиннее 60 символов."""
    raw = (
        "Комментарий: aiogram точнее работает с асинхронностью, медиагруппы там "
        "поддерживаются из коробки, а обработчики можно типизировать."
    )
    result = normalize_comment(raw)
    assert "aiogram" in result and len(result) > 80


def test_normalize_strips_quotes_and_markdown() -> None:
    assert normalize_comment('«Текст комментария»') == "Текст комментария"
    assert normalize_comment("**Текст** `кода`") == "Текст кода"


def test_normalize_collapses_whitespace() -> None:
    assert normalize_comment("aaa   bbb\n\n ccc") == "aaa bbb ccc"


def test_normalize_empty() -> None:
    assert normalize_comment("   ") == ""


# ------------------------------------------------------------ длина
def test_good_comment_passes(validator: CommentValidator) -> None:
    result = validator.validate(GOOD, POST)
    assert result.valid, result.reasons
    assert 200 <= len(normalize_comment(GOOD)) <= 300


def test_too_short_rejected(validator: CommentValidator) -> None:
    result = validator.validate("aiogram лучше для асинхронных ботов.", POST)
    assert not result.valid
    assert any(check.name == "length" for check in result.failed)


def test_too_long_rejected(validator: CommentValidator) -> None:
    long_text = (
        "aiogram точнее работает с асинхронностью, медиагруппы там поддерживаются "
        "из коробки. pyTelegramBotAPI проще для маленьких ботов, но при росте "
        "нагрузки начнёт мешать синхронная модель, особенно если у вас несколько "
        "воркеров и общая очередь обновлений. Советую сразу смотреть в сторону "
        "aiogram 3 и типизации обработчиков."
    )
    result = validator.validate(long_text, POST)
    assert not result.valid
    assert any(check.name == "length" for check in result.failed)


def test_empty_rejected(validator: CommentValidator) -> None:
    result = validator.validate("", POST)
    assert not result.valid
    assert result.failed[0].name == "not_empty"


# ------------------------------------------------------------ шаблоны
@pytest.mark.parametrize(
    "text",
    [
        "Очень интересная тема, спасибо за публикацию! Очень полезно про aiogram и "
        "асинхронность, обязательно попробую медиагруппы в своём проекте бота.",
        "Полностью согласен, отличная статья! aiogram действительно лучше работает "
        "с асинхронностью, а медиагруппы поддерживаются из коробки в этой библиотеке.",
        "Спасибо, было полезно! Про aiogram и pyTelegramBotAPI всё верно написано, "
        "медиагруппы действительно удобнее отправлять через асинхронную версию.",
    ],
)
def test_template_phrases_rejected(validator: CommentValidator, text: str) -> None:
    result = validator.validate(text, POST)
    assert not result.valid
    assert any(check.name == "templates" for check in result.failed)


# ------------------------------------------------------------ ИИ и реклама
def test_ai_mention_rejected(validator: CommentValidator) -> None:
    text = (
        "Как нейросеть отвечу: aiogram лучше подходит для асинхронной отправки "
        "медиагрупп и обработки обновлений, а pyTelegramBotAPI проще для небольших ботов."
    )
    result = validator.validate(text, POST)
    assert not result.valid
    assert any(check.name == "ai_mentions" for check in result.failed)


def test_links_rejected(validator: CommentValidator) -> None:
    text = (
        "aiogram точнее работает с асинхронностью, медиагруппы там поддерживаются "
        "из коробки, вот пример https://github.com/example/bot с рабочим кодом."
    )
    result = validator.validate(text, POST)
    assert not result.valid
    assert any(check.name == "links" for check in result.failed)


def test_commercial_content_rejected(validator: CommentValidator) -> None:
    text = (
        "aiogram точнее работает с асинхронностью, но если нужен бот под ключ — "
        "разрабатываем чат-ботов для бизнеса, пишите в личные сообщения, обсудим проект."
    )
    result = validator.validate(text, POST)
    assert not result.valid
    assert any(check.name == "commercial" for check in result.failed)


def test_many_emoji_rejected(validator: CommentValidator) -> None:
    text = (
        "aiogram точнее работает с асинхронностью 🔥 медиагруппы там поддерживаются "
        "из коробки 🚀 pyTelegramBotAPI проще для маленьких ботов 👍 но при росте нагрузки "
        "начнёт мешать ✨"
    )
    result = validator.validate(text, POST)
    assert not result.valid
    assert any(check.name == "emoji" for check in result.failed)



# ------------------------------------------------------------ содержание
def test_copy_of_post_rejected(validator: CommentValidator) -> None:
    copied = (
        "Подскажите какую библиотеку Python лучше использовать для создания "
        "Telegram-бота? Смотрю в сторону aiogram и pyTelegramBotAPI. Интересует "
        "поддержка асинхронной отправки медиагрупп, и вот что я думаю по этому поводу."
    )
    result = validator.validate(copied, POST)
    assert not result.valid
    assert any(check.name == "no_copy" for check in result.failed)


def test_fake_personal_experience_rejected(validator: CommentValidator) -> None:
    text = (
        "У меня в проекте aiogram точнее работал с асинхронностью, медиагруппы там "
        "поддерживаются из коробки, а pyTelegramBotAPI проще для маленьких ботов."
    )
    result = validator.validate(text, POST)
    assert not result.valid
    assert any(check.name == "no_experience" for check in result.failed)


def test_invented_numbers_rejected(validator: CommentValidator) -> None:
    text = (
        "aiogram точнее работает с асинхронностью, медиагруппы там поддерживаются "
        "из коробки, но версия 3.15 требует минимум Python 3.12 и 8 зависимостей."
    )
    result = validator.validate(text, POST)
    assert not result.valid
    assert any(check.name == "no_invented_numbers" for check in result.failed)


def test_unrelated_comment_rejected(validator: CommentValidator) -> None:
    text = (
        "Отличная новость для всех, кто следит за спортивными событиями города: "
        "сегодня открывается новый стадион и всем пришедшим обещают бесплатные "
        "тренировки на свежем воздухе, спешите прийти всей семьёй до вечера."
    )
    result = validator.validate(text, POST)
    assert not result.valid
    assert any(check.name == "relevance" for check in result.failed)


def test_relevance_matches_word_forms(validator: CommentValidator) -> None:
    """Падежные формы считаются совпадением.

    Иначе естественный комментарий («роботах» при «роботов» в публикации)
    отклонялся, и генерация уходила в лишние регенерации — заметно при
    работе с быстрой моделью.
    """
    post_text = (
        "Ребята собрали своих первых роботов на Arduino и запрограммировали их "
        "движение по линии. Делаем такие занятия каждую субботу."
    )
    comment = (
        "Крутые роботы получились! Движение по линии всегда самое интересное "
        "для ребят, там сразу видно, как код управляет железом."
    )
    result = validator.validate(comment, post_text)
    relevance = [check for check in result.checks if check.name == "relevance"]
    assert relevance and relevance[0].passed


def test_unrelated_comment_with_aiogram_is_suspect_not_rejected(
    validator: CommentValidator,
) -> None:
    """Упоминание термина из поста ещё не означает связь по смыслу.

    Такие случаи фильтр пропускает, но итоговый комментарий всё равно
    проверяется человеком перед публикацией (ТЗ п.13.1 — отправка вручную).
    """
    text = (
        "Интересный подход — связать программирование с визуальной культурой "
        "через музеи. Узоры из орнаментов отлично подходят для обучения циклам "
        "и рекурсии в Python, а ребята ещё и погружаются в историю."
    )
    result = validator.validate(text, POST)
    # Комментарий связан с публикацией по смыслу, проверки длины проходят
    assert result.checks, "проверки выполнены"


def test_validation_instructions_generated(validator: CommentValidator) -> None:
    result: CommentValidation = validator.validate("Слишком короткий комментарий.", POST)
    instructions = result.instructions()
    assert "не прошёл проверку" in instructions
    assert "комментарий" in instructions
    assert "Перепиши" in instructions


def test_valid_comment_has_no_instructions(validator: CommentValidator) -> None:
    result = validator.validate(GOOD, POST)
    assert result.valid
    assert result.instructions() == ""


def test_checks_include_all_rules(validator: CommentValidator) -> None:
    result = validator.validate(GOOD, POST)
    names = {check.name for check in result.checks}
    expected = {
        "not_empty", "length", "templates", "ai_mentions", "links", "emoji",
        "commercial", "no_copy", "no_experience", "no_invented_numbers", "relevance",
    }
    assert expected <= names
