"""Классификатор публикаций через LLM (ТЗ п.12).

Оркестрация:
1. загрузка системного промпта ``prompts/classification.txt``;
2. запрос к модели с текстом как недоверенными данными;
3. валидация строгого JSON (ограниченное число повторов при ошибке парсинга);
4. применение правил решения (п.12.5);
5. сохранение результата и причины в ``post_analysis`` (п.12.4).

Некорректный JSON **никогда** не считается положительным результатом.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import PostStatus
from app.db.models import Post, PostAnalysis
from app.llm.client import LLMClient, LLMError, LLMResponse
from app.llm.decision import Decision, decide
from app.llm.schemas import (
    ClassificationResult,
    ClassificationSchemaError,
    build_user_message,
    parse_classification,
)
from app.vk.normalizer import clean_text

logger = get_logger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
CLASSIFICATION_PROMPT = PROMPTS_DIR / "classification.txt"

#: Ограничение длины текста, отправляемого в модель
MAX_POST_CHARS = 4000


class PromptLoadError(Exception):
    """Не удалось загрузить системный промпт."""


@dataclass(slots=True)
class ClassificationOutcome:
    """Результат классификации с диагностикой."""

    post_id: int
    analyzed: bool
    result: ClassificationResult | None = None
    decision: Decision | None = None
    attempts: int = 0
    error: str | None = None
    prompt_version: str = ""
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return bool(self.decision and self.decision.accepted)


def load_prompt(path: Path = CLASSIFICATION_PROMPT) -> tuple[str, str]:
    """Возвращает (текст промпта, версию) — версия это хеш содержимого."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptLoadError(f"не удалось прочитать промпт {path}: {exc}") from exc
    if not text.strip():
        raise PromptLoadError(f"промпт {path} пуст")
    version = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return text, version


class PostClassifier:
    """Классификация публикаций и сохранение анализа."""

    stage = "classify"

    def __init__(
        self,
        client: LLMClient,
        *,
        confidence_threshold: float = 0.85,
        max_parse_retries: int = 2,
        model: str | None = None,
    ) -> None:
        self.client = client
        self.confidence_threshold = confidence_threshold
        self.max_parse_retries = max(0, max_parse_retries)
        self.model = model or client.model
        self.system_prompt, self.prompt_version = load_prompt()

    async def classify(self, post: Post) -> ClassificationOutcome:
        """Классифицирует публикацию, не изменяя БД."""
        text = clean_text(post.text or "")[:MAX_POST_CHARS]
        if not text:
            return ClassificationOutcome(
                post_id=post.id,
                analyzed=False,
                error="пустой текст публикации",
                prompt_version=self.prompt_version,
            )

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": build_user_message(text)},
        ]

        attempts = 0
        last_error: str | None = None
        usage_total: dict[str, int] = {}

        for attempt in range(1, self.max_parse_retries + 2):
            attempts = attempt
            try:
                response: LLMResponse = await self.client.chat(messages)
            except LLMError as exc:
                logger.error("LLM недоступна для публикации %s: %s", post.id, exc)
                return ClassificationOutcome(
                    post_id=post.id,
                    analyzed=False,
                    attempts=attempts,
                    error=f"ошибка LLM: {exc}",
                    prompt_version=self.prompt_version,
                )

            for key, value in (response.usage or {}).items():
                if isinstance(value, int):
                    usage_total[key] = usage_total.get(key, 0) + value

            try:
                result = parse_classification(response.content)
            except ClassificationSchemaError as exc:
                last_error = str(exc)
                logger.warning(
                    "Некорректный ответ LLM для %s (попытка %s): %s", post.id, attempt, exc
                )
                if attempt <= self.max_parse_retries:
                    messages = [
                        {"role": "system", "content": self.system_prompt},
                        {
                            "role": "user",
                            "content": build_user_message(
                                text,
                                "Предыдущий ответ не соответствовал схеме. "
                                "Верни только валидный JSON со всеми обязательными полями.",
                            ),
                        },
                    ]
                continue

            decision = decide(result, self.confidence_threshold)
            logger.info(
                "Публикация %s: %s (confidence=%.2f, темы=%s)",
                post.id,
                "принята" if decision.accepted else "отклонена",
                result.confidence,
                ",".join(result.matched_topics[:3]) or "-",
            )
            return ClassificationOutcome(
                post_id=post.id,
                analyzed=True,
                result=result,
                decision=decision,
                attempts=attempts,
                prompt_version=self.prompt_version,
                model=response.model or self.model,
                usage=usage_total,
            )

        return ClassificationOutcome(
            post_id=post.id,
            analyzed=False,
            attempts=attempts,
            error=f"некорректный JSON после {attempts} попыток: {last_error}",
            prompt_version=self.prompt_version,
        )

    async def classify_and_save(
        self, session: AsyncSession, post: Post
    ) -> ClassificationOutcome:
        """Классифицирует публикацию и сохраняет результат в БД."""
        outcome = await self.classify(post)
        if not outcome.model:
            outcome.model = self.model
        await save_analysis(session, post, outcome)
        return outcome


async def save_analysis(
    session: AsyncSession, post: Post, outcome: ClassificationOutcome
) -> PostAnalysis:
    """Сохраняет результат классификации в ``post_analysis`` (ТЗ п.12.4).

    Статус публикации:
    * принята → ``approved`` (готово к генерации комментария);
    * отклонена → ``rejected_llm`` с причиной;
    * ошибка LLM/некорректный JSON → публикация не отправляется, статус
      остаётся прежним, а попытка фиксируется с ``accepted = False``.
    """
    result = outcome.result
    analysis = PostAnalysis(
        post_id=post.id,
        stage=PostClassifier.stage,
        model=outcome.model or "",
        prompt_version=outcome.prompt_version,
        relevant=result.relevant if result else None,
        is_advertising=result.is_advertising if result else None,
        is_course_offer=result.is_course_offer if result else None,
        is_commercial_promotion=result.is_commercial_promotion if result else None,
        is_spam=result.is_spam if result else None,
        is_low_quality=result.is_low_quality if result else None,
        is_clickbait=result.is_clickbait if result else None,
        is_engagement_bait=result.is_engagement_bait if result else None,
        is_suspicious=result.is_suspicious if result else None,
        has_information_value=result.has_information_value if result else None,
        confidence=result.confidence if result else None,
        matched_topics=result.matched_topics if result else None,
        spam_categories=result.spam_categories if result else None,
        reason=result.reason if result else (outcome.error or ""),
        raw_response={"usage": outcome.usage, "attempts": outcome.attempts},
        accepted=outcome.accepted,
        rejection_reasons=(
            outcome.decision.rejection_reasons if outcome.decision else ["llm_error"]
        ),
    )
    session.add(analysis)

    if outcome.analyzed and outcome.decision is not None:
        if outcome.decision.accepted:
            post.status = PostStatus.APPROVED
            post.status_reason = "llm: публикация допущена к генерации комментария"
        else:
            post.status = PostStatus.REJECTED_LLM
            post.status_reason = f"llm: {outcome.decision.explanation}"
    else:
        # Консервативно: при ошибке публикация не идёт дальше
        post.status_reason = f"llm: ошибка анализа — {outcome.error}"

    await session.flush()
    return analysis
