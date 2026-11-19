"""Тесты фильтров на примерах из ТЗ (разделы 9, 10, 11)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db.enums import AdFlag
from app.filters.base import FilterContext, Verdict
from app.filters.loader import load_config
from app.filters.pipeline import FilterPipeline, PipelineOutcome
from app.vk.models import NormalizedPost


def make_post(text: str, *, ads: AdFlag = AdFlag.NO, owner_id: int = -500) -> NormalizedPost:
    post = NormalizedPost(
        owner_id=owner_id,
        post_id=1,
        text=text,
        published_at=datetime.now(UTC),
        post_url=f"https://vk.com/wall{owner_id}_1",
        marked_as_ads=ads is AdFlag.YES,
        ad_flag=ads,
    )
    return post


@pytest.fixture(scope="module")
def pipeline() -> FilterPipeline:
    return FilterPipeline(load_config(force=True))


# ---------------------------------------------------------------- ТЗ п.8
def test_official_ad_rejected(pipeline: FilterPipeline) -> None:
    result = pipeline.run(make_post("Официальный рекламный пост про Python", ads=AdFlag.YES))
    assert result.outcome is PipelineOutcome.REJECTED
    assert result.status.value == "rejected_ad"


def test_ad_filter_cannot_be_disabled(pipeline: FilterPipeline) -> None:
    assert pipeline.is_enabled("ad") is True


def test_unknown_ad_flag_is_suspect(pipeline: FilterPipeline) -> None:
    result = pipeline.run(
        make_post(
            "Пишу на Python, разбираюсь с asyncio и middleware. "
            "Сейчас копаю, как устроен event loop внутри.",
            ads=AdFlag.UNKNOWN,
        )
    )
    assert not result.rejected, result.reason
    assert "ad_flag_unknown" in result.suspicion_signals


# ---------------------------------------------------------------- ТЗ п.10
@pytest.mark.parametrize(
    "text",
    [
        "Набираю группу на курс Python с нуля. Запись открыта, старт 1 декабря.",
        "Открыт набор на интенсив по созданию чат-ботов. Стоимость 30 000 рублей.",
        "Запускаю наставничество для начинающих программистов. 8 недель, разбор кода, оплата 50000.",
        "Приходите на бесплатный вебинар, где я расскажу о своей программе обучения за 50 000 рублей.",
        "Онлайн-школа программирования: новый поток курса по Python, запись открыта.",
    ],
)
def test_course_ads_rejected(pipeline: FilterPipeline, text: str) -> None:
    result = pipeline.run(make_post(text))
    assert result.outcome is PipelineOutcome.REJECTED, result.reason
    assert "course" in result.reason


@pytest.mark.parametrize(
    "text",
    [
        "Прошёл курс по Python и наконец понял, как работают декораторы. "
        "Сделал небольшую библиотеку для кэширования своих запросов в asyncio.",
        "Учусь на курсе по программированию, сегодня разбирали ООП и наследование в Python. "
        "Преподаватель объясняет понятно, но домашки много.",
        "Подскажите, с какого курса по Python лучше начать, если уже знаю основы "
        "и хочу разобраться с asyncio и типизацией? "
        "Интересуют курсы с практикой, а не только теорией.",
    ],
)
def test_personal_learning_not_rejected(pipeline: FilterPipeline, text: str) -> None:
    result = pipeline.run(make_post(text))
    assert not result.rejected, result.reason


# ---------------------------------------------------------------- ТЗ п.9
@pytest.mark.parametrize(
    "text",
    [
        "Разрабатываем чат-ботов для бизнеса под ключ. Пишите в личные сообщения.",
        "Автоматизируем отдел продаж с помощью ИИ. Поможем увеличить прибыль. Оставляйте заявку.",
        "Наша команда создаёт сайты и приложения. Есть свободные места на разработку в этом месяце.",
        "Предлагаю услуги Python-разработчика. Пишите в личку, обсудим ваш проект.",
        "Настроим CRM и воронку продаж для вашего бизнеса, внедрение под ключ, пишите в личные сообщения.",
    ],
)
def test_commercial_offers_rejected(pipeline: FilterPipeline, text: str) -> None:
    result = pipeline.run(make_post(text))
    assert result.outcome is PipelineOutcome.REJECTED, result.reason
    assert "commercial" in result.reason



@pytest.mark.parametrize(
    "text",
    [
        "Написал небольшого бота на Python для учёта расходов: aiogram + SQLite. "
        "Пришлось добавить идемпотентность обработки callback-запросов, вынес проверку "
        "в middleware по update_id — иначе при двойном клике создавались дубликаты.",
        "Мой пет-проект на FastAPI: собрал API для заметок, разбираюсь с asyncpg и Alembic. "
        "Пока без коммерческой цели, просто учусь писать бэкенд.",
    ],
)
def test_own_projects_not_rejected(pipeline: FilterPipeline, text: str) -> None:
    """Содержательные публикации о своих проектах не реклама (ТЗ п.9)."""
    result = pipeline.run(make_post(text))
    assert not result.rejected, result.reason



# ---------------------------------------------------------------- ТЗ п.11
@pytest.mark.parametrize(
    "text",
    [
        "Напиши «+» в комментариях, если хочешь разбор по Python. Ставь лайк и делай репост!",
        "Отметь трёх друзей в комментариях и получи гайд по программированию бесплатно!",
        "Ставь плюс, если интересно продолжение про asyncio.",
    ],
)
def test_engagement_bait_rejected(pipeline: FilterPipeline, text: str) -> None:
    result = pipeline.run(make_post(text))
    assert result.outcome is PipelineOutcome.REJECTED, result.reason
    assert "spam" in result.reason


@pytest.mark.parametrize(
    "text",
    [
        "Наш бот приносит 15% в неделю на автоинвестировании. Первым 100 участникам бонус. "
        "Пиши в личку, реферальная ссылка внутри.",
        "Пассивный доход с гарантированной прибылью, инвестиции без риска. "
        "Пригласи двух партнёров и получай доход с команды — сетевой маркетинг нового уровня.",
    ],
)
def test_financial_schemes_rejected(pipeline: FilterPipeline, text: str) -> None:
    result = pipeline.run(make_post(text))
    assert result.outcome is PipelineOutcome.REJECTED, result.reason


def test_hashtags_only_rejected(pipeline: FilterPipeline) -> None:
    result = pipeline.run(make_post("#курс #python #обучение #набор #скидка #промокод #it #боты"))
    assert result.outcome is PipelineOutcome.REJECTED
    assert "quality" in result.reason


def test_links_only_rejected(pipeline: FilterPipeline) -> None:
    result = pipeline.run(make_post("https://example.com/sale https://promo-site.top/page"))
    assert result.outcome is PipelineOutcome.REJECTED



def test_no_topic_link_rejected(pipeline: FilterPipeline) -> None:
    """Случайное совпадение маркера без тематической связи (ТЗ п.11.1)."""
    result = pipeline.run(
        make_post(
            "НОВИНКИ WOLFORD: зимний принт, деликатное мерцание металлизированных нитей. "
            "Смотрите каталог коллекции в нашем салоне в центре города."
        )
    )
    assert result.outcome is PipelineOutcome.REJECTED
    assert "quality" in result.reason


# ---------------------------------------------------------------- ТЗ п.11.3
def test_short_question_with_emoji_and_hashtags_passes(pipeline: FilterPipeline) -> None:
    """Эмодзи, хэштеги и небольшой вопрос — не спам (ТЗ п.11.3)."""
    text = (
        "🚀 Подскажите, какую библиотеку Python лучше использовать для создания "
        "Telegram-бота? Смотрю aiogram и pyTelegramBotAPI. #python #боты"
    )
    result = pipeline.run(make_post(text))
    assert not result.rejected, result.reason


def test_github_link_with_explanation_passes(pipeline: FilterPipeline) -> None:
    text = (
        "Выложил свою библиотеку для кэширования запросов в asyncio, код на Python открыт: "
        "https://github.com/example/async-cache. Внутри есть тесты и пример с FastAPI."
    )
    result = pipeline.run(make_post(text))
    assert not result.rejected, result.reason


def test_duplicate_rejected(pipeline: FilterPipeline) -> None:
    """Точный дубль ранее обработанной публикации (ТЗ п.11.4)."""
    text = (
        "Разбираю, как устроен asyncio в Python: цикл событий, корутины и задачи. "
        "Внутри статьи подробный разбор исходников модуля и примеры кода."
    )
    first = pipeline.run(make_post(text, owner_id=-601))
    assert not first.rejected, first.reason

    context = FilterContext(seen_texts=[text])
    second = pipeline.run(make_post(text, owner_id=-602), context)
    assert second.outcome is PipelineOutcome.REJECTED
    assert "duplicate" in second.reason


def test_pipeline_result_serializable(pipeline: FilterPipeline) -> None:
    result = pipeline.run(make_post("Короткий пост про Python и asyncio"))
    data = result.to_dict()
    assert data["outcome"] in {"passed", "suspect", "rejected"}
    assert "filters" in data


def test_filter_exception_does_not_pass_post(pipeline: FilterPipeline) -> None:
    """Внутренняя ошибка фильтра переводит публикацию в SUSPECT, а не в PASS."""

    class Broken:
        name = "broken"

        def check(self, post, context):  # noqa: ANN001
            raise RuntimeError("boom")

    result = pipeline._safe_check(Broken(), make_post("текст"), FilterContext())
    assert result.verdict is Verdict.SUSPECT
    assert "broken:error" in result.signals
