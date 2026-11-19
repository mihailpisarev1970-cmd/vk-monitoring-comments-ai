"""Общие фикстуры тестов.

Тесты БД используют ту же PostgreSQL из docker-compose, но отдельную схему
НЕ создают: применяются к текущей базе. Каждый тест работает в транзакции
с откатом (savepoint), поэтому данные в БД не остаются.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

# Windows: ProactorEventLoop некорректно закрывает соединения aiohttp/redis,
# поэтому тесты выполняются на SelectorEventLoop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Сессия с транзакцией, которая откатывается после теста.

    Тесты могут вызывать rollback внутри (проверка IntegrityError) — тогда
    откатывается только savepoint, а внешняя транзакция остаётся живой.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()
