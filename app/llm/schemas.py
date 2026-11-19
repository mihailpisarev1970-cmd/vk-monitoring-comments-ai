"""Схема и валидация ответа LLM-классификатора (ТЗ п.12.4).

Требования ТЗ:
* JSON должен валидироваться, типы полей проверяются;
* при отсутствии обязательных полей ответ считается некорректным;
* некорректный JSON **нельзя** принимать за положительный результат.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: Маркеры блока с текстом публикации — защита от prompt injection (ТЗ п.12.3)
POST_START = "<<<POST>>>"
POST_END = "<<<END POST>>>"

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class ClassificationSchemaError(Exception):
    """Ответ модели не соответствует схеме (некорректный JSON или поля)."""

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class ClassificationResult(BaseModel):
    """Результат классификации публикации (ТЗ п.12.4)."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    relevant: bool
    is_advertising: bool
    is_course_offer: bool
    is_commercial_promotion: bool
    is_spam: bool
    is_low_quality: bool
    is_clickbait: bool
    is_engagement_bait: bool
    is_suspicious: bool
    has_information_value: bool
    confidence: float = Field(ge=0.0, le=1.0)
    matched_topics: list[str] = Field(default_factory=list)
    spam_categories: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: Any) -> Any:
        """Принимает «0.92» и «92%», но не превращает мусор в число."""
        if isinstance(value, bool):
            raise ValueError("confidence не может быть boolean")
        if isinstance(value, str):
            cleaned = value.strip().rstrip("%")
            parsed = float(cleaned)
            return parsed / 100 if value.strip().endswith("%") else parsed
        return value

    @field_validator("matched_topics", "spam_categories", mode="before")
    @classmethod
    def _coerce_str_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value else []
        return value

    @field_validator("reason", mode="before")
    @classmethod
    def _coerce_reason(cls, value: Any) -> Any:
        if value is None:
            return ""
        text = str(value).strip()
        return text[:500]

    # ------------------------------------------------------------------
    @property
    def forbidden_flags(self) -> list[str]:
        """Запрещающие признаки, установленные моделью (ТЗ п.12.5)."""
        flags = {
            "is_advertising": self.is_advertising,
            "is_course_offer": self.is_course_offer,
            "is_commercial_promotion": self.is_commercial_promotion,
            "is_spam": self.is_spam,
            "is_low_quality": self.is_low_quality,
            "is_clickbait": self.is_clickbait,
            "is_engagement_bait": self.is_engagement_bait,
            "is_suspicious": self.is_suspicious,
        }
        return [name for name, value in flags.items() if value]


def extract_json(raw: str) -> dict[str, Any]:
    """Извлекает JSON-объект из ответа модели.

    Модель может обернуть ответ в ```json … ``` или добавить пояснение —
    это не повод терять результат, но при полном отсутствии JSON ошибка.
    """
    if not raw or not raw.strip():
        raise ClassificationSchemaError("пустой ответ модели", raw=raw)

    cleaned = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(cleaned)
        if not match:
            raise ClassificationSchemaError(
                "в ответе модели нет JSON-объекта", raw=raw
            ) from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ClassificationSchemaError(
                f"некорректный JSON: {exc.msg} (позиция {exc.pos})", raw=raw
            ) from exc

    if not isinstance(data, dict):
        raise ClassificationSchemaError("ожидался JSON-объект", raw=raw)
    return data


def parse_classification(raw: str) -> ClassificationResult:
    """Разбирает и валидирует ответ модели.

    Raises:
        ClassificationSchemaError: если JSON некорректен или не хватает
            обязательных полей. Такой ответ **никогда** не считается
            положительным (ТЗ п.12.4).
    """
    data = extract_json(raw)
    try:
        return ClassificationResult.model_validate(data)
    except ValidationError as exc:
        missing = [
            str(error["loc"][0])
            for error in exc.errors()
            if error.get("type") == "missing"
        ]
        if missing:
            raise ClassificationSchemaError(
                f"в ответе отсутствуют обязательные поля: {', '.join(missing)}", raw=raw
            ) from exc
        raise ClassificationSchemaError(
            f"поля ответа не соответствуют схеме: {exc.error_count()} ошиб.", raw=raw
        ) from exc


def build_user_message(text: str, extra: str | None = None) -> str:
    """Формирует сообщение пользователя с текстом как недоверенными данными."""
    parts = [POST_START, text.strip(), POST_END]
    if extra:
        parts.extend(["", extra])
    return "\n".join(parts)
