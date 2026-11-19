"""Тесты отправки в Telegram (ТЗ п.14.2, п.14.3)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.telegram.formatter import build_notification
from app.telegram.sender import (
    TelegramError,
    TelegramPermanentError,
    TelegramSender,
    TelegramTransientError,
)

MESSAGE = build_notification(
    post_text="Текст публикации про Python",
    comment="Комментарий про aiogram и асинхронность",
    topics=["Python"],
    post_url="https://vk.com/wall-1_2",
)


def ok_response(message_id: int = 100) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "ok": True,
            "result": {
                "message_id": message_id,
                "chat": {"id": 238583074, "type": "private"},
            },
        },
    )


def error_response(code: int, description: str, retry_after: int | None = None) -> httpx.Response:
    body: dict = {"ok": False, "error_code": code, "description": description}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return httpx.Response(200, json=body)


class Recorder:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if self.responses else ok_response()
        if isinstance(item, Exception):
            raise item
        return item


def make_sender(recorder: Recorder) -> TelegramSender:
    return TelegramSender(token="123:abc", transport=httpx.MockTransport(recorder))


def test_requires_token() -> None:
    with pytest.raises(TelegramError):
        TelegramSender(token="")


async def test_send_success() -> None:
    recorder = Recorder([ok_response(555)])
    sender = make_sender(recorder)
    try:
        result = await sender.send("238583074", MESSAGE)
    finally:
        await sender.close()

    assert result.message_id == 555
    assert result.chat_id == "238583074"
    request = recorder.requests[0]
    assert request.url.path.endswith("/sendMessage")
    payload = json.loads(request.content)
    assert payload["parse_mode"] == "HTML"
    assert payload["chat_id"] == "238583074"
    keyboard = json.loads(payload["reply_markup"])
    assert keyboard["inline_keyboard"][0][0]["url"] == "https://vk.com/wall-1_2"


async def test_send_encodes_html_content() -> None:
    recorder = Recorder([ok_response()])
    sender = make_sender(recorder)
    try:
        await sender.send("1", MESSAGE)
    finally:
        await sender.close()
    payload = json.loads(recorder.requests[0].content)
    assert "<b>" in payload["text"]


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
async def test_transient_codes(code: int) -> None:
    recorder = Recorder([error_response(code, "temporary")])
    sender = make_sender(recorder)
    with pytest.raises(TelegramTransientError):
        await sender.send("1", MESSAGE)
    await sender.close()


async def test_retry_after_parsed() -> None:
    recorder = Recorder([error_response(429, "Too Many Requests", retry_after=25)])
    sender = make_sender(recorder)
    with pytest.raises(TelegramTransientError) as exc:
        await sender.send("1", MESSAGE)
    await sender.close()
    assert exc.value.retry_after == 25


@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_permanent_codes(code: int) -> None:
    recorder = Recorder([error_response(code, "bad")])
    sender = make_sender(recorder)
    with pytest.raises(TelegramPermanentError):
        await sender.send("1", MESSAGE)
    await sender.close()


async def test_network_error_is_transient() -> None:
    recorder = Recorder([httpx.ConnectError("no network")])
    sender = make_sender(recorder)
    with pytest.raises(TelegramTransientError):
        await sender.send("1", MESSAGE)
    await sender.close()


async def test_timeout_is_transient() -> None:
    recorder = Recorder([httpx.ReadTimeout("slow")])
    sender = make_sender(recorder)
    with pytest.raises(TelegramTransientError):
        await sender.send("1", MESSAGE)
    await sender.close()


async def test_non_json_is_transient() -> None:
    recorder = Recorder([httpx.Response(200, text="<html>")])
    sender = make_sender(recorder)
    with pytest.raises(TelegramTransientError):
        await sender.send("1", MESSAGE)
    await sender.close()


async def test_unexpected_structure_is_permanent() -> None:
    recorder = Recorder([httpx.Response(200, json={"ok": True, "result": {"no_id": 1}})])
    sender = make_sender(recorder)
    with pytest.raises(TelegramPermanentError):
        await sender.send("1", MESSAGE)
    await sender.close()


async def test_send_plain() -> None:
    recorder = Recorder([ok_response(7)])
    sender = make_sender(recorder)
    try:
        result = await sender.send_plain("1", "тест")
    finally:
        await sender.close()
    assert result.message_id == 7
