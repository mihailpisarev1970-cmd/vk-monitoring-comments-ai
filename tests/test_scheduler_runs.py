"""Тесты истории запусков и планировщика (ТЗ п.5.1)."""

from __future__ import annotations

import pytest

from app.db.enums import RunStatus
from app.scheduler.runs import RunCounters, fail_run, finish_run, start_run
from app.scheduler.watermark import load_watermark, save_watermark

pytestmark = pytest.mark.integration


async def test_start_and_finish_run(db_session) -> None:
    run = await start_run(db_session, trigger="test")
    assert run.status is RunStatus.RUNNING
    assert run.started_at is not None

    counters = RunCounters(
        queries_total=12,
        posts_found=100,
        posts_new=40,
        posts_analyzed=20,
        posts_approved=5,
        posts_rejected=15,
        notifications_queued=5,
        notifications_sent=5,
    )
    await finish_run(db_session, run, counters=counters)

    assert run.status is RunStatus.SUCCESS
    assert run.finished_at is not None
    assert run.posts_new == 40
    assert run.notifications_queued == 5
    assert run.details["posts_approved"] == 5


async def test_partial_status_on_errors(db_session) -> None:
    run = await start_run(db_session, trigger="test")
    counters = RunCounters(queries_total=10, queries_failed=2)
    counters.errors.append({"stage": "vk", "error": "timeout"})
    await finish_run(db_session, run, counters=counters)
    assert run.status is RunStatus.PARTIAL
    assert run.errors


async def test_failed_status_when_all_queries_fail(db_session) -> None:
    run = await start_run(db_session, trigger="test")
    counters = RunCounters(queries_total=5, queries_failed=5)
    await finish_run(db_session, run, counters=counters)
    assert run.status is RunStatus.FAILED


async def test_fail_run_records_error(db_session) -> None:
    run = await start_run(db_session, trigger="test")
    await fail_run(db_session, run, "boom")
    assert run.status is RunStatus.FAILED
    assert run.errors[-1]["error"] == "boom"


async def test_watermark_persisted(db_session) -> None:
    from datetime import UTC, datetime

    point = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    await save_watermark(db_session, last_success_at=point, run_id=77, window_end=point)
    loaded = await load_watermark(db_session)
    assert loaded.last_success_at == point
    assert loaded.last_run_id == 77


async def test_watermark_update_overwrites(db_session) -> None:
    from datetime import UTC, datetime, timedelta

    first = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    second = first + timedelta(hours=1)
    await save_watermark(db_session, last_success_at=first)
    await save_watermark(db_session, last_success_at=second)
    loaded = await load_watermark(db_session)
    assert loaded.last_success_at == second


def test_scheduler_jobs_configured() -> None:
    from app.scheduler.runner import SCHEDULE_HOURS, build_scheduler

    scheduler = build_scheduler()
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert "monitoring" in jobs
    assert "telegram_queue" in jobs
    assert SCHEDULE_HOURS == [10, 11, 12, 17, 18, 19]


def test_schedule_covers_two_windows() -> None:
    """Заказчик задал два окна: 10:00–12:00 и 17:00–19:00 по МСК.

    В базу попадают только публикации не старше 4 часов, поэтому каждое
    окно охватывает предшествующие часы без пропусков.
    """
    from app.scheduler.runner import SCHEDULE_HOURS

    morning = [hour for hour in SCHEDULE_HOURS if 10 <= hour <= 12]
    evening = [hour for hour in SCHEDULE_HOURS if 17 <= hour <= 19]
    assert morning == [10, 11, 12]
    assert evening == [17, 18, 19]
    # Вне окон поиск не выполняется
    assert all(10 <= hour <= 12 or 17 <= hour <= 19 for hour in SCHEDULE_HOURS)


def test_monitoring_job_max_instances_one() -> None:
    from app.scheduler.runner import build_scheduler

    scheduler = build_scheduler()
    job = next(j for j in scheduler.get_jobs() if j.id == "monitoring")
    assert job.max_instances == 1
    assert job.coalesce is True
