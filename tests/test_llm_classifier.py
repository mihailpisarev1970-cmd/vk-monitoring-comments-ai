"""Тесты классификатора: разбор, повторы, запись в post_analysis (ТЗ п.12)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app.db.enums import AdFlag, PostStatus
from app.db.models import Post, PostAnalysis
from app.llm.classifier import (
    ClassificationOutcome,
    PostClassifier,
    PromptLoadError,
    load_prompt,
    save_analysis,
)
from app.llm.client import LLMClient

VALID = {
    "relevant": True,
    "is_advertising": False,
    "is_course_offer": False,
    "is_commercial_promotion": False,
    "is_spam": False,
    "is_low_quality": False,
    "is_clickbait": False,
    "is_engagement_bait": False,
    "is_suspicious": False,
    "has_information_value": True,
    "confidence": 0.93,
    "matched_topics": ["Python"],
    "spam_categories": [],
    "reason": "Содержательный вопрос",
}
AD = VALID | {
    "is_advertising": True,
    "is_commercial_promotion": True,
    "has_information_value": False,
    "reason": "Коммерческое предложение",
}


class StubLLM:
    """Подменяет HTTP-ответы AiDostup по очереди."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        content = self.contents.pop(0) if self.contents else "{}"
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-5-5",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 1234},
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


def make_post(text: str = "Подскажите, какую библиотеку использовать для бота?") -> Post:
    """Создаёт публикацию; ``id`` присваивается БД при flush."""
    return Post(
        owner_id=-100,
        post_id=1,
        text=text,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        post_url="https://vk.com/wall-100_1",
        raw_payload={},
        ad_flag=AdFlag.NO,
    )


def test_prompt_loads() -> None:
    text, version = load_prompt()
    assert len(text) > 3000
    assert "<<<POST>>>" in text
    assert len(version) == 12


def test_prompt_contains_decision_rules() -> None:
    text, _ = load_prompt()
    for marker in ("is_advertising", "is_course_offer", "is_spam", "confidence", "JSON"):
        assert marker in text


def test_missing_prompt_raises(tmp_path) -> None:
    with pytest.raises(PromptLoadError):
        load_prompt(tmp_path / "missing.txt")


async def test_classify_accepts_valid() -> None:
    stub = StubLLM([json.dumps(VALID, ensure_ascii=False)])
    async with make_client(stub) as client:
        classifier = PostClassifier(client)
        outcome = await classifier.classify(make_post())

    assert outcome.analyzed is True
    assert outcome.accepted is True
    assert outcome.attempts == 1
    assert outcome.model == "claude-opus-5-5"
    # Текст публикации передан как недоверенные данные
    body = stub.requests[0].content.decode()
    assert "<<<POST>>>" in body


async def test_classify_rejects_ad() -> None:
    stub = StubLLM([json.dumps(AD, ensure_ascii=False)])
    async with make_client(stub) as client:
        outcome = await PostClassifier(client).classify(make_post())

    assert outcome.analyzed is True
    assert outcome.accepted is False
    assert "is_advertising" in outcome.decision.rejection_reasons


async def test_retry_on_invalid_json() -> None:
    stub = StubLLM(["не JSON вовсе", json.dumps(VALID, ensure_ascii=False)])
    async with make_client(stub) as client:
        outcome = await PostClassifier(client).classify(make_post())

    assert outcome.analyzed is True
    assert outcome.attempts == 2
    assert len(stub.requests) == 2


async def test_invalid_json_never_accepted() -> None:
    stub = StubLLM(["не JSON", "снова не JSON", "{broken"])
    async with make_client(stub) as client:
        outcome = await PostClassifier(client, max_parse_retries=2).classify(make_post())

    assert outcome.analyzed is False
    assert outcome.accepted is False
    assert outcome.error is not None


async def test_empty_post_not_sent() -> None:
    stub = StubLLM([json.dumps(VALID)])
    async with make_client(stub) as client:
        outcome = await PostClassifier(client).classify(make_post("   "))

    assert outcome.analyzed is False
    assert stub.requests == []



async def test_llm_error_not_accepted() -> None:
    class Failing(StubLLM):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            raise httpx.ConnectError("сеть недоступна")

    async with make_client(Failing([])) as client:
        post = make_post()
        post.id = 0
        outcome = await PostClassifier(client).classify(post)

    assert outcome.analyzed is False
    assert outcome.accepted is False
    assert "ошибка LLM" in (outcome.error or "")


# ------------------------------------------------------- интеграция с БД
@pytest.mark.integration
async def test_save_analysis_approved(db_session) -> None:
    post = make_post("Подробный технический вопрос про asyncio и middleware в aiogram")
    db_session.add(post)
    await db_session.flush()

    stub = StubLLM([json.dumps(VALID, ensure_ascii=False)])
    async with make_client(stub) as client:
        outcome = await PostClassifier(client).classify_and_save(db_session, post)

    assert outcome.accepted is True
    assert post.status is PostStatus.APPROVED
    analysis = await db_session.scalar(
        select(PostAnalysis).where(PostAnalysis.post_id == post.id)
    )
    assert analysis is not None
    assert analysis.accepted is True
    assert analysis.stage == "classify"
    assert analysis.confidence == pytest.approx(0.93)
    assert analysis.matched_topics == ["Python"]


@pytest.mark.integration
async def test_save_analysis_rejected_llm(db_session) -> None:
    post = make_post("Разрабатываем чат-ботов для бизнеса под ключ. Пишите в личку.")
    db_session.add(post)
    await db_session.flush()

    stub = StubLLM([json.dumps(AD, ensure_ascii=False)])
    async with make_client(stub) as client:
        outcome = await PostClassifier(client).classify_and_save(db_session, post)

    assert outcome.accepted is False
    assert post.status is PostStatus.REJECTED_LLM
    assert "llm:" in (post.status_reason or "")
    analysis = await db_session.scalar(
        select(PostAnalysis).where(PostAnalysis.post_id == post.id)
    )
    assert analysis.accepted is False
    assert analysis.rejection_reasons is not None
    assert "is_advertising" in analysis.rejection_reasons


@pytest.mark.integration
async def test_llm_error_keeps_post_unprocessed(db_session) -> None:
    post = make_post("Технический вопрос про Python и asyncio, требующий проверки")
    db_session.add(post)
    await db_session.flush()

    outcome = ClassificationOutcome(
        post_id=post.id,
        analyzed=False,
        error="ошибка LLM: таймаут",
        prompt_version="abc",
    )
    await save_analysis(db_session, post, outcome)

    assert post.status is PostStatus.NEW  # не проходит дальше
    assert "ошибка анализа" in (post.status_reason or "")
    analysis = await db_session.scalar(
        select(PostAnalysis).where(PostAnalysis.post_id == post.id)
    )
    assert analysis.accepted is False
    assert analysis.rejection_reasons == ["llm_error"]
