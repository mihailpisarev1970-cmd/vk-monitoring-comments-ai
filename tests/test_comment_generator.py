"""Тесты генератора комментариев (ТЗ п.13.1, п.13.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app.db.enums import AdFlag, PostStatus
from app.db.models import Post, PostAnalysis
from app.llm.client import LLMClient
from app.llm.comment_generator import CommentGenerator, CommentOutcome, save_comment_analysis

POST_TEXT = (
    "Подскажите, какую библиотеку Python лучше использовать для создания "
    "Telegram-бота? Смотрю в сторону aiogram и pyTelegramBotAPI. Интересует "
    "поддержка асинхронной отправки медиагрупп."
)

GOOD_COMMENT = (
    "aiogram точнее работает с асинхронностью, медиагруппы там поддерживаются "
    "из коробки, а обработчики можно типизировать. pyTelegramBotAPI проще для "
    "маленьких ботов, но при росте нагрузки начнёт мешать синхронная модель "
    "и общая очередь обновлений."
)

BAD_COMMENT = "Очень интересная тема, спасибо за публикацию! aiogram и медиагруппы."


class StubLLM:
    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        content = self.contents.pop(0) if self.contents else ""
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-5-5",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 900},
            },
        )


def make_client(stub: StubLLM) -> LLMClient:
    async def no_sleep(_delay: float) -> None:
        return None

    return LLMClient(
        api_key="test-key",
        base_url="https://aidostup.site/v1",
        model="claude-opus-5-5",
        max_retries=1,
        transport=httpx.MockTransport(stub),
        sleep=no_sleep,
    )


def make_post(text: str = POST_TEXT) -> Post:
    return Post(
        owner_id=-200,
        post_id=7,
        text=text,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        post_url="https://vk.com/wall-200_7",
        raw_payload={},
        ad_flag=AdFlag.NO,
    )


def test_prompt_loaded() -> None:
    from app.llm.classifier import load_prompt

    text, version = load_prompt(CommentGenerator.prompt_path())
    assert "200 до 300" in text
    assert "<<<POST>>>" in text
    assert len(version) == 12


async def test_generates_valid_comment() -> None:
    stub = StubLLM([GOOD_COMMENT])
    async with make_client(stub) as client:
        outcome = await CommentGenerator(client).generate(make_post())

    assert outcome.valid is True
    assert outcome.attempts == 1
    assert outcome.comment is not None
    assert 200 <= len(outcome.comment) <= 300


async def test_regenerates_on_invalid_comment() -> None:
    stub = StubLLM([BAD_COMMENT, GOOD_COMMENT])
    async with make_client(stub) as client:
        outcome = await CommentGenerator(client).generate(make_post())

    assert outcome.valid is True
    assert outcome.attempts == 2
    assert len(stub.requests) == 2
    # Во второй запрос передана инструкция по исправлению
    body = stub.requests[1].content.decode()
    assert "не прошёл проверку" in body


async def test_fails_after_max_attempts() -> None:
    stub = StubLLM([BAD_COMMENT, BAD_COMMENT])
    async with make_client(stub) as client:
        outcome = await CommentGenerator(client, max_attempts=2).generate(make_post())

    assert outcome.valid is False
    assert outcome.generated is False
    assert outcome.attempts == 2


async def test_empty_post_not_sent() -> None:
    stub = StubLLM([GOOD_COMMENT])
    async with make_client(stub) as client:
        outcome = await CommentGenerator(client).generate(make_post("   "))

    assert outcome.generated is False
    assert stub.requests == []


@pytest.mark.integration
async def test_save_valid_comment(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()

    stub = StubLLM([GOOD_COMMENT])
    async with make_client(stub) as client:
        outcome = await CommentGenerator(client).generate_and_save(db_session, post)

    assert outcome.valid is True
    assert post.status is PostStatus.APPROVED
    analysis = await db_session.scalar(
        select(PostAnalysis).where(
            PostAnalysis.post_id == post.id,
            PostAnalysis.stage == "comment_generation",
        )
    )
    assert analysis is not None
    assert analysis.accepted is True
    assert analysis.reason == outcome.comment


@pytest.mark.integration
async def test_save_invalid_comment_rejects(db_session) -> None:
    post = make_post()
    db_session.add(post)
    await db_session.flush()

    stub = StubLLM([BAD_COMMENT, BAD_COMMENT])
    async with make_client(stub) as client:
        outcome = await CommentGenerator(client, max_attempts=2).generate_and_save(
            db_session, post
        )

    assert outcome.valid is False
    assert post.status is PostStatus.REJECTED_COMMENT
    assert "comment:" in (post.status_reason or "")


@pytest.mark.integration
async def test_llm_error_keeps_status(db_session) -> None:
    post = make_post()
    post.status = PostStatus.APPROVED
    db_session.add(post)
    await db_session.flush()

    outcome = CommentOutcome(
        post_id=post.id,
        generated=False,
        error="ошибка LLM: timeout",
        prompt_version="abc",
    )
    await save_comment_analysis(db_session, post, outcome)

    assert post.status is PostStatus.APPROVED  # статус не испорчен
    assert "не сгенерирован" in (post.status_reason or "")
