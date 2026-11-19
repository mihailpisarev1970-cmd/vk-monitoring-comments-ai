"""Исключения VK API (ТЗ п.6.1 — обработка ошибок авторизации, лимитов и сбоев)."""

from __future__ import annotations

from typing import Any


class VKError(Exception):
    """Базовая ошибка работы с VK API."""

    def __init__(self, message: str, *, error_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.error_code = error_code
        self.payload = payload

    @property
    def is_retryable(self) -> bool:
        """Можно ли повторять запрос."""
        return False


class VKAuthError(VKError):
    """Ключ доступа недействителен или отозван (error_code 5, 27, 28)."""

    ERROR_CODES = (5, 27, 28)

    @property
    def is_retryable(self) -> bool:
        return False


class VKAccessError(VKError):
    """Доступ к методу запрещён для данного ключа (error_code 15, 7, 18, 203)."""

    ERROR_CODES = (7, 15, 18, 203)

    @property
    def is_retryable(self) -> bool:
        return False


class VKRateLimitError(VKError):
    """Превышен лимит частоты запросов (error_code 6, 29, 30)."""

    ERROR_CODES = (6, 29, 30)

    @property
    def is_retryable(self) -> bool:
        return True


class VKServerError(VKError):
    """Временная недоступность API или внутренняя ошибка VK (error_code 1, 10, 2)."""

    ERROR_CODES = (1, 2, 10)

    @property
    def is_retryable(self) -> bool:
        return True


class VKInvalidRequestError(VKError):
    """Некорректные параметры запроса (error_code 100).

    Повтор с теми же параметрами бессмысленен.
    """

    ERROR_CODES = (100, 14, 113)

    @property
    def is_retryable(self) -> bool:
        return False


class VKResponseFormatError(VKError):
    """Ответ API не соответствует ожидаемой структуре (ТЗ п.6.1 — изменения структуры)."""

    @property
    def is_retryable(self) -> bool:
        return False


class VKNetworkError(VKError):
    """Сетевая ошибка или таймаут HTTP-запроса."""

    @property
    def is_retryable(self) -> bool:
        return True


class VKConfigurationError(VKError):
    """Не заданы ключи доступа или версия API."""

    @property
    def is_retryable(self) -> bool:
        return False


def build_error(error: dict[str, Any]) -> VKError:
    """Преобразует объект `error` из ответа VK в конкретный класс исключения."""
    code = error.get("error_code")
    message = error.get("error_msg") or f"VK API error {code}"
    params = error.get("request_params")

    mapping: list[tuple[tuple[int, ...], type[VKError]]] = [
        (VKAuthError.ERROR_CODES, VKAuthError),
        (VKAccessError.ERROR_CODES, VKAccessError),
        (VKRateLimitError.ERROR_CODES, VKRateLimitError),
        (VKServerError.ERROR_CODES, VKServerError),
        (VKInvalidRequestError.ERROR_CODES, VKInvalidRequestError),
    ]
    for codes, exc_cls in mapping:
        if code in codes:
            return exc_cls(message, error_code=code, payload={"error": error, "params": params})
    return VKError(message, error_code=code, payload=error)
