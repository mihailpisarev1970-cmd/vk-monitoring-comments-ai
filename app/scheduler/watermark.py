"""Контрольные точки (watermark) и восстановление пропусков (ТЗ п.5.1).

Требования:
* хранить время последнего успешно обработанного интервала;
* не терять публикации на границах окон — использовать перекрытие;
* после перезапуска продолжать с последней контрольной точки;
* при пропуске запланированных запусков проверить пропущенный интервал
  в пределах настроенного периода восстановления.

Хранится в таблице ``scheduler_state`` (ключ → JSONB).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import SchedulerState

logger = get_logger(__name__)

#: Ключ записи watermark в scheduler_state
WATERMARK_KEY = "monitor_watermark"
#: Сколько последних текстов хранить как контекст дедупликации (ТЗ п.11.4)
DEDUP_HISTORY_SIZE = 300
#: Размер стандартного окна поиска (минуты) — для восстановления watermark
WINDOW_MINUTES = 120
#: Перекрытие окна (минуты)
OVERLAP_MINUTES = 15
#: Максимальная давность watermark, исправляемая автоматически (часы)
RECOVERY_WINDOW_HOURS = 12


@dataclass(slots=True)
class Watermark:
    """Контрольная точка мониторинга."""

    last_success_at: datetime | None = None
    last_run_id: int | None = None
    last_window_end: datetime | None = None

    def to_payload(self) -> dict[str, str | int | None]:
        return {
            "last_success_at": _iso(self.last_success_at),
            "last_run_id": self.last_run_id,
            "last_window_end": _iso(self.last_window_end),
        }

    @classmethod
    def from_payload(cls, payload: dict | None) -> Watermark:
        if not payload:
            return cls()
        return cls(
            last_success_at=_parse(payload.get("last_success_at")),
            last_run_id=_to_int(payload.get("last_run_id")),
            last_window_end=_parse(payload.get("last_window_end")),
        )


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _to_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def load_watermark(session: AsyncSession) -> Watermark:
    """Читает контрольную точку из БД."""
    record = await session.get(SchedulerState, WATERMARK_KEY)
    if record is None:
        return Watermark()
    return Watermark.from_payload(record.value)


async def save_watermark(
    session: AsyncSession,
    *,
    last_success_at: datetime,
    run_id: int | None = None,
    window_end: datetime | None = None,
) -> Watermark:
    """Сохраняет контрольную точку после успешного цикла."""
    watermark = Watermark(
        last_success_at=last_success_at, last_run_id=run_id, last_window_end=window_end
    )
    stmt = (
        pg_insert(SchedulerState)
        .values(
            key=WATERMARK_KEY,
            value=watermark.to_payload(),
            note="Последний успешно обработанный интервал мониторинга",
        )
        .on_conflict_do_update(
            index_elements=[SchedulerState.key],
            set_={"value": watermark.to_payload(), "updated_at": datetime.now(UTC)},
        )
    )
    await session.execute(stmt)
    await session.flush()
    logger.info("Контрольная точка сохранена: %s", watermark.last_success_at)
    return watermark



async def load_seen_texts(session: AsyncSession, limit: int = DEDUP_HISTORY_SIZE) -> list[str]:
    """Возвращает тексты последних обработанных публикаций (ТЗ п.11.4).

    Дедупликация не должна ограничиваться одной партией: одинаковые рекламные
    тексты рассылаются в течение дня разными сообществами.
    """
    from app.db.models import Post

    rows = (
        await session.execute(
            select(Post.text).order_by(Post.id.desc()).limit(limit)
        )
    ).all()
    from app.vk.normalizer import clean_text

    return [clean_text(row[0]) for row in rows if row[0]]


#: Признаки живой сети, генерирующей публикации прямо сейчас
FRESH_ACTIVITY_HOURS = 2


def effective_watermark(
    watermark: Watermark, *, now: datetime, fresh_hours: int = FRESH_ACTIVITY_HOURS
) -> Watermark:
    """Сдвигает watermark при обнаружении свежей активности.

    Если последний успешный цикл давно не находил новых публикаций, а в ВК
    прямо сейчас идёт активность, значит контрольная точка «застряла» после
    ручных тестов или очистки базы. В этом случае восстанавливаем окно
    последних часов, иначе новые публикации никогда не попадут в обработку.
    """
    now = now.astimezone(UTC)
    if watermark.last_success_at is None:
        return watermark

    age = now - watermark.last_success_at.astimezone(UTC)
    if age <= timedelta(hours=fresh_hours):
        return watermark

    max_age = timedelta(hours=RECOVERY_WINDOW_HOURS)
    new_point = now - timedelta(minutes=WINDOW_MINUTES + OVERLAP_MINUTES)
    if age <= max_age:
        logger.warning(
            "Watermark старше %s часов и найдена свежая активность — окно восстановлено",
            fresh_hours,
        )
        return Watermark(
            last_success_at=new_point,
            last_run_id=watermark.last_run_id,
            last_window_end=watermark.last_window_end,
        )
    return watermark


def compute_window_start(
    watermark: Watermark,
    *,
    now: datetime,
    window_minutes: int,
    overlap_minutes: int,
    recovery_hours: int,
) -> datetime:
    """Определяет начало следующего окна поиска.

    * первый запуск — ``now − window − overlap``;
    * обычный запуск — от контрольной точки минус перекрытие;
    * после простоя — не глубже ``recovery_hours`` от текущего момента
      (защита от гигантских окон после долгого простоя).
    """
    now = now.astimezone(UTC)
    default_start = now - timedelta(minutes=window_minutes + overlap_minutes)

    if watermark.last_success_at is None:
        return default_start

    candidate = watermark.last_success_at.astimezone(UTC) - timedelta(minutes=overlap_minutes)
    floor = now - timedelta(hours=max(1, recovery_hours))
    start = min(max(candidate, floor), now)
    logger.info(
        "Окно после перезапуска: старт %s (вода %s, пол %s)",
        start.isoformat(),
        candidate.isoformat(),
        floor.isoformat(),
    )
    return start


def missed_intervals(
    watermark: Watermark,
    *,
    now: datetime,
    schedule_hours: list[int],
    timezone_name: str,
    window_minutes: int,
    overlap_minutes: int,
) -> list[tuple[datetime, datetime]]:
    """Находит пропущенные запланированные интервалы.

    Возвращает список окон (start, end), которые не были обработаны, если
    сервис был выключен в момент планового запуска (ТЗ п.5.1).
    """
    from zoneinfo import ZoneInfo

    if watermark.last_success_at is None:
        return []

    tz = ZoneInfo(timezone_name)
    last_utc = watermark.last_success_at.astimezone(UTC)
    now_utc = now.astimezone(UTC)
    last = last_utc.astimezone(tz)
    current = now_utc.astimezone(tz)
    intervals: list[tuple[datetime, datetime]] = []

    cursor = last.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    first_slot: datetime | None = None
    last_slot: datetime | None = None
    while cursor <= current:
        scheduled_utc = cursor.astimezone(UTC)
        if (
            cursor.hour in schedule_hours
            and scheduled_utc > last_utc
            and scheduled_utc > now_utc - timedelta(hours=1)
            and scheduled_utc <= now_utc
        ):
            first_slot = first_slot or scheduled_utc
            last_slot = scheduled_utc
        cursor += timedelta(hours=1)

    if first_slot is None or last_slot is None:
        return []

    # Все пропущенные слоты покрываются одним окном восстановления с перекрытием
    window_start = first_slot - timedelta(minutes=window_minutes + overlap_minutes)
    intervals.append((max(window_start, last_utc), last_slot))

    return intervals
