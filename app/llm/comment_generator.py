"""Генерация и валидация комментариев (ТЗ п.13).

Алгоритм:
1. генерация комментария по промпту ``prompts/comment_generation.txt``;
2. проверка по правилам п.13.2 (:class:`CommentValidator`);
3. при неудаче — повторная генерация с дополнительными инструкциями
   (ограниченное число попыток);
4. если и повтор не помог — публикация **не отправляется** в Telegram.

Результат сохраняется в ``post_analysis`` со стадиями
``comment_generation`` и ``comment_validation``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import PostStatus
from app.db.models import Post, PostAnalysis
from app.filters.loader import load_config
from app.llm.classifier import load_prompt
from app.llm.client import LLMClient, LLMError
from app.llm.comment_validator import CommentValidation, CommentValidator, normalize_comment
from app.llm.schemas import build_user_message
from app.vk.normalizer import clean_text

logger = get_logger(__name__)

COMMENT_PROMPT = "comment_generation.txt"
MAX_POST_CHARS = 3000


@dataclass(slots=True)
class CommentOutcome:
    """Результат генерации комментария."""

    post_id: int
    generated: bool
    comment: str | None = None
    attempts: int = 0
    validation: CommentValidation | None = None
    error: str | None = None
    prompt_version: str = ""
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return bool(self.generated and self.validation and self.validation.valid)


class CommentGenerator:
    """Генерация комментария с проверкой и повторными попытками."""

    stage = "comment_generation"

    def __init__(
        self,
        client: LLMClient,
        *,
        validator: CommentValidator | None = None,
        max_attempts: int = 2,
        temperature: float = 0.7,
        model: str | None = None,
    ) -> None:
        self.client = client
        bundle = load_config()
        comment_cfg = bundle.filters.comment
        self.validator = validator or CommentValidator(
            min_length=comment_cfg.min_length,
            max_length=comment_cfg.max_length,
            commercial_words=bundle.words.commercial,
        )
        self.max_attempts = max(1, max_attempts)
        self.temperature = temperature
        self.model = model or client.model
        system_prompt, self.prompt_version = load_prompt(self.prompt_path())
        self.system_prompt = system_prompt

    @staticmethod
    def prompt_path() -> Path:
        """Путь к промпту генерации комментариев."""
        return Path(__file__).parent / "prompts" / COMMENT_PROMPT

    def _trim_to_limit(self, comment: str) -> str:
        """Подрезает слишком длинный комментарий по границе предложения.

        Быстрые модели (например, claude-haiku-4-5) чаще превышают лимит
        длины. Полная регенерация стоит ещё одного запроса к модели, поэтому
        сначала пробуем аккуратно укоротить текст: отбрасываем последние
        предложения, пока не уложимся в лимит. Если после подрезки текст
        становится короче минимума, возвращаем исходный — пусть решит
        валидатор и регенерация.
        """
        max_length = self.validator.max_length
        min_length = self.validator.min_length
        if len(comment) <= max_length:
            return comment

        # Делим на предложения, сохраняя знаки препинания
        sentences = re.split(r"(?<=[.!?])\s+", comment)
        trimmed = ""
        for sentence in sentences:
            candidate = f"{trimmed} {sentence}".strip() if trimmed else sentence
            if len(candidate) > max_length:
                break
            trimmed = candidate

        if len(trimmed) >= min_length:
            logger.info(
                "Комментарий подрезан по длине: %s → %s символов", len(comment), len(trimmed)
            )
            return trimmed
        return comment

    # ------------------------------------------------------------------
    async def generate(self, post: Post) -> CommentOutcome:
        """Генерирует и проверяет комментарий (без записи в БД)."""
        text = clean_text(post.text or "")[:MAX_POST_CHARS]
        if not text:
            return CommentOutcome(
                post_id=post.id,
                generated=False,
                error="пустой текст публикации",
                prompt_version=self.prompt_version,
            )

        usage_total: dict[str, int] = {}
        last_validation: CommentValidation | None = None
        last_comment = ""
        extra: str | None = None

        for attempt in range(1, self.max_attempts + 1):
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": build_user_message(text, extra)},
            ]
            try:
                response = await self.client.chat(
                    messages, temperature=self.temperature, max_tokens=400
                )
            except LLMError as exc:
                logger.error("Генерация комментария для %s не удалась: %s", post.id, exc)
                return CommentOutcome(
                    post_id=post.id,
                    generated=False,
                    attempts=attempt,
                    validation=last_validation,
                    error=f"ошибка LLM: {exc}",
                    prompt_version=self.prompt_version,
                )

            for key, value in (response.usage or {}).items():
                if isinstance(value, int):
                    usage_total[key] = usage_total.get(key, 0) + value

            last_comment = normalize_comment(response.content)
            # Быстрые модели хуже держат лимит длины: вместо полной
            # регенерации (лишняя LLM-итерация) аккуратно подрезаем по
            # границе предложения.
            last_comment = self._trim_to_limit(last_comment)
            last_validation = self.validator.validate(last_comment, text)

            if last_validation.valid:
                logger.info(
                    "Комментарий для %s готов (%s символов, попытка %s)",
                    post.id,
                    len(last_comment),
                    attempt,
                )
                return CommentOutcome(
                    post_id=post.id,
                    generated=True,
                    comment=last_comment,
                    attempts=attempt,
                    validation=last_validation,
                    prompt_version=self.prompt_version,
                    model=response.model or self.model,
                    usage=usage_total,
                )

            logger.warning(
                "Комментарий для %s не прошёл проверку (попытка %s): %s",
                post.id,
                attempt,
                "; ".join(last_validation.reasons),
            )
            extra = last_validation.instructions()

        return CommentOutcome(
            post_id=post.id,
            generated=False,
            comment=last_comment or None,
            attempts=self.max_attempts,
            validation=last_validation,
            error="комментарий не прошёл проверку после "
            f"{self.max_attempts} попыток",
            prompt_version=self.prompt_version,
            usage=usage_total,
        )



    async def generate_and_save(self, session: AsyncSession, post: Post) -> CommentOutcome:
        """Генерирует комментарий и сохраняет результат в БД."""
        outcome = await self.generate(post)
        if not outcome.model:
            outcome.model = self.model
        await save_comment_analysis(session, post, outcome)
        return outcome


async def save_comment_analysis(
    session: AsyncSession, post: Post, outcome: CommentOutcome
) -> PostAnalysis:
    """Записывает результат в ``post_analysis`` и обновляет статус публикации.

    * комментарий валиден → статус остаётся ``approved`` (готов к отправке);
    * не прошёл проверку → ``rejected_comment`` (ТЗ п.13.2);
    * ошибка LLM → статус не меняется, публикация не отправляется.
    """
    validation = outcome.validation
    checks = validation.checks if validation else []
    failed = validation.failed if validation else []

    analysis = PostAnalysis(
        post_id=post.id,
        stage=CommentGenerator.stage,
        model=outcome.model,
        prompt_version=outcome.prompt_version,
        accepted=outcome.valid,
        reason=(
            outcome.comment
            if outcome.valid
            else (outcome.error or "; ".join(validation.reasons if validation else []))
        ),
        raw_response={
            "attempts": outcome.attempts,
            "usage": outcome.usage,
            "comment_length": len(outcome.comment or ""),
            "checks": {
                check.name: {"passed": check.passed, "detail": check.detail}
                for check in checks
            },
        },
        rejection_reasons=[f"{check.name}: {check.detail}" for check in failed]
        or (None if outcome.valid else ["generation_failed"]),
    )
    session.add(analysis)

    if outcome.valid:
        post.status = PostStatus.APPROVED
        post.status_reason = (
            f"comment: готов, {len(outcome.comment or '')} символов, "
            f"попыток {outcome.attempts}"
        )
    elif validation is not None:
        post.status = PostStatus.REJECTED_COMMENT
        reasons = validation.reasons
        post.status_reason = "comment: не прошёл проверку — " + "; ".join(reasons[:3])
    else:
        post.status_reason = f"comment: не сгенерирован — {outcome.error}"

    await session.flush()
    return analysis


def prompt_checksum(prompt_text: str) -> str:
    """Короткий хеш промпта для диагностики версий."""
    return hashlib.sha1(prompt_text.encode("utf-8")).hexdigest()[:12]
