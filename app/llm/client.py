"""Клиент AiDostup (OpenAI-совместимый API, ТЗ п.12).

Особенности, выявленные при проверке API (docs/llm_model_choice.md):
* базовый URL и модель берутся из конфигурации, в коде не зашиты;
* ответы ``claude-opus-5-5`` иногда занимают 17–22 с → таймаут по умолчанию 90 с;
* повтор безопасен только для ``429`` и ``503`` (документация AiDostup),
  сетевые ошибки и таймауты тоже повторяются;
* ``400/401/402/403`` не повторяются — это ошибки запроса или ключа.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class LLMError(Exception):
    """Базовая ошибка LLM."""


class LLMConfigurationError(LLMError):
    """Не заданы ключ или модель."""


class LLMRequestError(LLMError):
    """Ошибка запроса (неверные параметры, нет баланса, доступ запрещён)."""


class LLMResponseError(LLMError):
    """Ответ получен, но не соответствует ожидаемой структуре."""


@dataclass(slots=True)
class LLMResponse:
    """Результат одного вызова чата."""

    content: str
    model: str
    elapsed: float
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    """Асинхронный клиент ``POST /chat/completions``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 90.0,
        max_retries: int = 2,
        backoff_base: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        key = api_key if api_key is not None else settings.aidostup_api_key.get_secret_value()
        self.base_url = (base_url or settings.aidostup_base_url).rstrip("/")
        self.model = model if model is not None else settings.aidostup_model
        if not key:
            raise LLMConfigurationError("AIDOSTUP_API_KEY не задан в конфигурации")
        if not self.model:
            raise LLMConfigurationError("AIDOSTUP_MODEL не задан в конфигурации")

        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 700,
        model: str | None = None,
    ) -> LLMResponse:
        """Выполняет запрос к чату с ограниченным числом повторов."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            started = time.monotonic()
            try:
                response = await self._client.post(
                    f"{self.base_url}/chat/completions", json=payload
                )
            except httpx.HTTPError as exc:
                last_error = LLMError(f"сетевая ошибка: {exc}")
                logger.warning("LLM: сеть (попытка %s/%s): %s", attempt, self._max_retries, exc)
            else:
                elapsed = time.monotonic() - started
                if response.status_code == 200:
                    return self._parse_success(response, elapsed)
                if response.status_code in RETRYABLE_STATUS:
                    last_error = LLMError(
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                    logger.warning(
                        "LLM: временная ошибка %s (попытка %s/%s)",
                        response.status_code,
                        attempt,
                        self._max_retries,
                    )
                else:
                    raise LLMRequestError(
                        f"HTTP {response.status_code}: {response.text[:300]}"
                    )

            if attempt < self._max_retries:
                await self._sleep(self._backoff_base**attempt)

        raise LLMError(f"запрос не выполнен за {self._max_retries} попыток: {last_error}")

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_success(response: httpx.Response, elapsed: float) -> LLMResponse:
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError("ответ не является JSON") from exc

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(
                f"неожиданная структура ответа: {str(data)[:200]}"
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("модель вернула пустой ответ")

        return LLMResponse(
            content=content,
            model=str(data.get("model") or ""),
            elapsed=elapsed,
            usage=data.get("usage") or {},
            raw=data,
        )
