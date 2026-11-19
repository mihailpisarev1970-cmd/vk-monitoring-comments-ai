"""Отказоустойчивость цикла: промежуточные точки сохранения и зависшие запуски.

Проверяет поведение, обнаруженное при реальном сбое: цикл шёл 20+ минут,
процесс был убит — и вся работа (включая оплаченные LLM-запросы) пропала,
потому что commit выполнялся только в самом конце.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.enums import RunStatus
from app.db.models import MonitoringRun, Post
from app.scheduler.monitoring import _save_progress, reap_stale_runs
from app.scheduler.runs import RunCounters

pytestmark = pytest.mark.asyncio


async def test_save_progress_writes_counters(db_session) -> None:
    """Промежуточное сохранение переносит счётчики в monitoring_runs."""
    run = MonitoringRun(trigger="test", status=RunStatus.RUNNING, started_at=datetime.now(UTC))
    db_session.add(run)
    await db_session.flush()

    counters = RunCounters(
        posts_found=42,
        posts_new=21,
        posts_analyzed=7,
        posts_approved=3,
        posts_rejected=4,
        notifications_queued=2,
    )
    await _save_progress(db_session, run, counters)

    assert run.posts_found == 42
    assert run.posts_new == 21
    assert run.posts_analyzed == 7
    assert run.posts_approved == 3
    assert run.details is not None
    assert run.details["posts_analyzed"] == 7


async def test_progress_survives_abort_but_requires_commit(db_session) -> None:
    """Сохранённый прогресс не откатывается вместе с незавершённым циклом."""
    run = MonitoringRun(trigger="test", status=RunStatus.RUNNING, started_at=datetime.now(UTC))
    db_session.add(run)
    await db_session.flush()
    run_id = run.id

    await _save_progress(db_session, run, RunCounters(posts_analyzed=5))
    # Имитация обрыва: следующий этап падает, но данные прогресса уже сохранены
    await db_session.rollback()

    restored = await db_session.get(MonitoringRun, run_id)
    assert restored is not None
    assert restored.posts_analyzed == 5
    assert restored.status == RunStatus.RUNNING


async def test_reap_stale_runs_marks_old_running_as_failed(db_session) -> None:
    """Зависший запуск помечается failed с описанием причины."""
    stale = MonitoringRun(
        trigger="test",
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC) - timedelta(hours=5),
    )
    fresh = MonitoringRun(
        trigger="test",
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db_session.add_all([stale, fresh])
    await db_session.flush()

    reaped = await reap_stale_runs(db_session, older_than_hours=3)

    assert reaped == 1
    assert stale.status == RunStatus.FAILED
    assert stale.finished_at is not None
    assert stale.errors and stale.errors[0]["stage"] == "cycle"
    # Свежий запуск не трогаем: он может выполняться прямо сейчас
    assert fresh.status == RunStatus.RUNNING


async def test_reap_stale_runs_keeps_finished_runs(db_session) -> None:
    """Завершённые запуски не переписываются."""
    done = MonitoringRun(
        trigger="test",
        status=RunStatus.SUCCESS,
        started_at=datetime.now(UTC) - timedelta(hours=6),
        finished_at=datetime.now(UTC) - timedelta(hours=5),
    )
    db_session.add(done)
    await db_session.flush()

    assert await reap_stale_runs(db_session, older_than_hours=3) == 0
    assert done.status == RunStatus.SUCCESS


async def test_pending_new_returns_unprocessed_posts(db_session) -> None:
    """Публикации, не дошедшие до LLM в прошлом цикле, берутся снова."""
    from app.scheduler.monitoring import MonitoringCycle

    waiting = Post(
        owner_id=-90001,
        post_id=501,
        text="Публикация ждёт LLM-обработки с прошлого цикла.",
        text_hash="hash-waiting",
        published_at=datetime.now(UTC),
        post_url="https://vk.com/wall-90001_501",
        post_type="post",
    )
    db_session.add(waiting)
    await db_session.flush()

    rows = await MonitoringCycle._pending_new(
        db_session, now=datetime.now(UTC), max_age_hours=4
    )
    # В реальной БД может быть больше 500 ожидающих публикаций (LIMIT в запросе),
    # поэтому проверяем порядок выборки, а не попадание конкретной записи.
    assert rows, "очередь не должна быть пустой"
    published = [post.published_at for post in rows]
    assert published == sorted(published), "очередь должна идти от старых к новым"
    if len(rows) < 500:
        assert waiting.id in {post.id for post in rows}


async def test_pending_new_skips_stale_posts(db_session) -> None:
    """Публикации старше ``max_age_hours`` в очередь LLM не попадают."""
    from app.scheduler.monitoring import MonitoringCycle

    stale = Post(
        owner_id=-90004,
        post_id=504,
        text="Публикация опубликована слишком давно для обработки.",
        text_hash="hash-stale",
        published_at=datetime.now(UTC) - timedelta(hours=5),
        post_url="https://vk.com/wall-90004_504",
        post_type="post",
    )
    db_session.add(stale)
    await db_session.flush()

    rows = await MonitoringCycle._pending_new(
        db_session, now=datetime.now(UTC), max_age_hours=4
    )
    assert stale.id not in {post.id for post in rows}


async def test_pending_new_skips_posts_with_failed_analysis(db_session) -> None:
    """Посты с уже записанным анализом не повторяются бесконечно.

    Без этого один «битый» текст тратил бы токены LLM каждый час.
    """
    from app.db.models import PostAnalysis
    from app.scheduler.monitoring import MonitoringCycle

    broken = Post(
        owner_id=-90002,
        post_id=502,
        text="Публикация, на которой LLM вернула некорректный JSON.",
        text_hash="hash-broken",
        published_at=datetime.now(UTC),
        post_url="https://vk.com/wall-90002_502",
        post_type="post",
    )
    db_session.add(broken)
    await db_session.flush()
    db_session.add(
        PostAnalysis(
            post_id=broken.id,
            stage="classify",
            model="test",
            prompt_version="v1",
            reason="некорректный JSON",
            accepted=False,
            rejection_reasons=["llm_error"],
        )
    )
    await db_session.flush()

    rows = await MonitoringCycle._pending_new(
        db_session, now=datetime.now(UTC), max_age_hours=4
    )
    assert broken.id not in {post.id for post in rows}


async def test_pending_new_respects_exclude(db_session) -> None:
    """Уже отфильтрованные в этом цикле публикации не дублируются."""
    from app.scheduler.monitoring import MonitoringCycle

    post = Post(
        owner_id=-90003,
        post_id=503,
        text="Публикация уже прошла фильтры в текущем цикле.",
        text_hash="hash-current",
        published_at=datetime.now(UTC),
        post_url="https://vk.com/wall-90003_503",
        post_type="post",
    )
    db_session.add(post)
    await db_session.flush()

    rows = await MonitoringCycle._pending_new(
        db_session, now=datetime.now(UTC), max_age_hours=4, exclude={post.id}
    )
    assert post.id not in {row.id for row in rows}


@pytest.mark.asyncio(loop_scope="function")
async def test_llm_limit_defaults_and_config() -> None:
    """Суточный лимит LLM: заказчик требует не более 25 публикаций в день."""
    from app.filters.loader import load_config

    config = load_config(force=True)
    assert config.filters.llm.max_posts_per_cycle == 25
    assert config.filters.monitoring.max_age_hours == 4


async def test_reap_stale_runs_closes_orphaned_run_immediately(db_session) -> None:
    """Свежий «осиротевший» прогон закрывается при старте нового цикла.

    Сценарий: процесс убит (например, пересборкой контейнера), запись
    осталась в running. Новый цикл взял Redis-блокировку — значит живых
    конкурентов нет и запись осиротела. Без этого она висела бы в
    истории до истечения «возрастного» порога (3 часа).
    """
    orphaned = MonitoringRun(
        trigger="manual",
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db_session.add(orphaned)
    await db_session.flush()

    reaped = await reap_stale_runs(db_session, older_than_hours=0)

    assert reaped == 1
    assert orphaned.status == RunStatus.FAILED
    assert orphaned.errors
    assert "прерван" in orphaned.errors[0]["error"]
