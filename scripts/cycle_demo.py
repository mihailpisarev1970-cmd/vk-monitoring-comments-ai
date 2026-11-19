"""Прогон цикла с расширенным окном (демонстрация полного потока).

Запуск: python -m scripts.cycle_demo [--hours 6]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from app.core.logging import setup_logging
from app.db.models import SchedulerState
from app.db.session import engine, session_scope
from app.scheduler.monitoring import MonitoringCycle
from app.scheduler.watermark import WATERMARK_KEY


async def main(hours: int) -> int:
    setup_logging(log_to_file=False)
    now = datetime.now(UTC)
    point = now - timedelta(hours=hours)

    async with session_scope() as session:
        record = await session.get(SchedulerState, WATERMARK_KEY)
        if record is None:
            session.add(
                SchedulerState(
                    key=WATERMARK_KEY,
                    value={"last_success_at": point.isoformat(), "last_run_id": None},
                    note="демонстрационное окно",
                )
            )
        else:
            record.value = {"last_success_at": point.isoformat(), "last_run_id": None}
        await session.flush()
    print(f"Watermark установлен на {point.isoformat()} (окно {hours} ч)")

    result = await MonitoringCycle().run(trigger="manual")
    print("\n=== Результат цикла ===")
    for key, value in result.to_dict().items():
        print(f"  {key}: {value}")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=6)
    sys.exit(asyncio.run(main(parser.parse_args().hours)))
