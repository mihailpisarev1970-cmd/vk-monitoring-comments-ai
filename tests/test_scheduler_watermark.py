"""Тесты watermark и восстановления пропусков (ТЗ п.5.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.scheduler.watermark import (
    Watermark,
    compute_window_start,
    missed_intervals,
)

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


# ------------------------------------------------------------ сериализация
def test_watermark_roundtrip() -> None:
    original = Watermark(
        last_success_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        last_run_id=42,
        last_window_end=datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
    )
    restored = Watermark.from_payload(original.to_payload())
    assert restored.last_success_at == original.last_success_at
    assert restored.last_run_id == 42
    assert restored.last_window_end == original.last_window_end


def test_watermark_from_empty_payload() -> None:
    empty = Watermark.from_payload(None)
    assert empty.last_success_at is None
    assert empty.last_run_id is None


def test_watermark_tolerates_bad_values() -> None:
    restored = Watermark.from_payload(
        {"last_success_at": "не дата", "last_run_id": "x", "last_window_end": None}
    )
    assert restored.last_success_at is None
    assert restored.last_run_id is None


# ------------------------------------------------------------ окно поиска
def test_first_run_uses_default_window() -> None:
    start = compute_window_start(
        Watermark(),
        now=NOW,
        window_minutes=120,
        overlap_minutes=15,
        recovery_hours=12,
    )
    assert start == NOW - timedelta(minutes=135)


def test_window_starts_from_watermark_with_overlap() -> None:
    watermark = Watermark(last_success_at=NOW - timedelta(minutes=60))
    start = compute_window_start(
        watermark, now=NOW, window_minutes=120, overlap_minutes=15, recovery_hours=12
    )
    assert start == NOW - timedelta(minutes=75)


def test_window_limited_after_long_downtime() -> None:
    """После простоя окно не должно уходить в глубокое прошлое."""
    watermark = Watermark(last_success_at=NOW - timedelta(days=3))
    start = compute_window_start(
        watermark, now=NOW, window_minutes=120, overlap_minutes=15, recovery_hours=12
    )
    assert start == NOW - timedelta(hours=12)


def test_window_never_in_future() -> None:
    watermark = Watermark(last_success_at=NOW + timedelta(hours=1))
    start = compute_window_start(
        watermark, now=NOW, window_minutes=120, overlap_minutes=15, recovery_hours=12
    )
    assert start <= NOW


# ------------------------------------------------------------ пропуски
def test_no_missed_intervals_without_watermark() -> None:
    assert (
        missed_intervals(
            Watermark(),
            now=NOW,
            schedule_hours=list(range(10, 24)),
            timezone_name="Europe/Moscow",
            window_minutes=120,
            overlap_minutes=15,
        )
        == []
    )


def test_missed_intervals_detected() -> None:
    """Сервис был выключен несколько часов — пропущенные окна должны быть найдены."""
    watermark = Watermark(last_success_at=datetime(2026, 3, 10, 9, 0, tzinfo=UTC))
    intervals = missed_intervals(
        watermark,
        now=datetime(2026, 3, 10, 14, 30, tzinfo=UTC),
        schedule_hours=list(range(8, 24)),
        timezone_name="Europe/Moscow",
        window_minutes=120,
        overlap_minutes=15,
    )
    # Все пропущенные слоты 09:00–14:00 UTC покрыты одним окном
    assert len(intervals) == 1
    start, end = intervals[0]
    assert start >= datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 10, 14, 0, tzinfo=UTC)


def test_no_missed_when_just_before_schedule() -> None:
    """Если watermark покрывает плановый слот, восстановление не требуется."""
    watermark = Watermark(last_success_at=datetime(2026, 3, 10, 15, 2, tzinfo=UTC))
    intervals = missed_intervals(
        watermark,
        now=datetime(2026, 3, 10, 15, 5, tzinfo=UTC),
        schedule_hours=list(range(14, 24)),
        timezone_name="Europe/Moscow",
        window_minutes=120,
        overlap_minutes=15,
    )
    assert intervals == []


def test_missed_detected_after_skip() -> None:
    """Пропущенные слоты покрываются одним окном восстановления."""
    watermark = Watermark(last_success_at=datetime(2026, 3, 10, 14, 0, tzinfo=UTC))
    intervals = missed_intervals(
        watermark,
        now=datetime(2026, 3, 10, 17, 30, tzinfo=UTC),
        schedule_hours=list(range(10, 24)),
        timezone_name="Europe/Moscow",
        window_minutes=120,
        overlap_minutes=15,
    )
    assert intervals == [
        (datetime(2026, 3, 10, 14, 45, tzinfo=UTC), datetime(2026, 3, 10, 17, 0, tzinfo=UTC))
    ]


def test_missed_intervals_respects_two_windows() -> None:
    """Пропущенные окна восстанавливаются только внутри 10–12 и 18–20.

    Между утренним и вечерним окном планировщик молчит, поэтому слоты
    12:00–17:00 восстанавливать не нужно.
    """
    # Последний успех в 11:30 МСК, сервис лежал до 18:30 МСК
    watermark = Watermark(last_success_at=datetime(2026, 3, 10, 8, 30, tzinfo=UTC))
    now = datetime(2026, 3, 10, 15, 30, tzinfo=UTC)

    intervals = missed_intervals(
        watermark,
        now=now,
        schedule_hours=[10, 11, 18, 19],
        timezone_name="Europe/Moscow",
        window_minutes=120,
        overlap_minutes=15,
    )

    assert len(intervals) == 1
    start, end = intervals[0]
    end_local = end.astimezone(ZoneInfo("Europe/Moscow"))
    # Восстановление заканчивается на пропущенном слоте 18:00 МСК
    assert (end_local.hour, end_local.minute) == (18, 0)
    # И не уходит в прошлое дальше последнего успеха
    assert start >= watermark.last_success_at


def test_no_missed_intervals_between_windows() -> None:
    """Сервис работал в 11:00, а до 18:00 ещё далеко — восстанавливать нечего."""
    watermark = Watermark(last_success_at=datetime(2026, 3, 10, 8, 5, tzinfo=UTC))  # 11:05 MSK
    now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)  # 15:00 MSK

    assert (
        missed_intervals(
            watermark,
            now=now,
            schedule_hours=[10, 11, 18, 19],
            timezone_name="Europe/Moscow",
            window_minutes=120,
            overlap_minutes=15,
        )
        == []
    )
