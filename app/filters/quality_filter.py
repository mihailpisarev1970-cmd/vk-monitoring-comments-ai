"""Фильтр низкокачественного контента и случайных совпадений (ТЗ п.11.1, п.11.3).

Исключает:
* публикации **без текстовой информации** — только медиа, эмодзи, ссылки,
  хэштеги (требование заказчика);
* публикации короче **двух предложений** (требование заказчика);
* бессмысленные тексты и наборы случайных слов/хэштегов;
* публикации из одних ссылок без пояснений;
* кликбейт (опционально, ``clickbait_reject``);
* случайные совпадения с поисковыми маркерами без тематической связи.

Не исключает автоматически (ТЗ п.11.3): эмодзи, несколько хэштегов, ссылку на
GitHub, техническую ссылку, использование ИИ, небольшой вопрос по теме —
если он содержит не меньше двух предложений.
"""

from __future__ import annotations

import re

from app.filters.base import FilterContext, FilterResult
from app.filters.loader import QualitySettings, WordList
from app.vk.models import NormalizedPost
from app.vk.normalizer import clean_text

_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)
_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+", re.UNICODE)
#: Эмодзи и прочие пиктограммы — не являются текстовой информацией
_EMOJI_RE = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002600-\U000027bf" "\U0001f000-\U0001f2ff"
    "\U00002190-\U000021ff" "\u2b00-\u2bff" "\ufe0f\u200d" "]+",
    re.UNICODE,
)
#: Границы предложений: точка/!/?/… + пробел, либо перевод строки.
#: Точка внутри сокращений («т.д.», «т.е.») и в доменах не считается концом.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+", re.UNICODE)
_ABBREVIATION_RE = re.compile(
    r"\b(?:т\.\s?[едк]\.|т\.к\.|др\.|пр\.|см\.|напр\.|рис\.|стр\.|г\.|гг\.|в\.|вв\.|[a-zA-Z]\.)",
    re.IGNORECASE,
)

#: Домены, которые не являются рекламными ссылками
_TECHNICAL_DOMAINS = (
    "github.com", "gitlab.com", "stackoverflow.com", "docs.python.org",
    "pypi.org", "habr.com", "medium.com", "youtube.com", "vk.com", "t.me",
)


def count_sentences(text: str) -> int:
    """Считает содержательные предложения в тексте.

    Предложением считается фрагмент, в котором есть хотя бы два слова:
    это отсекает «предложения» из одного восклицания, эмодзи или ссылки.
    Сокращения («т.е.», «др.») и инициалы на границы не влияют.
    """
    if not text:
        return 0
    protected = _ABBREVIATION_RE.sub(lambda m: m.group(0).replace(".", "\u2024"), text)
    total = 0
    for chunk in _SENTENCE_SPLIT_RE.split(protected):
        chunk = chunk.strip()
        if not chunk:
            continue
        if len(_WORD_RE.findall(_EMOJI_RE.sub(" ", chunk))) >= 2:
            total += 1
    return total


def text_information_length(text: str) -> int:
    """Длина текста за вычетом ссылок, хэштегов, эмодзи и пунктуации.

    Показывает, сколько реальной словесной информации несёт публикация.
    """
    if not text:
        return 0
    cleaned = _LINK_RE.sub(" ", text)
    cleaned = _HASHTAG_RE.sub(" ", cleaned)
    cleaned = _EMOJI_RE.sub(" ", cleaned)
    words = _WORD_RE.findall(cleaned)
    return len(" ".join(words))


def _sentence_word(count: int) -> str:
    """Склонение слова «предложение» для сообщения об отказе."""
    if count % 10 == 1 and count % 100 != 11:
        return "е"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "я"
    return "й"


class QualityFilter:
    """Качество текста и тематическая связь."""

    name = "quality"

    def __init__(
        self,
        settings_: QualitySettings,
        topics: WordList | None = None,
        clickbait: WordList | None = None,
        meaningless: WordList | None = None,
        off_topic: WordList | None = None,
    ) -> None:
        self.settings = settings_
        self.enabled = True
        self.topics = topics or WordList(category="topics")
        self.clickbait = clickbait or WordList(category="quality.clickbait")
        self.meaningless = meaningless or WordList(category="quality.meaningless")
        self.off_topic = off_topic or WordList(category="quality.off_topic")


    def check(self, post: NormalizedPost, context: FilterContext | None = None) -> FilterResult:
        text = clean_text(post.text)
        stripped = text.strip()

        # --- Нет текстовой информации (только медиа/эмодзи/ссылки/хэштеги) ---
        if self.settings.reject_non_text:
            info_length = text_information_length(stripped)
            has_media = bool(post.raw_payload.get("attachments"))
            if info_length < self.settings.min_text_length:
                signals = ["no_text_info"]
                if has_media:
                    signals.append("media_only")
                return FilterResult.reject(
                    self.name,
                    "публикация без текстовой информации "
                    f"(информативных символов: {info_length})",
                    signals=signals,
                    details={
                        "info_length": info_length,
                        "attachments": len(post.raw_payload.get("attachments") or []),
                    },
                )

        # --- Один абзац/предложение и меньше ---
        if self.settings.min_sentences > 0:
            sentences = count_sentences(stripped)
            if sentences < self.settings.min_sentences:
                return FilterResult.reject(
                    self.name,
                    f"слишком короткая публикация: {sentences} предложени"
                    f"{_sentence_word(sentences)} (нужно "
                    f"{self.settings.min_sentences})",
                    signals=["too_few_sentences"],
                    details={"sentences": sentences},
                )

        # --- Пустой или бессмысленный текст ---
        if len(stripped) < self.settings.min_text_length:
            words = _WORD_RE.findall(stripped)
            return FilterResult.reject(
                self.name,
                "слишком короткий текст",
                signals=["too_short"],
                details={"length": len(stripped), "words": len(words)},
            )

        if self.meaningless.matches(stripped):
            return FilterResult.reject(self.name, "бессмысленный текст", signals=["meaningless"])

        links = _LINK_RE.findall(stripped)
        hashtags = _HASHTAG_RE.findall(stripped)
        words = _WORD_RE.findall(stripped)
        content_length = max(1, len(stripped))

        # --- Текст из одних хэштегов ---
        hashtag_chars = sum(len(tag) for tag in hashtags)
        if hashtags and hashtag_chars / content_length > self.settings.max_hashtag_ratio:
            return FilterResult.reject(
                self.name,
                "публикация состоит преимущественно из хэштегов",
                signals=["hashtags_only"],
                details={"ratio": round(hashtag_chars / content_length, 2)},
            )

        # --- Текст из одних ссылок ---
        technical = [link for link in links if any(d in link for d in _TECHNICAL_DOMAINS)]
        non_technical = len(links) - len(technical)
        link_chars = sum(len(link) for link in links)
        words_without_links = len(_WORD_RE.findall(_LINK_RE.sub(" ", stripped)))
        if non_technical and (
            link_chars / content_length > self.settings.max_link_ratio
            or words_without_links < self.settings.min_words
        ):
            return FilterResult.reject(
                self.name,
                "публикация состоит преимущественно из ссылок без пояснений",
                signals=["links_only"],
                details={"links": len(links), "words": words_without_links},
            )

        # --- Слишком мало слов ---
        if len(words) < self.settings.min_words and not stripped.endswith("?"):
            return FilterResult.suspect(
                self.name,
                "слишком мало содержательного текста",
                signals=["few_words"],
                details={"words": len(words)},
            )

        # --- Кликбейт ---
        clickbait_hits = self.clickbait.matches(stripped)
        if clickbait_hits:
            if self.settings.clickbait_reject:
                return FilterResult.reject(
                    self.name,
                    "кликбейт",
                    signals=[f"clickbait:{h}" for h in clickbait_hits[:2]],
                )
            return FilterResult.suspect(
                self.name,
                "признаки кликбейта",
                signals=[f"clickbait:{h}" for h in clickbait_hits[:2]],
            )

        # --- Тематическая связь (ТЗ п.11.1, п.11.4) ---
        if self.settings.require_topic_link:
            topic_hits = self.topics.matches(stripped)
            off_topic_hits = self.off_topic.matches(stripped)
            if len(topic_hits) < self.settings.topic_terms_min_matches:
                return FilterResult.reject(
                    self.name,
                    "нет тематической связи с проектом (случайное совпадение маркера)",
                    signals=["no_topic_link"] + [f"off_topic:{h}" for h in off_topic_hits[:2]],
                    details={"topic_matches": len(topic_hits)},
                )
            if off_topic_hits and len(topic_hits) <= 1:
                return FilterResult.suspect(
                    self.name,
                    "возможное случайное совпадение маркера",
                    signals=[f"off_topic:{h}" for h in off_topic_hits[:2]],
                    details={"topic_matches": len(topic_hits)},
                )
            # Тематическая словоформа вроде «питон» может означать животное
            # («королевский питон»), поэтому слабую связь помечаем как suspect
            if len(topic_hits) == 1 and self._single_topic_is_ambiguous(stripped, topic_hits[0]):
                return FilterResult.suspect(
                    self.name,
                    "тематический термин найден в постороннем контексте",
                    signals=["ambiguous_topic"],
                    details={"topic_matches": 1},
                )

        return FilterResult.passthrough(self.name)

    @staticmethod
    def _single_topic_is_ambiguous(text: str, topic_pattern: str) -> bool:
        """Проверяет сомнительные совпадения (например, питон-рептилия)."""
        lowered = text.lower()
        ambiguous_terms = ("питон", "королевск", "террариум", "рептил", "окрас")
        return "питон" in topic_pattern.lower() and any(t in lowered for t in ambiguous_terms[1:])
