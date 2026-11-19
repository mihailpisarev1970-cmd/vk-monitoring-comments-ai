"""Тесты новых правил отбора публикаций (требования заказчика).

Проверяются:
* отклонение публикаций без текстовой информации;
* отклонение публикаций из одного предложения и короче;
* отклонение предложений работы/вакансий;
* отклонение рекламных публикаций;
* определение доступности комментариев по разметке страницы.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db.enums import AdFlag
from app.filters.loader import load_config
from app.filters.pipeline import FilterPipeline, PipelineOutcome
from app.filters.quality_filter import count_sentences, text_information_length
from app.vk.comments_check import CommentsAccess, build_post_url, interpret_response
from app.vk.models import NormalizedPost


def make_post(
    text: str,
    *,
    ads: AdFlag = AdFlag.NO,
    owner_id: int = -500,
    attachments: list[dict] | None = None,
) -> NormalizedPost:
    return NormalizedPost(
        owner_id=owner_id,
        post_id=1,
        text=text,
        published_at=datetime.now(UTC),
        post_url=f"https://vk.com/wall{owner_id}_1",
        marked_as_ads=ads is AdFlag.YES,
        ad_flag=ads,
        raw_payload={"attachments": attachments or []},
    )


@pytest.fixture(scope="module")
def pipeline() -> FilterPipeline:
    return FilterPipeline(load_config(force=True))


# ------------------------------------------------------- подсчёт предложений
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Одно предложение про Python и asyncio.", 1),
        ("Первое предложение. Второе предложение.", 2),
        ("Первое предложение тут. Второе предложение тут. Третье предложение тут.", 3),
        ("Строка один с текстом\nСтрока два с текстом", 2),
        ("Python — это язык. Он удобен для скриптов.", 2),
        ("Коротко", 0),
        ("", 0),
        ("!!!", 0),
        ("🤔🤔🤔", 0),
    ],
)
def test_count_sentences(text: str, expected: int) -> None:
    assert count_sentences(text) == expected


def test_abbreviations_do_not_split_sentences() -> None:
    """Точка в сокращениях не создаёт ложных границ предложений."""
    text = "Использую pandas, numpy и т.д. для анализа данных о заказах в Python."
    assert count_sentences(text) == 1


# -------------------------------------------------- информативность текста
@pytest.mark.parametrize(
    "text",
    [
        "",
        "https://example.com/article",
        "#python #asyncio #backend",
        "🔥🔥🔥",
        "https://t.me/channel #promo 🔥",
        "!!! ??? ...",
    ],
)
def test_non_text_posts_have_low_info_length(text: str) -> None:
    assert text_information_length(text) < 40


def test_meaningful_text_has_high_info_length() -> None:
    text = "Разобрал, как устроен event loop в asyncio, и написал заметку об этом."
    assert text_information_length(text) >= 40


# ------------------------------------------------ нет текстовой информации
@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/post",
        "#python #backend",
        "🔥🔥🔥🔥",
        "",
        "Смотрите фото",
    ],
)
def test_post_without_text_info_rejected(pipeline: FilterPipeline, text: str) -> None:
    result = pipeline.run(make_post(text))
    assert result.outcome is PipelineOutcome.REJECTED, result.reason
    assert "quality" in result.reason
    assert "без текстовой информации" in result.reason


def test_media_only_post_rejected_with_signal(pipeline: FilterPipeline) -> None:
    result = pipeline.run(
        make_post(
            "🔥",
            attachments=[{"type": "photo", "photo": {"id": 1}}],
        )
    )
    assert result.outcome is PipelineOutcome.REJECTED, result.reason
    quality = next(r for r in result.all_results if r.filter_name == "quality")
    assert "media_only" in quality.signals


# -------------------------------------------------- слишком короткий пост
@pytest.mark.parametrize(
    "text",
    [
        "Написал скрипт на Python для парсинга выгрузок и он работает.",
        "Сделал небольшой бот на aiogram, разбираюсь с вебхуками теперь.",
        "Python удобен для автоматизации рутины в работе аналитика.",
    ],
)
def test_single_sentence_post_rejected(pipeline: FilterPipeline, text: str) -> None:
    """Пост из одного предложения отклоняется (требование заказчика)."""
    assert count_sentences(text) == 1
    result = pipeline.run(make_post(text))
    assert result.outcome is PipelineOutcome.REJECTED, result.reason
    assert "предложени" in result.reason
    quality = next(r for r in result.all_results if r.filter_name == "quality")
    assert "too_few_sentences" in quality.signals


def test_two_sentence_post_passes_quality(pipeline: FilterPipeline) -> None:
    text = (
        "Написал скрипт на Python для парсинга выгрузок, он работает стабильно. "
        "Дальше хочу добавить логирование и ретраи при сбоях сети."
    )
    assert count_sentences(text) == 2
    result = pipeline.run(make_post(text))
    assert not result.rejected, result.reason


# ------------------------------------------------- предложение работы
@pytest.mark.parametrize(
    "text",
    [
        "Ищем в команду Python-разработчика. Требования к кандидату: опыт от 3 лет, "
        "знание asyncio. Полная занятость, зарплата от 200000 рублей.",
        "Открыта вакансия backend-разработчика на Python. Обязанности: поддержка API "
        "и сервисов. Условия работы: удалённо, оклад 150000.",
        "Ищу разработчика на Python в проект по автоматизации. Нужно написать "
        "несколько скриптов. Резюме присылайте в личные сообщения.",
    ],
)
def test_job_offers_rejected(pipeline: FilterPipeline, text: str) -> None:
    """Публикации с предложением работы не годятся для комментария."""
    result = pipeline.run(make_post(text))
    assert result.outcome is PipelineOutcome.REJECTED, result.reason


def test_single_mention_of_resume_not_rejected(pipeline: FilterPipeline) -> None:
    """Одиночное упоминание слова — не повод для отказа."""
    text = (
        "Переписал резюме под вакансии в IT и заметил, что стало больше откликов. "
        "Самое сложное — описать опыт с Python коротко и по делу."
    )
    result = pipeline.run(make_post(text))
    assert not result.rejected, result.reason


def test_two_job_mentions_not_enough_for_reject(pipeline: FilterPipeline) -> None:
    """Два сигнала работы — ниже порога, публикация проходит."""
    text = (
        "Обновлял резюме и смотрел вакансии, но понял, что хочу сначала "
        "разобраться с asyncio в Python. Много интересных задач в backend."
    )
    result = pipeline.run(make_post(text))
    assert not result.rejected, result.reason


# ------------------------------------------------------ рекламный характер
@pytest.mark.parametrize(
    "text",
    [
        "Скидка 50% на наш курс по Python! Только сегодня, успейте записаться. "
        "Промокод PYTHON50, переходите по ссылке.",
        "Мы запустили новый продукт для автоматизации. Выгодное предложение, "
        "пробный период бесплатно. Оформить заказ на сайте.",
        "Представляем вам уникальную возможность — наш новый сервис для разработчиков. "
        "Гарантируем результат, подписывайтесь на канал.",
    ],
)
def test_advertising_posts_rejected(pipeline: FilterPipeline, text: str) -> None:
    """Явно рекламные публикации отклоняются."""
    result = pipeline.run(make_post(text))
    assert result.outcome is PipelineOutcome.REJECTED, result.reason


# ------------------------------------------------ проверка комментариев
def test_build_post_url() -> None:
    assert build_post_url(-238001933, 6418) == "https://vk.com/wall-238001933_6418"


def test_interpret_can_post_false_means_closed() -> None:
    """can_post=False — автор ограничил комментирование (реальный кейс)."""
    payload = {"count": 0, "can_post": False, "groups_can_post": True, "items": []}
    assert interpret_response(payload) is CommentsAccess.CLOSED


def test_interpret_can_post_true_means_open() -> None:
    payload = {"count": 28, "can_post": True, "groups_can_post": True, "items": []}
    assert interpret_response(payload) is CommentsAccess.OPEN


def test_interpret_missing_can_post_is_unknown() -> None:
    """Изменение формата ответа не должно отклонять публикации."""
    assert interpret_response({"count": 0}) is CommentsAccess.UNKNOWN
    assert interpret_response({}) is CommentsAccess.UNKNOWN
    assert interpret_response(None) is CommentsAccess.UNKNOWN
    assert interpret_response([1, 2]) is CommentsAccess.UNKNOWN


def test_interpret_only_strict_bool_is_trusted() -> None:
    """Значения, похожие на bool, но не bool, трактуются как unknown."""
    assert interpret_response({"can_post": "false"}) is CommentsAccess.UNKNOWN
    assert interpret_response({"can_post": 0}) is CommentsAccess.UNKNOWN
    assert interpret_response({"can_post": 1}) is CommentsAccess.UNKNOWN


def test_unknown_means_not_closed() -> None:
    from app.vk.comments_check import CommentsCheckResult

    result = CommentsCheckResult(access=CommentsAccess.UNKNOWN, post_key="-1_2")
    assert result.closed is False
    assert result.known is False


def test_closed_result_is_closed() -> None:
    from app.vk.comments_check import CommentsCheckResult

    result = CommentsCheckResult(access=CommentsAccess.CLOSED, post_key="-1_2")
    assert result.closed is True
    assert result.known is True


def test_check_many_without_requests_returns_empty() -> None:
    """Пустой список не делает ни одного запроса."""
    import asyncio

    from app.vk.comments_check import check_many

    assert asyncio.run(check_many([])) == {}


def test_comments_settings_defaults() -> None:
    config = load_config(force=True)
    settings_ = config.filters.comments
    assert settings_.enabled is True
    assert settings_.timeout_seconds == 20
    assert settings_.max_checks_per_cycle == 25
    assert config.filters.filters.is_enabled("comments") is True


def test_quality_settings_new_defaults() -> None:
    config = load_config(force=True)
    assert config.filters.quality.reject_non_text is True
    assert config.filters.quality.min_sentences == 2
