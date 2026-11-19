"""Тесты HTTP-клиента VK: троттлинг, backoff, обработка ошибок (ТЗ п.6.1)."""

from __future__ import annotations

import httpx
import pytest

from app.vk.client import VKClient
from app.vk.exceptions import (
    VKAccessError,
    VKAuthError,
    VKConfigurationError,
    VKInvalidRequestError,
    VKNetworkError,
    VKRateLimitError,
    VKResponseFormatError,
    build_error,
)


class Recorder:
    """MockTransport-обработчик: отдаёт заданные ответы и запоминает запросы."""

    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = (
            self.responses.pop(0)
            if self.responses
            else httpx.Response(200, json={"response": {}})
        )
        if isinstance(item, Exception):
            raise item
        return item


def make_client(recorder: Recorder, **kwargs) -> VKClient:
    async def no_sleep(_delay: float) -> None:
        return None

    params = {
        "access_token": "test-token",
        "api_version": "5.199",
        "min_interval": 0,
        "max_retries": 3,
        "transport": httpx.MockTransport(recorder),
        "sleep": no_sleep,
    }
    params.update(kwargs)
    return VKClient(**params)


def test_requires_token() -> None:
    with pytest.raises(VKConfigurationError):
        VKClient(access_token="")


async def test_successful_call_returns_response() -> None:
    recorder = Recorder([httpx.Response(200, json={"response": {"items": [1, 2]}})])
    client = make_client(recorder)
    try:
        result = await client.call("newsfeed.search", {"q": "Python"})
    finally:
        await client.close()
    assert result == {"items": [1, 2]}
    request = recorder.requests[0]
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.url.path.endswith("/newsfeed.search")


async def test_api_version_sent_from_config() -> None:
    recorder = Recorder([httpx.Response(200, json={"response": {}})])
    client = make_client(recorder, api_version="5.131")
    try:
        await client.call("users.get", {"user_ids": 1})
    finally:
        await client.close()
    assert "v=5.131" in recorder.requests[0].content.decode()


async def test_rate_limit_is_retried() -> None:
    recorder = Recorder(
        [
            httpx.Response(
                200, json={"error": {"error_code": 6, "error_msg": "Too many requests"}}
            ),
            httpx.Response(200, json={"response": {"ok": True}}),
        ]
    )
    client = make_client(recorder)
    try:
        assert await client.call("newsfeed.search", {"q": "x"}) == {"ok": True}
    finally:
        await client.close()
    assert len(recorder.requests) == 2


async def test_retries_are_limited() -> None:
    error = {"error": {"error_code": 6, "error_msg": "Too many requests"}}
    recorder = Recorder([httpx.Response(200, json=error) for _ in range(5)])
    client = make_client(recorder, max_retries=3)
    with pytest.raises(VKRateLimitError):
        await client.call("newsfeed.search", {"q": "x"})
    await client.close()
    assert len(recorder.requests) == 3


async def test_auth_error_is_not_retried() -> None:
    recorder = Recorder(
        [
            httpx.Response(
                200,
                json={"error": {"error_code": 5, "error_msg": "User authorization failed"}},
            )
        ]
    )
    client = make_client(recorder)
    with pytest.raises(VKAuthError):
        await client.call("newsfeed.search", {"q": "x"})
    await client.close()
    assert len(recorder.requests) == 1


async def test_access_error_is_not_retried() -> None:
    recorder = Recorder(
        [httpx.Response(200, json={"error": {"error_code": 15, "error_msg": "Access denied"}})]
    )
    client = make_client(recorder)
    with pytest.raises(VKAccessError):
        await client.call("newsfeed.search", {"q": "x"})
    await client.close()
    assert len(recorder.requests) == 1


async def test_invalid_params_not_retried() -> None:
    recorder = Recorder(
        [
            httpx.Response(
                200,
                json={
                    "error": {
                        "error_code": 100,
                        "error_msg": "One of the parameters specified was missing or invalid",
                    }
                },
            )
        ]
    )
    client = make_client(recorder)
    with pytest.raises(VKInvalidRequestError):
        await client.call("newsfeed.search", {"count": 250})
    await client.close()
    assert len(recorder.requests) == 1


async def test_network_error_is_retried() -> None:
    recorder = Recorder(
        [httpx.ConnectError("boom"), httpx.Response(200, json={"response": 1})]
    )
    client = make_client(recorder)
    try:
        assert await client.call("users.get") == 1
    finally:
        await client.close()
    assert len(recorder.requests) == 2


async def test_network_errors_exhaust_retries() -> None:
    recorder = Recorder([httpx.ConnectError("boom") for _ in range(4)])
    client = make_client(recorder, max_retries=2)
    with pytest.raises(VKNetworkError):
        await client.call("users.get")
    await client.close()


async def test_non_json_response_raises() -> None:
    recorder = Recorder([httpx.Response(200, text="<html>oops</html>")])
    client = make_client(recorder)
    with pytest.raises(VKResponseFormatError):
        await client.call("users.get")
    await client.close()


async def test_unexpected_structure_raises() -> None:
    recorder = Recorder([httpx.Response(200, json={"weird": 1})])
    client = make_client(recorder)
    with pytest.raises(VKResponseFormatError):
        await client.call("users.get")
    await client.close()


async def test_newsfeed_search_caps_count() -> None:
    recorder = Recorder([httpx.Response(200, json={"response": {"items": []}})])
    client = make_client(recorder)
    try:
        await client.newsfeed_search(query="Python", start_time=1, end_time=2, count=500)
    finally:
        await client.close()
    body = recorder.requests[0].content.decode()
    assert "count=200" in body
    assert "extended=1" in body
    assert "start_time=1" in body and "end_time=2" in body


async def test_backoff_delays_grow() -> None:
    client = make_client(Recorder([]), backoff_base=1.5, backoff_max=30)
    await client.close()
    assert client._backoff_delay(1) == pytest.approx(1.5)
    assert client._backoff_delay(2) == pytest.approx(2.25)
    assert client._backoff_delay(50) == 30


def test_build_error_mapping() -> None:
    assert isinstance(build_error({"error_code": 5}), VKAuthError)
    assert isinstance(build_error({"error_code": 15}), VKAccessError)
    assert isinstance(build_error({"error_code": 6}), VKRateLimitError)
    assert isinstance(build_error({"error_code": 100}), VKInvalidRequestError)
    assert build_error({"error_code": 5}).is_retryable is False
    assert build_error({"error_code": 6}).is_retryable is True
