"""Полный цикл мониторинга (ТЗ раздел 5).

Один цикл:

1. Redis-блокировка — исключает параллельные запуски (ТЗ п.5.1);
2. загрузка конфигурации (маркеры, фильтры, пороги) с hot-reload;
3. определение окна поиска по watermark;
4. поиск публикаций в ВК по каждому маркеру;
5. сохранение в PostgreSQL с дедупликацией по (owner_id, post_id);
6. технические фильтры → отклонённые не идут в LLM;
7. LLM-классификация → ``post_analysis``;
8. генерация комментария только для принятых;
9. постановка уведомлений в очередь Telegram и отправка;
10. обновление watermark и запись ``monitoring_runs``.

Ошибка отдельного этапа фиксируется, но не роняет цикл целиком.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import PostStatus, RunStatus
from app.db.models import MonitoringRun, Post, PostAnalysis
from app.db.redis_client import build_lock_key, cycle_lock
from app.db.session import session_scope
from app.filters.base import FilterContext
from app.filters.loader import ConfigError, FiltersConfigBundle, load_config
from app.filters.pipeline import FilterPipeline
from app.llm.classifier import PostClassifier
from app.llm.client import LLMClient
from app.llm.comment_generator import CommentGenerator
from app.scheduler import quota
from app.scheduler.cleanup import purge_processed_posts
from app.scheduler.runs import RunCounters, fail_run, finish_run, start_run
from app.scheduler.watermark import (
    compute_window_start,
    effective_watermark,
    load_seen_texts,
    load_watermark,
    save_watermark,
)
from app.telegram.queue import NotificationWorker, enqueue_post
from app.telegram.sender import TelegramSender
from app.vk.client import VKClient
from app.vk.comments_check import check_many
from app.vk.models import MarkerQuery, NormalizedPost, SearchWindow
from app.vk.normalizer import clean_text
from app.vk.repository import save_posts
from app.vk.search import filter_fresh_posts, posts_to_summary, search_markers

logger = get_logger(__name__)

#: Сколько часов истории восстанавливать после долгого простоя
RECOVERY_HOURS = 12

#: Как часто фиксировать промежуточный прогресс цикла в БД
SAVE_EVERY = 5

#: TTL блокировки цикла: продлевается heartbeat-задачей во время работы.
#: Небольшой TTL (15 минут) — страховка от «зависшей» блокировки, если
#: процесс убит без graceful shutdown: следующий цикл не ждёт час, а
#: heartbeat продлевает TTL, пока цикл действительно работает.
LOCK_TTL_SECONDS = 900


@dataclass(slots=True)
class CycleResult:
    """Итог одного цикла."""

    run_id: int | None = None
    status: str = "unknown"
    counters: RunCounters = field(default_factory=RunCounters)
    skipped: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "skipped": self.skipped,
            "reason": self.reason,
            **self.counters.to_dict(),
        }


async def _rollback_quietly(session: AsyncSession) -> None:
    """Откатывает транзакцию, скрывая ошибки отката."""
    try:
        await session.rollback()
    except SQLAlchemyError as exc:  # pragma: no cover - аварийный путь
        logger.warning("Не удалось откатить транзакцию цикла: %s", exc)


async def _save_progress(
    session: AsyncSession,
    run: MonitoringRun,
    counters: RunCounters,
) -> None:
    """Фиксирует промежуточный прогресс цикла.

    Цикл может идти десятки минут (LLM + генерация комментариев). Без
    промежуточных commit'ов обрыв процесса терял всю работу, поэтому
    каждые ``SAVE_EVERY`` публикаций счётчики и статусы сохраняются, а
    запись ``monitoring_runs`` обновляется.
    """
    try:
        run.posts_found = counters.posts_found
        run.posts_new = counters.posts_new
        run.posts_analyzed = counters.posts_analyzed
        run.posts_approved = counters.posts_approved
        run.posts_rejected = counters.posts_rejected
        run.notifications_queued = counters.notifications_queued
        run.queries_total = counters.queries_total
        run.queries_failed = counters.queries_failed
        run.details = counters.to_dict()
        await session.commit()
    except SQLAlchemyError as exc:
        logger.warning("Не удалось сохранить прогресс цикла: %s", exc)
        await _rollback_quietly(session)


async def reap_stale_runs(session: AsyncSession, *, older_than_hours: int = 3) -> int:
    """Помечает зависшие циклы как failed.

    Вызывается при старте сервиса: если процесс был убит посреди цикла,
    запись осталась в статусе ``running`` навсегда — из-за чего история
    запусков и мониторинг перестают быть достоверными.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
    stale = (
        await session.execute(
            select(MonitoringRun).where(
                MonitoringRun.status == RunStatus.RUNNING,
                MonitoringRun.started_at < cutoff,
            )
        )
    ).scalars().all()

    for run in stale:
        run.status = RunStatus.FAILED
        run.finished_at = datetime.now(UTC)
        errors = list(run.errors or [])
        errors.append({"stage": "cycle", "error": "цикл прерван без завершения"})
        run.errors = errors
    if stale:
        await session.flush()
        logger.warning("Помечено прерванных циклов: %s", len(stale))
    return len(stale)


class MonitoringCycle:
    """Оркестратор одного цикла мониторинга."""

    def __init__(
        self,
        *,
        llm_client: LLMClient | None = None,
        telegram_sender: TelegramSender | None = None,
        vk_client: VKClient | None = None,
    ) -> None:
        self._llm = llm_client
        self._sender = telegram_sender
        self._vk = vk_client

    async def run(self, *, trigger: str = "schedule") -> CycleResult:
        """Выполняет полный цикл под распределённой блокировкой."""
        async with cycle_lock(build_lock_key("monitoring"), ttl_seconds=LOCK_TTL_SECONDS) as acquired:
            if not acquired:
                logger.warning("Предыдущий цикл ещё выполняется — запуск пропущен")
                return CycleResult(
                    skipped=True, status="skipped", reason="cycle already running"
                )
            return await self._run_guarded(trigger)

    async def _run_guarded(self, trigger: str) -> CycleResult:
        try:
            bundle = load_config(force=True)
        except ConfigError as exc:
            # ТЗ п.4.1: при некорректной конфигурации поиск НЕ выполняется
            logger.error("Поиск не выполнен: ошибка конфигурации — %s", exc)
            async with session_scope() as session:
                run = await start_run(session, trigger=trigger)
                await fail_run(session, run, f"config error: {exc}")
                return CycleResult(
                    run_id=run.id, status=RunStatus.FAILED.value, reason=str(exc)
                )

        async with session_scope() as session:
            watermark = await load_watermark(session)
            now = datetime.now(UTC)
            monitoring = bundle.filters.monitoring
            window_start = compute_window_start(
                effective_watermark(watermark, now=now),
                now=now,
                window_minutes=monitoring.window_minutes,
                overlap_minutes=monitoring.overlap_minutes,
                recovery_hours=RECOVERY_HOURS,
            )
            window = SearchWindow(start=window_start, end=now)

            # Мы держим Redis-блокировку, значит других живых циклов нет:
            # оставшиеся в running записи — от убитых процессов.
            await reap_stale_runs(session, older_than_hours=0)

            run = await start_run(
                session, trigger=trigger, window_start=window.start, window_end=window.end
            )
            counters = RunCounters()

            # Очистка БД от отработанных публикаций — до сбора текстов
            # дедупликации, чтобы старые записи не попадали в контекст.
            cleanup = await self._cleanup(session, bundle, counters)

            # Фиксируем запись о запуске до начала долгих этапов: если процесс
            # будет убит, в истории останется «зависший» run, который при
            # следующем старте будет помечен как failed (см. reap_stale_runs).
            await session.commit()

            try:
                await self._execute(session, bundle, window, run, counters, cleanup)
            except Exception as exc:  # noqa: BLE001 - цикл не должен падать целиком
                logger.exception("Ошибка цикла мониторинга: %s", exc)
                await _rollback_quietly(session)
                await fail_run(session, run, str(exc), details=counters.to_dict())
                return CycleResult(
                    run_id=run.id, status=RunStatus.FAILED.value, counters=counters
                )

            await finish_run(session, run, counters=counters, details=counters.to_dict())
            if run.status in (RunStatus.SUCCESS, RunStatus.PARTIAL):
                await save_watermark(
                    session,
                    last_success_at=window.end,
                    run_id=run.id,
                    window_end=window.end,
                )
            result = CycleResult(run_id=run.id, status=run.status.value, counters=counters)
            logger.info("Цикл завершён: %s", result.to_dict())
            return result

    async def _search(
        self,
        queries: list[MarkerQuery],
        window: SearchWindow,
        bundle: FiltersConfigBundle,
        counters: RunCounters,
    ):
        """Поиск публикаций в ВК по всем маркерам.

        В базу попадают только свежие публикации: опубликованные не более
        ``monitoring.max_age_hours`` назад (требование заказчика).
        """
        count = bundle.filters.monitoring.count_per_query
        if self._vk is not None:
            result = await search_markers(self._vk, queries, window, count=count)
        else:
            async with VKClient() as client:
                result = await search_markers(client, queries, window, count=count)

        stale_skipped = filter_fresh_posts(
            result,
            now=window.end,
            max_age_hours=bundle.filters.monitoring.max_age_hours,
            window=window,
        )
        summary = posts_to_summary(result)
        counters.queries_total = result.queries_total
        counters.queries_failed = result.queries_failed
        counters.posts_found = len(result.posts)
        counters.posts_stale = stale_skipped
        counters.duplicates_skipped = result.duplicates_skipped
        for error in result.errors:
            counters.errors.append({"stage": "vk_search", **error})
        logger.info("Поиск: %s", {k: v for k, v in summary.items() if k != "errors"})
        return result

    async def _execute(
        self,
        session: AsyncSession,
        bundle: FiltersConfigBundle,
        window: SearchWindow,
        run: MonitoringRun,
        counters: RunCounters,
        cleanup: dict[str, int] | None = None,
    ) -> None:
        """Основные этапы цикла."""
        if cleanup:
            logger.info("Очистка БД: %s", cleanup)

        queries = bundle.queries
        if not queries:
            logger.warning("Нет активных маркеров — поиск пропущен")
            return

        search_result = await self._search(queries, window, bundle, counters)

        new_posts: list[Post] = []
        if search_result.posts:
            new_posts, _known = await save_posts(session, search_result.posts)
            counters.posts_new = len(new_posts)
            await session.flush()
            await _save_progress(session, run, counters)

        approved: list[Post] = []
        if new_posts:
            candidates = await self._apply_filters(
                session, new_posts, bundle, run, counters, search_result.posts
            )
        else:
            candidates = []

        # Публикации, оставшиеся в статусе new с прошлых циклов (LLM-этап
        # ограничен суточным лимитом), берутся из PostgreSQL первыми и
        # обрабатываются по очереди — от старых к новым, но только пока
        # сохраняют свежесть (``monitoring.max_age_hours``).
        carried = await self._pending_new(
            session,
            now=window.end,
            max_age_hours=bundle.filters.monitoring.max_age_hours,
            exclude={post.id for post in candidates},
        )
        if carried:
            logger.info("В очереди с прошлых циклов: %s публикаций", len(carried))
            candidates = [*carried, *candidates]

        if candidates:
            approved = await self._analyze(
                session, candidates, bundle, run, counters, now=window.end
            )

        # Повторно предлагаем в очередь готовые публикации прошлых циклов:
        # уведомления не должны теряться, если предыдущая отправка не удалась
        pending_approved = await self._pending_approved(session)
        to_deliver = {post.id: post for post in [*approved, *pending_approved]}
        if to_deliver:
            deliverable = await self._check_comments(
                session, list(to_deliver.values()), bundle, counters
            )
            if deliverable:
                await self._deliver(session, deliverable, counters)

    @staticmethod
    async def _cleanup(
        session: AsyncSession, bundle: FiltersConfigBundle, counters: RunCounters
    ) -> dict[str, int]:
        """Удаляет из БД отработанные публикации, чтобы база не раздувалась.

        Сбой очистки не должен ронять цикл: это обслуживающая операция.
        """
        settings_ = bundle.filters.db
        if not settings_.enabled:
            return {}
        try:
            stats = await purge_processed_posts(
                session,
                retention_hours=settings_.retention_hours,
                batch_size=settings_.batch_size,
            )
        except Exception as exc:  # noqa: BLE001 - очистка не критична для цикла
            logger.warning("Очистка БД не выполнена: %s", exc)
            await _rollback_quietly(session)
            counters.errors.append({"stage": "db_cleanup", "error": str(exc)})
            return {}

        counters.posts_purged = stats.deleted_posts
        return stats.to_dict()

    async def _check_comments(
        self,
        session: AsyncSession,
        posts: list[Post],
        bundle: FiltersConfigBundle,
        counters: RunCounters,
    ) -> list[Post]:
        """Отбрасывает публикации с закрытыми комментариями.

        Используется ``wall.getComments`` из VK API: поле ``can_post`` в
        ответе равно False, если автор ограничил комментирование (см.
        :mod:`app.vk.comments_check`). Проверяются только одобренные
        публикации — перед постановкой в очередь.

        При недоступности проверки (сеть, лимит API, неожиданный ответ)
        публикация сохраняется: потеря полезного уведомления хуже лишнего
        сообщения.
        """
        settings_ = bundle.filters.comments
        if not settings_.enabled or not posts:
            return posts

        limit = settings_.max_checks_per_cycle
        batch = posts[:limit]
        results = await check_many(
            [(post.owner_id, post.post_id) for post in batch],
            timeout_seconds=settings_.timeout_seconds,
            limit=limit,
            client=self._vk,
        )

        deliverable: list[Post] = list(posts[len(batch):])
        for post in batch:
            result = results.get(post.key)
            if result is None:
                deliverable.append(post)
                continue
            if result.closed:
                post.status = PostStatus.REJECTED_FILTER
                post.status_reason = "comments: комментарии к публикации закрыты"
                counters.posts_rejected += 1
                logger.info("Публикация %s отклонена — комментарии закрыты", post.key)
                continue
            if not result.known:
                counters.errors.append(
                    {
                        "stage": "comments_check",
                        "post": post.key,
                        "error": result.reason or "unknown",
                    }
                )
            deliverable.append(post)

        await session.flush()
        closed = sum(1 for r in results.values() if r.closed)
        logger.info(
            "Проверка комментариев: %s из %s закрыты (проверено %s)",
            closed,
            len(batch),
            len(results),
        )
        return deliverable

    @staticmethod
    async def _pending_new(
        session: AsyncSession,
        *,
        now: datetime,
        max_age_hours: int,
        exclude: set[int] | None = None,
    ) -> list[Post]:
        """Публикации из PostgreSQL, ожидающие LLM, — по одной, по порядку.

        Свежие публикации, сохранённые предыдущими циклами, обрабатываются
        первыми: заказчик требует брать из базы каждую публикацию по
        очереди и проверять её свежесть перед отправкой в LLM.

        Публикации старше ``max_age_hours`` в очередь не попадают: они уже
        не считаются свежими, и тратить на них вызовы LLM нельзя.

        Публикации, у которых уже есть запись анализа (то есть LLM ответила,
        но с ошибкой — некорректный JSON, сбой сети), не повторяются
        бесконечно: иначе один «битый» текст тратил бы токены каждый час.
        """
        analysed = select(PostAnalysis.post_id)
        threshold = now.astimezone(UTC) - timedelta(hours=max_age_hours)
        stmt = (
            select(Post)
            .where(
                Post.status == PostStatus.NEW,
                Post.published_at >= threshold,
                Post.published_at <= now.astimezone(UTC),
                ~Post.id.in_(analysed),
            )
            .order_by(Post.published_at)
            .limit(500)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if exclude:
            rows = [post for post in rows if post.id not in exclude]
        return rows

    @staticmethod
    async def _pending_approved(session: AsyncSession) -> list[Post]:
        """Публикации, готовые к отправке, но ещё не попавшие в очередь."""
        from app.db.models import Notification

        rows = (
            await session.execute(
                select(Post)
                .where(
                    Post.status == PostStatus.APPROVED,
                    ~Post.id.in_(select(Notification.post_id)),
                )
                .order_by(Post.id)
                .limit(20)
            )
        ).scalars().all()
        if rows:
            logger.info("Найдено %s готовых публикаций без уведомления", len(rows))
        return list(rows)

    @staticmethod
    async def _apply_filters(
        session: AsyncSession,
        posts: list[Post],
        bundle: FiltersConfigBundle,
        run: MonitoringRun,
        counters: RunCounters,
        batch: list[NormalizedPost] | None = None,
    ) -> list[Post]:
        """Прогоняет публикации через технические фильтры.

        Контекст дедупликации включает:
        * тексты последних обработанных публикаций из БД (кросс-цикловая защита);
        * остальные публикации текущей партии, включая уже известные записи,
          которые не попали в обработку как новые но являются дублями.
        """
        pipeline = FilterPipeline(bundle)
        history = await load_seen_texts(session)
        context = FilterContext(seen_texts=list(history))

        # Дополняем контекст текстами нынешней выборки из ВК
        for item in batch or []:
            if item.text:
                context.seen_texts.append(clean_text(item.text))

        candidates: list[Post] = []
        for index, post in enumerate(posts, start=1):
            own_text = clean_text(post.text)
            # Убираем собственный текст из контекста, чтобы не «найти себя»
            comparison = [text for text in context.seen_texts if text != own_text]
            current_context = FilterContext(seen_texts=comparison)

            result = pipeline.run(_to_normalized(post), current_context)
            if result.rejected:
                post.status = result.status
                post.status_reason = result.reason
                counters.posts_rejected += 1
            else:
                post.status_reason = result.reason
                candidates.append(post)

            if index % SAVE_EVERY == 0:
                await _save_progress(session, run, counters)

        await session.flush()
        await _save_progress(session, run, counters)
        logger.info(
            "Фильтры: допущено %s из %s (отклонено %s, контекст %s текстов)",
            len(candidates),
            len(posts),
            counters.posts_rejected,
            len(context.seen_texts),
        )
        return candidates

    async def _analyze(
        self,
        session: AsyncSession,
        posts: list[Post],
        bundle: FiltersConfigBundle,
        run: MonitoringRun,
        counters: RunCounters,
        *,
        now: datetime,
    ) -> list[Post]:
        """LLM-классификация и генерация комментариев.

        Перед отправкой в LLM публикация проходит две проверки
        (требование заказчика):

        1. **свежесть** — публикация опубликована не более
           ``monitoring.max_age_hours`` назад; устаревшие не передаются
           в LLM и убираются из очереди;
        2. **суточный лимит** — LLM обрабатывает не более
           ``llm.max_posts_per_cycle`` публикаций в сутки.
        """
        threshold = bundle.filters.llm.confidence_threshold
        max_regen = bundle.filters.comment.max_regeneration_attempts
        approved: list[Post] = []

        monitoring = bundle.filters.monitoring
        posts, skipped_stale = self._drop_stale(posts, now=now, max_age_hours=monitoring.max_age_hours)
        counters.posts_skipped += skipped_stale

        limit = bundle.filters.llm.max_posts_per_cycle
        tz_name = settings.timezone
        used = await quota.daily_used(now, tz_name)
        if used >= limit:
            logger.warning(
                "Суточный лимит LLM исчерпан (%s из %s) — LLM-этап пропущен",
                used,
                limit,
            )
            counters.posts_skipped += len(posts)
            await _save_progress(session, run, counters)
            return approved

        owns_client = self._llm is None
        client = self._llm or LLMClient()
        try:
            classifier = PostClassifier(client, confidence_threshold=threshold)
            generator = CommentGenerator(client, max_attempts=max_regen + 1)

            for index, post in enumerate(posts, start=1):
                # Свежесть проверяется непосредственно перед запросом: цикл
                # может идти долго, а к моменту обработки публикация
                # способна выйти за пределы окна.
                if self._is_stale(post, now=now, max_age_hours=monitoring.max_age_hours):
                    post.status = PostStatus.REJECTED_FILTER
                    post.status_reason = (
                        f"freshness: публикация старше {monitoring.max_age_hours} ч"
                    )
                    counters.posts_skipped += 1
                    continue

                slot = await quota.reserve(now, tz_name, limit)
                if slot == 0:
                    logger.warning(
                        "Суточный лимит LLM (%s) исчерпан — оставшиеся публикации "
                        "будут отложены",
                        limit,
                    )
                    counters.posts_skipped += len(posts) - index + 1
                    break

                outcome = await classifier.classify_and_save(session, post)
                counters.posts_analyzed += 1
                if not outcome.accepted:
                    counters.posts_rejected += 1
                else:
                    comment = await generator.generate_and_save(session, post)
                    if comment.valid:
                        counters.posts_approved += 1
                        approved.append(post)
                    else:
                        counters.posts_rejected += 1
                        counters.errors.append(
                            {
                                "stage": "comment",
                                "post": post.key,
                                "error": comment.error or "invalid comment",
                            }
                        )

                # LLM-этап самый долгий: фиксируем прогресс, чтобы обрыв
                # процесса не откатывал десятки уже оплаченных запросов
                if index % SAVE_EVERY == 0:
                    await _save_progress(session, run, counters)
        finally:
            if owns_client:
                await client.close()

        await _save_progress(session, run, counters)
        logger.info(
            "LLM: проверено %s, одобрено %s (пропущено: %s)",
            counters.posts_analyzed,
            counters.posts_approved,
            counters.posts_skipped,
        )
        return approved

    @staticmethod
    def _is_stale(post: Post, *, now: datetime, max_age_hours: int) -> bool:
        """Публикация старше ``max_age_hours`` (или с датой из будущего)?."""
        published = post.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        published = published.astimezone(UTC)
        now_utc = now.astimezone(UTC)
        return published < now_utc - timedelta(hours=max_age_hours) or published > now_utc

    @classmethod
    def _drop_stale(
        cls, posts: list[Post], *, now: datetime, max_age_hours: int
    ) -> tuple[list[Post], int]:
        """Убирает из очереди устаревшие публикации, не тратя вызовы LLM.

        Публикации остаются в статусе ``new``: они нужны для статистики, но
        повторно в LLM не попадут, пока не станут свежими (чего не будет).
        """
        keep: list[Post] = []
        dropped = 0
        for post in posts:
            if cls._is_stale(post, now=now, max_age_hours=max_age_hours):
                dropped += 1
            else:
                keep.append(post)
        if dropped:
            logger.info(
                "Отложено устаревших публикаций (старше %s ч): %s", max_age_hours, dropped
            )
        return keep, dropped

    async def _deliver(
        self, session: AsyncSession, posts: list[Post], counters: RunCounters
    ) -> None:
        """Ставит уведомления в очередь и отправляет готовые."""
        chat_id = settings.telegram_chat_id
        if not chat_id:
            logger.error("TELEGRAM_CHAT_ID не задан — уведомления не отправлены")
            counters.errors.append({"stage": "telegram", "error": "chat_id is not set"})
            return

        queued = 0
        for post in posts:
            notification = await enqueue_post(session, post, chat_id=chat_id)
            if notification is not None:
                queued += 1
        counters.notifications_queued = queued
        await session.flush()

        owns_sender = self._sender is None
        sender = self._sender or TelegramSender()
        try:
            worker = NotificationWorker(sender)
            stats = await worker.process_due(session)
            counters.notifications_sent = stats.sent
            if stats.errors:
                counters.errors.extend(
                    {"stage": "telegram_delivery", "error": error} for error in stats.errors
                )
        finally:
            if owns_sender:
                await sender.close()


def _to_normalized(post: Post) -> NormalizedPost:
    """ORM-запись → модель нормализованной публикации для фильтров."""
    return NormalizedPost(
        owner_id=post.owner_id,
        post_id=post.post_id,
        author_id=post.author_id,
        author_name=post.author_name,
        text=post.text or "",
        published_at=post.published_at,
        post_url=post.post_url,
        comments_count=post.comments_count,
        likes_count=post.likes_count,
        reposts_count=post.reposts_count,
        views_count=post.views_count,
        marked_as_ads=post.marked_as_ads,
        ad_flag=post.ad_flag,
        post_type=post.post_type,
        text_hash=post.text_hash,
        raw_payload=post.raw_payload or {},
    )
