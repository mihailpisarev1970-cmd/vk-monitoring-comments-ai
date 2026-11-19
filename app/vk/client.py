"""HTTP-клиент VK API.

Особенности (ТЗ п.6.1):
* авторизация сервисным ключом (заголовок Authorization: Bearer, как
  рекомендует документация VK);
* троттлинг — минимальный интервал между запросами;
* экспоненциальный backoff с ограниченным числом попыток;
* версия API берётся из конфигурации (ТЗ п.3.1).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.vk.exceptions import (
    VKAuthError,
    VKConfigurationError,
    VKError,
    VKNetworkError,
    VKResponseFormatError,
    build_error,
)

logger = get_logger(__name__)

VK_API_BASE = "https://api.vk.com/method"


class VKClient:
    """Асинхронный клиент VK API с retry и троттлингом."""

    def __init__(
        self,
        *,
        access_token: str | None = None,
        api_version: str | None = None,
        base_url: str = VK_API_BASE,
        min_interval: float = 0.4,
        max_retries: int = 4,
        backoff_base: float = 1.5,
        backoff_max: float = 30.0,
        timeout: float = 25.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        token = (
            access_token
            if access_token is not None
            else settings.vk_access_token.get_secret_value()
        )
        version = api_version or settings.vk_api_version
        if not token:
            raise VKConfigurationError("VK_ACCESS_TOKEN не задан в конфигурации")
        if not version:
            raise VKConfigurationError("VK_API_VERSION не задан в конфигурации")

        self._token = token
        self._version = version
        self._base_url = base_url.rstrip("/")
        self._min_interval = max(0.0, min_interval)
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._sleep = sleep
        self._last_request_at = 0.0
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "VKMonitorAssistant/1.0 (+python-httpx)",
            },
        )

    # ---------- lifecycle ----------
    async def __aenter__(self) -> VKClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ---------- public API ----------
    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Вызывает метод VK API и возвращает поле `response`.

        Повторяются только транзиентные ошибки (лимит частоты, 5xx, сеть),
        не более `max_retries` попыток. Ошибки авторизации и параметров
        пробрасываются сразу.
        """
        payload = {k: v for k, v in (params or {}).items() if v is not None}
        payload["v"] = self._version

        last_error: VKError | None = None
        for attempt in range(1, self._max_retries + 1):
            await self._throttle()
            try:
                response = await self._client.post(
                    f"{self._base_url}/{method}", data=self._encode(payload)
                )
            except httpx.HTTPError as exc:
                last_error = VKNetworkError(f"{method}: сетевая ошибка — {exc}")
                logger.warning(
                    "VK %s: network error (attempt %s/%s): %s",
                    method,
                    attempt,
                    self._max_retries,
                    exc,
                )
            else:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise VKResponseFormatError(
                        f"{method}: ответ не является JSON (HTTP {response.status_code})"
                    ) from exc

                if "error" in data:
                    last_error = build_error(data["error"])
                    if isinstance(last_error, VKAuthError):
                        logger.error("VK %s: ошибка авторизации — %s", method, last_error)
                        raise last_error
                    if not last_error.is_retryable:
                        raise last_error
                    logger.warning(
                        "VK %s: %s (attempt %s/%s)",
                        method,
                        last_error,
                        attempt,
                        self._max_retries,
                    )
                elif "response" in data:
                    return data["response"]
                else:
                    raise VKResponseFormatError(
                        f"{method}: неожиданная структура ответа: {str(data)[:200]}"
                    )

            if attempt < self._max_retries:
                await self._sleep(self._backoff_delay(attempt))

        if last_error is not None:
            raise last_error
        raise VKError(f"{method}: запрос не выполнен")

    async def newsfeed_search(
        self,
        *,
        query: str,
        start_time: int,
        end_time: int,
        count: int = 200,
        extended: bool = True,
    ) -> dict[str, Any]:
        """Поиск публикаций через `newsfeed.search` (ТЗ п.6.1).

        Пагинация недоступна с сервисным ключом, поэтому count ограничен 200,
        а глубина регулируется длиной окна (см. docs/vk_api_check.md).
        """
        result = await self.call(
            "newsfeed.search",
            {
                "q": query,
                "start_time": start_time,
                "end_time": end_time,
                "count": min(count, 200),
                "extended": 1 if extended else 0,
            },
        )
        if not isinstance(result, dict):
            raise VKResponseFormatError("newsfeed.search: response не является объектом")
        return result

    async def wall_get_by_id(self, owner_id: int, post_id: int) -> dict[str, Any]:
        """Точная загрузка публикации — повторная проверка рекламных полей."""
        result = await self.call("wall.getById", {"posts": f"{owner_id}_{post_id}"})
        if not isinstance(result, list):
            raise VKResponseFormatError("wall.getById: неожиданный формат response")
        return result[0] if result else {}

    # ---------- internals ----------
    @staticmethod
    def _encode(payload: dict[str, Any]) -> dict[str, str]:
        return {
            key: str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in payload.items()
        }

    async def _throttle(self) -> None:
        """Гарантирует минимальный интервал между запросами."""
        if self._min_interval <= 0:
            return
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            await self._sleep(wait)
        self._last_request_at = time.monotonic()

    def _backoff_delay(self, attempt: int) -> float:
        """Экспоненциальная задержка: 1.5, 2.25, 3.4, … с потолком."""
        return min(self._backoff_base**attempt, self._backoff_max)
