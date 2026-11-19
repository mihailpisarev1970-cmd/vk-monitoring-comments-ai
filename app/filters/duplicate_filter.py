"""Фильтр дубликатов и похожих текстов (ТЗ п.11.4).

Реализация:
* точные дубли — по нормализованному тексту (``text_hash`` и сравнение текста);
* близкие тексты — **SimHash** (64-бит) по шинглам слов с порогом из конфигурации;
* одинаковые рекламные тексты, массово рассылаемые разными аккаунтами,
  выявляются как потенциальный спам;
* похожие технические вопросы **не** отсекаются автоматически: если тексты
  различаются по значимым словам (вопросам), решение принимает LLM —
  фильтр возвращает ``SUSPECT``, а не ``REJECT``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from app.core.logging import get_logger
from app.filters.base import FilterContext, FilterResult
from app.filters.loader import DuplicateSettings
from app.vk.models import NormalizedPost
from app.vk.normalizer import clean_text

logger = get_logger(__name__)

_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+", re.UNICODE)
_HASH_BITS = 64

#: Признаки того, что текст — вопрос или техническое обсуждение
_QUESTION_MARKERS = (
    "?", "подскажите", "как ", "почему", "зачем", "что лучше", "кто знает",
    "посоветуйте", "какую", "какой", "можно ли", "стоит ли",
)


def tokenize(text: str) -> list[str]:
    """Разбивает текст на слова в нижнем регистре."""
    return [word.lower() for word in _WORD_RE.findall(text)]


def shingles(tokens: list[str], size: int) -> list[str]:
    """Формирует шинглы (n-граммы слов)."""
    if size <= 0 or len(tokens) < size:
        return tokens or []
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def _hash64(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash(text: str, shingle_size: int = 3) -> int:
    """Вычисляет 64-битный SimHash текста."""
    tokens = tokenize(clean_text(text))
    features = shingles(tokens, shingle_size) if tokens else []
    if not features:
        return 0
    vector = [0] * _HASH_BITS
    for feature in features:
        value = _hash64(feature)
        for bit in range(_HASH_BITS):
            vector[bit] += 1 if value & (1 << bit) else -1
    result = 0
    for bit in range(_HASH_BITS):
        if vector[bit] > 0:
            result |= 1 << bit
    return result


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def similarity(left: int, right: int) -> float:
    """Доля совпавших битов (1.0 — идентичные тексты)."""
    return 1.0 - hamming_distance(left, right) / _HASH_BITS


def is_question_like(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _QUESTION_MARKERS)


class DuplicateFilter:
    """Обнаружение точных и близких дубликатов."""

    name = "duplicate"

    def __init__(self, settings_: DuplicateSettings) -> None:
        self.settings = settings_
        self.enabled = True

    def check(self, post: NormalizedPost, context: FilterContext) -> FilterResult:
        text = clean_text(post.text)
        if not text:
            return FilterResult.passthrough(self.name)

        # Точный дубль (сравнение нормализованного текста)
        for known in context.seen_texts:
            if known == text:
                return FilterResult.reject(
                    self.name,
                    "точный дубль ранее обработанной публикации",
                    signals=["exact_duplicate"],
                    details={"similarity": 1.0},
                )

        # 2. Близкие тексты (SimHash). Короткие тексты сравниваются только точно.
        if len(text) < self.settings.min_length_for_similarity:
            return FilterResult.passthrough(self.name)

        post_hash = simhash(text, self.settings.shingle_size)
        threshold = self.settings.similarity_threshold
        best = 0.0
        checked = 0
        for known in context.seen_texts:
            if len(known) < self.settings.min_length_for_similarity:
                continue
            checked += 1
            score = similarity(post_hash, simhash(known, self.settings.shingle_size))
            best = max(best, score)
            if score >= threshold:
                # Похожий технический вопрос — сигнал для LLM, а не отказ
                if is_question_like(text) and not is_question_like(known):
                    return FilterResult.suspect(
                        self.name,
                        "текст похож на известный, но содержит вопрос",
                        signals=["near_duplicate_question"],
                        details={"similarity": round(score, 3)},
                    )
                return FilterResult.reject(
                    self.name,
                    "близкий дубль ранее обработанной публикации",
                    signals=["near_duplicate"],
                    details={"similarity": round(score, 3), "compared": checked},
                )

        if best >= threshold - 0.1:
            return FilterResult.suspect(
                self.name,
                "текст частично совпадает с известной публикацией",
                signals=["partial_similarity"],
                details={"similarity": round(best, 3), "compared": checked},
            )
        return FilterResult.passthrough(self.name)

    @staticmethod
    def prepare_context_payload(posts: Iterable[NormalizedPost]) -> list[str]:
        """Готовит список текстов для контекста дедупликации."""
        return [clean_text(post.text) for post in posts if post.text]
