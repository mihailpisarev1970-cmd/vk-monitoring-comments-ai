"""Тесты клиента AiDostup (ТЗ п.12, п.12.1)."""

from __future__ import annotations

import httpx
import pytest

from app.llm.client import (
    LLMClient,
    LLMConfigurationError,
    LLMError,
    LLMRequestError,
    LLMResponseError,
)

CALL_MESSAGES = [{"role": "user", "content": "привет"}]


def success_body(content: str = '{"ok": true}') -> dict:
    return {
        "id": "msg_1",
        "model": "claude-opus-5-5",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class Recorder:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if self.responses else httpx.Response(200, json=success_body())
        if isinstance(item, Exception):
            raise item
        return item


def make_client(recorder: Recorder, **kwargs) -> LLMClient:
    async def no_sleep(_delay: float) -> None:
        return None

    params = {
        "api_key": "test-key",
        "base_url": "https://aidostup.site/v1",
        "model": "claude-opus-5-5",
        "max_retries": 3,
        "transport": httpx.MockTransport(recorder),
        "sleep": no_sleep,
    }
    params.update(kwargs)
    return LLMClient(**params)


def test_requires_api_key() -> None:
    with pytest.raises(LLMConfigurationError):
        LLMClient(api_key="", model="claude-opus-5-5")


def test_requires_model() -> None:
    with pytest.raises(LLMConfigurationError):
        LLMClient(api_key="k", model="")


async def test_successful_chat() -> None:
    recorder = Recorder([httpx.Response(200, json=success_body('{"x": 1}'))])
    client = make_client(recorder)
    try:
        response = await client.chat(CALL_MESSAGES)
    finally:
        await client.close()
    assert response.content == '{"x": 1}'
    assert response.model == "claude-opus-5-5"
    assert response.usage["total_tokens"] == 15
    request = recorder.requests[0]
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.url.path.endswith("/chat/completions")


async def test_model_and_params_sent() -> None:
    recorder = Recorder([])
    client = make_client(recorder)
    try:
        await client.chat(CALL_MESSAGES, temperature=0.7, max_tokens=100)
    finally:
        await client.close()
    payload = recorder.requests[0].content.decode()
    assert "claude-opus-5-5" in payload
    assert '"temperature":0.7' in payload.replace(" ", "")
    assert '"max_tokens":100' in payload.replace(" ", "")


async def test_rate_limit_retried() -> None:
    recorder = Recorder([httpx.Response(429, text="too many"), httpx.Response(200, json=success_body())])
    client = make_client(recorder)
    try:
        response = await client.chat(CALL_MESSAGES)
    finally:
        await client.close()
    assert response.content
    assert len(recorder.requests) == 2


async def test_503_retried_then_fails() -> None:
    recorder = Recorder([httpx.Response(503, text="overloaded") for _ in range(3)])
    client = make_client(recorder, max_retries=2)
    with pytest.raises(LLMError):
        await client.chat(CALL_MESSAGES)
    await client.close()
    assert len(recorder.requests) == 2


async def test_401_not_retried() -> None:
    recorder = Recorder([httpx.Response(401, text="unauthorized")])
    client = make_client(recorder)
    with pytest.raises(LLMRequestError):
        await client.chat(CALL_MESSAGES)
    await client.close()
    assert len(recorder.requests) == 1


async def test_402_balance_not_retried() -> None:
    recorder = Recorder([httpx.Response(402, text="balance")])
    client = make_client(recorder)
    with pytest.raises(LLMRequestError):
        await client.chat(CALL_MESSAGES)
    await client.close()


async def test_400_not_retried() -> None:
    recorder = Recorder([httpx.Response(400, text="bad request")])
    client = make_client(recorder)
    with pytest.raises(LLMRequestError):
        await client.chat(CALL_MESSAGES)
    await client.close()


async def test_network_error_retried() -> None:
    recorder = Recorder([httpx.ConnectError("boom"), httpx.Response(200, json=success_body())])
    client = make_client(recorder)
    try:
        assert await client.chat(CALL_MESSAGES)
    finally:
        await client.close()
    assert len(recorder.requests) == 2


async def test_empty_content_raises() -> None:
    recorder = Recorder([httpx.Response(200, json=success_body("   "))])
    client = make_client(recorder)
    with pytest.raises(LLMResponseError):
        await client.chat(CALL_MESSAGES)
    await client.close()


async def test_unexpected_structure_raises() -> None:
    recorder = Recorder([httpx.Response(200, json={"weird": 1})])
    client = make_client(recorder)
    with pytest.raises(LLMResponseError):
        await client.chat(CALL_MESSAGES)
    await client.close()


async def test_max_retries_limited() -> None:
    recorder = Recorder([httpx.ConnectError("boom") for _ in range(5)])
    client = make_client(recorder, max_retries=3)
    with pytest.raises(LLMError):
        await client.chat(CALL_MESSAGES)
    await client.close()
    assert len(recorder.requests) == 3
