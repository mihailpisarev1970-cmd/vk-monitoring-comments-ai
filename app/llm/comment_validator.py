"""Проверка сгенерированного комментария (ТЗ п.13.2).

После генерации проверяется:
* длина (200–300 символов);
* ответ не пустой;
* отсутствие рекламного содержания;
* отсутствие бессодержательных общих фраз;
* соответствие содержанию исходной публикации;
* отсутствие неподтверждённых утверждений (обезличенность, без выдуманных цифр);
* отсутствие служебных строк («Подсчёт символов: …», «Комментарий: …»).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.filters.loader import SignalSettings, WordList
from app.llm.schemas import POST_END, POST_START
from app.vk.normalizer import clean_text

#: Универсальные фразы, запрещённые ТЗ п.13.1
TEMPLATE_PHRASES = (
    "очень интересная тема",
    "интересная тема, спасибо",
    "полностью согласен, отличная",
    "отличная статья",
    "спасибо, было полезно",
    "было очень полезно",
    "спасибо за публикацию",
    "отличный пост, спасибо",
    "интересная мысль, спасибо",
    "спасибо, что поделились",
    "очень полезная информация",
    "автор молодец",
    "продолжай в том же духе",
    "не могу не согласиться",
)

#: Признаки упоминания генерации ИИ (ТЗ п.13.1)
AI_MENTIONS = (
    "как ии",
    "как нейросеть",
    "искусственный интеллект",
    "нейросет",
    "сгенерирован",
    "языковая модель",
    "chatgpt",
    "чат-бот написал",
)

#: Личный опыт, которого не было в публикации (ТЗ п.13.1)
FIRST_PERSON_EXPERIENCE = (
    "я тоже так делал",
    "у меня было так же",
    "я сталкивался с этим",
    "я так делаю",
    "в моём проекте",
    "у меня в проекте",
    "я использовал этот",
)

#: Служебные строки, которые модель иногда добавляет (удаляются только если
#: состоят из метки и короткого пояснения — защита от удаления основного текста)
SERVICE_LINE_RE = re.compile(
    r"(?i)^\s*(комментарий|ответ|вариант|подсч[её]т символов|символов|итог)\s*:"
    r"[^\n]{0,60}$",
    re.MULTILINE,
)
#: Цитаты и markdown-обёртки (только внешние кавычки)
QUOTE_RE = re.compile(r'^["«\']\s*|\s*["»\']$')
MARKDOWN_RE = re.compile(r"(\*\*|__|`{1,3})")
#: Ссылки
LINK_RE = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+", re.IGNORECASE)
#: Эмодзи (базовые диапазоны)
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]")
#: Числа — для проверки выдуманных фактов
NUMBER_RE = re.compile(r"\d+[\d\s.,]*")


def normalize_comment(raw: str) -> str:
    """Очищает ответ модели от служебных строк, кавычек и markdown."""
    text = (raw or "").strip()
    if not text:
        return ""
    text = SERVICE_LINE_RE.sub("", text)
    text = MARKDOWN_RE.sub("", text)
    text = text.replace(POST_START, "").replace(POST_END, "")
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = QUOTE_RE.sub("", text).strip()
    return text


def _stems(words: list[str] | set[str]) -> set[str]:
    """Грубая основа слова для сравнения падежных форм.

    Полноценная морфология здесь избыточна: достаточно отбросить 1–2
    последних символа, чтобы «роботах», «роботов» и «роботы» совпали.
    """
    result: set[str] = set()
    for word in words:
        if len(word) >= 7:
            result.add(word[:-2])
        elif len(word) >= 5:
            result.add(word[:-1])
    return result


class CommentValidator:
    """Проверяет комментарий по требованиям ТЗ п.13.1–13.2."""

    def __init__(
        self,
        *,
        min_length: int = 200,
        max_length: int = 300,
        max_emoji: int = 1,
        max_template_phrases: int = 0,
        commercial_settings: SignalSettings | None = None,
        commercial_words: dict[str, WordList] | None = None,
    ) -> None:
        self.min_length = min_length
        self.max_length = max_length
        self.max_emoji = max_emoji
        self.max_template_phrases = max_template_phrases
        self.commercial_settings = commercial_settings or SignalSettings()
        self.commercial_words = commercial_words or {}

    def validate(self, comment: str, post_text: str = "") -> CommentValidation:
        """Полная проверка комментария."""
        text = normalize_comment(comment)
        checks: list[CommentCheck] = [self._check_not_empty(text)]
        if text:
            checks.extend(
                [
                    self._check_length(text),
                    self._check_templates(text),
                    self._check_ai_mentions(text),
                    self._check_links(text),
                    self._check_emoji(text),
                    self._check_commercial(text),
                    self._check_no_copy(text, post_text),
                    self._check_no_personal_experience(text, post_text),
                    self._check_invented_numbers(text, post_text),
                    self._check_relevance(text, post_text),
                ]
            )
        return CommentValidation(valid=all(c.passed for c in checks), checks=checks)

    # ------------------------------------------------------------------
    @staticmethod
    def _check_not_empty(text: str) -> CommentCheck:
        if not text:
            return CommentCheck("not_empty", False, "комментарий пустой")
        return CommentCheck("not_empty", True)

    def _check_length(self, text: str) -> CommentCheck:
        length = len(text)
        if length < self.min_length:
            return CommentCheck(
                "length",
                False,
                f"комментарий слишком короткий: {length} символов, нужно от {self.min_length}",
            )
        if length > self.max_length:
            return CommentCheck(
                "length",
                False,
                f"комментарий слишком длинный: {length} символов, нужно до {self.max_length}",
            )
        return CommentCheck("length", True, f"{length} символов")

    def _check_templates(self, text: str) -> CommentCheck:
        lowered = text.lower()
        found = [phrase for phrase in TEMPLATE_PHRASES if phrase in lowered]
        if len(found) > self.max_template_phrases:
            return CommentCheck(
                "templates",
                False,
                f"шаблонные фразы: «{found[0]}» — нужна конкретная мысль по теме",
            )
        return CommentCheck("templates", True)

    @staticmethod
    def _check_ai_mentions(text: str) -> CommentCheck:
        lowered = text.lower()
        found = [marker for marker in AI_MENTIONS if marker in lowered]
        if found:
            return CommentCheck("ai_mentions", False, f"упоминание генерации ИИ: «{found[0]}»")
        return CommentCheck("ai_mentions", True)

    @staticmethod
    def _check_links(text: str) -> CommentCheck:
        if LINK_RE.search(text):
            return CommentCheck("links", False, "ссылки в комментарии не допускаются")
        return CommentCheck("links", True)

    def _check_emoji(self, text: str) -> CommentCheck:
        count = len(EMOJI_RE.findall(text))
        if count > self.max_emoji:
            return CommentCheck("emoji", False, f"слишком много эмодзи: {count}")
        return CommentCheck("emoji", True)

    def _check_commercial(self, text: str) -> CommentCheck:
        hits: list[str] = []
        for word_list in self.commercial_words.values():
            matched = word_list.matches(text)
            if matched:
                hits.append(matched[0])
        if hits:
            return CommentCheck("commercial", False, f"рекламное содержание: «{hits[0]}»")
        return CommentCheck("commercial", True)

    @staticmethod
    def _check_no_copy(text: str, post_text: str) -> CommentCheck:
        """Комментарий не должен дословно повторять публикацию (ТЗ п.13.1)."""
        source = clean_text(post_text or "")
        if len(source) < 60:
            return CommentCheck("no_copy", True)
        comment_words = text.lower().split()
        source_lower = source.lower()
        window = 8
        for index in range(len(comment_words) - window + 1):
            fragment = " ".join(comment_words[index : index + window])
            if fragment in source_lower:
                return CommentCheck(
                    "no_copy", False, "комментарий дословно повторяет текст публикации"
                )
        return CommentCheck("no_copy", True)

    @staticmethod
    def _check_no_personal_experience(text: str, post_text: str) -> CommentCheck:
        """Запрет на выдуманный личный опыт (ТЗ п.13.1)."""
        lowered = text.lower()
        found = [marker for marker in FIRST_PERSON_EXPERIENCE if marker in lowered]
        if not found:
            return CommentCheck("no_experience", True)
        source = (post_text or "").lower()
        if found[0] in source:
            return CommentCheck("no_experience", True)
        return CommentCheck(
            "no_experience",
            False,
            f"утверждение о личном опыте вне контекста: «{found[0]}»",
        )

    @staticmethod
    def _check_invented_numbers(text: str, post_text: str) -> CommentCheck:
        """Числа не должны противоречить публикации (защита от выдуманных фактов)."""
        numbers = [n.strip() for n in NUMBER_RE.findall(text) if n.strip()]
        if not numbers:
            return CommentCheck("no_invented_numbers", True)
        source = post_text or ""
        invented = [number for number in numbers if number not in source]
        if invented:
            return CommentCheck(
                "no_invented_numbers",
                False,
                f"числа, которых нет в публикации: {', '.join(invented[:3])}",
            )
        return CommentCheck("no_invented_numbers", True)

    @staticmethod
    def _check_relevance(text: str, post_text: str) -> CommentCheck:
        """Комментарий должен пересекаться с публикацией по значимым словам.

        Слова сравниваются по основе (без последних 1–2 символов), чтобы
        падежные формы считались совпадением: иначе естественные
        комментарии вида «роботах» / «роботов» не засчитываются, и
        генерация уходит в лишние регенерации.
        """
        source_words = _stems(re.findall(r"[а-яёa-z]{5,}", (post_text or "").lower()))
        comment_words = _stems(re.findall(r"[а-яёa-z]{5,}", text.lower()))
        if not source_words:
            return CommentCheck("relevance", True)
        overlap = source_words & comment_words
        if len(overlap) < 2:
            return CommentCheck(
                "relevance", False, "комментарий не связан с содержанием публикации"
            )
        return CommentCheck("relevance", True)



@dataclass(slots=True)
class CommentCheck:
    """Результат проверки одного аспекта."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class CommentValidation:
    """Итоговый результат проверки комментария."""

    valid: bool
    checks: list[CommentCheck] = field(default_factory=list)

    @property
    def failed(self) -> list[CommentCheck]:
        return [check for check in self.checks if not check.passed]

    @property
    def reasons(self) -> list[str]:
        return [f"{check.name}: {check.detail}" for check in self.failed]

    def instructions(self) -> str:
        """Дополнительная инструкция для повторной генерации (ТЗ п.13.2)."""
        if self.valid:
            return ""
        lines = ["Предыдущий ответ не прошёл проверку:"]
        for check in self.failed:
            lines.append(f"- {check.detail}")
        lines.append("Перепиши комментарий с учётом этих замечаний.")
        return "\n".join(lines)
