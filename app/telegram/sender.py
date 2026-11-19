"""Отправка сообщений в Telegram (ТЗ п.14.2, п.14.3).

Используется httpx к api.telegram.org; aiogram 3 применяется в ``bot.py``.
Такое разделение делает очередь доставки тестируемой и независимой от
event loop бота.

Классификация ошибок (ТЗ п.14.3):
* 429, 5xx, сетевые ошибки и таймауты — **временные**, повторяем с задержкой;
* 400, 401, 403, 404 — **постоянные**, повторы бессмысленны.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.telegram.formatter import NotificationMessage

logger = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org"

#: Коды, при которых повтор имеет смысл
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
#: Коды, при которых повтор бессмысленен
PERMANENT_STATUS = frozenset({400, 401, 403, 404})


class TelegramError(Exception):
    """Базовая ошибка отправки."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class TelegramTransientError(TelegramError):
    """Временная ошибка — можно повторить (ТЗ п.14.3)."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float = 0.0):
        super().__init__(message, status=status, retryable=True)
        self.retry_after = retry_after


class TelegramPermanentError(TelegramError):
    """Постоянная ошибка — повтор не поможет."""


@dataclass(slots=True)
class SendResult:
    """Результат отправки."""

    message_id: int
    chat_id: str


class TelegramSender:
    """Отправляет готовые сообщения в Telegram."""

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = TELEGRAM_API,
    ) -> None:
        self._token = (
            token if token is not None else settings.telegram_bot_token.get_secret_value()
        )
        if not self._token:
            raise TelegramError("TELEGRAM_BOT_TOKEN не задан в конфигурации")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def __aenter__(self) -> TelegramSender:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def _api(self) -> str:
        """Базовый URL методов бота."""
        return f"{self._base_url}/bot{self._token}"

    async def send(
        self,
        chat_id: str,
        message: NotificationMessage,
        *,
        disable_preview: bool = True,
    ) -> SendResult:
        """Отправляет уведомление с inline-кнопкой (ТЗ п.14.2)."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": message.text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
            "reply_markup": json.dumps(message.keyboard(), ensure_ascii=False),
        }
        return await self._post("sendMessage", payload)

    async def send_plain(self, chat_id: str, text: str) -> SendResult:
        """Отправляет простой текст (служебные сообщения, отладка)."""
        return await self._post("sendMessage", {"chat_id": chat_id, "text": text})

    async def health(self) -> dict[str, Any]:
        """Проверяет токен и доступность чата (getMe + getChat)."""
        try:
            me = self._parse(await self._client.get(f"{self._api}/getMe"))
            chat_id = settings.telegram_chat_id
            if not chat_id:
                return {"bot": me, "chat": None, "chat_ok": False}
            chat = self._parse(
                await self._client.get(f"{self._api}/getChat", params={"chat_id": chat_id})
            )
            return {"bot": me, "chat": chat, "chat_ok": True}
        except TelegramError:
            raise
        except httpx.HTTPError as exc:
            raise TelegramTransientError(f"сетевая ошибка: {exc}") from exc

    async def _post(self, method: str, payload: dict[str, Any]) -> SendResult:
        body = {key: value for key, value in payload.items() if value is not None}
        try:
            response = await self._client.post(f"{self._api}/{method}", json=body)
            data = self._parse(response)
        except TelegramError:
            raise
        except httpx.TimeoutException as exc:
            raise TelegramTransientError(f"таймаут запроса: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TelegramTransientError(f"сетевая ошибка: {exc}") from exc

        result = data.get("result") or {}
        try:
            message_id = int(result["message_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TelegramPermanentError(
                f"неожиданный ответ Telegram: {str(data)[:200]}"
            ) from exc
        chat = result.get("chat") or {}
        return SendResult(
            message_id=message_id,
            chat_id=str(chat.get("id") or body.get("chat_id")),
        )

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramTransientError(
                f"ответ не является JSON (HTTP {response.status_code})"
            ) from exc

        if data.get("ok"):
            return data

        description = data.get("description") or "unknown error"
        code = int(data.get("error_code") or response.status_code)
        if code in RETRYABLE_STATUS or response.status_code in RETRYABLE_STATUS:
            retry_after = float(
                (data.get("parameters") or {}).get("retry_after")
                or response.headers.get("Retry-After")
                or 0
            )
            raise TelegramTransientError(
                f"HTTP {code}: {description}", status=code, retry_after=retry_after
            )
        raise TelegramPermanentError(f"HTTP {code}: {description}", status=code)


async def sleep_backoff(attempt: int, base: float = 2.0, cap: float = 300.0) -> None:
    """Задержка между повторами доставки (ТЗ п.14.3)."""
    await asyncio.sleep(min(base**attempt, cap))


    @property
    def _api(self) -> str:
        return f"{self._base_url}/bot{self._token}"
