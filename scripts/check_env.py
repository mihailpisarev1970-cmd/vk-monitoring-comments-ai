"""Проверка готовности окружения: БД, Redis, LLM API и Telegram.

Запуск:
    python -m scripts.check_env            # БД + Redis + миграции
    python -m scripts.check_env --external  # плюс реальные запросы к LLM и Telegram

Возвращает код 0 при успехе, 1 при любой ошибке.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.redis_client import close_redis, ping_redis
from app.db.session import engine

logger = get_logger("check_env")


async def check_postgres() -> bool:
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(text("SHOW server_version"))
            alembic_rev = await conn.scalar(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            tables = await conn.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
        logger.info("PostgreSQL OK: version=%s migrations=%s tables=%s", version, alembic_rev, tables)
        return bool(alembic_rev) and bool(tables) and int(tables) >= 6
    except Exception as exc:  # noqa: BLE001
        logger.error("PostgreSQL FAILED: %s", exc)
        return False
    finally:
        await engine.dispose()


async def check_redis() -> bool:
    ok = await ping_redis()
    if ok:
        logger.info("Redis OK: %s", settings.redis_url)
    else:
        logger.error("Redis FAILED: %s", settings.redis_url)
    await close_redis()
    return ok


async def check_llm() -> bool:
    """Реальный запрос к AiDostup: проверяет ключ и выбранную модель."""
    key = settings.aidostup_api_key.get_secret_value()
    if not key or not settings.aidostup_model:
        logger.error("LLM FAILED: AIDOSTUP_API_KEY или AIDOSTUP_MODEL не заданы")
        return False
    url = settings.aidostup_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.aidostup_model,
        "messages": [{"role": "user", "content": "Ответь одним словом: работает"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                url, json=payload, headers={"Authorization": f"Bearer {key}"}
            )
        if resp.status_code != 200:
            logger.error("LLM FAILED: HTTP %s %s", resp.status_code, resp.text[:300])
            return False
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        logger.info(
            "LLM OK: model=%s answer=%r tokens=%s",
            settings.aidostup_model,
            content[:40],
            usage.get("total_tokens"),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM FAILED: %s", exc)
        return False


async def check_telegram() -> bool:
    """Проверяет токен бота и доступность чата (без отправки сообщения)."""
    token = settings.telegram_bot_token.get_secret_value()
    if not token or not settings.telegram_chat_id:
        logger.error("Telegram FAILED: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return False
    base = f"https://api.telegram.org/bot{token}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            me = await client.get(f"{base}/getMe")
            me.raise_for_status()
            bot = me.json()["result"]
            chat = await client.get(
                f"{base}/getChat", params={"chat_id": settings.telegram_chat_id}
            )
            chat.raise_for_status()
            chat_title = chat.json()["result"].get("username") or chat.json()["result"].get("id")
        logger.info(
            "Telegram OK: bot=@%s chat=%s", bot.get("username"), chat_title
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Telegram FAILED: %s", exc)
        return False


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка окружения VK Monitor Assistant")
    parser.add_argument(
        "--external",
        action="store_true",
        help="дополнительно проверить LLM API и Telegram (реальные сетевые запросы)",
    )
    args = parser.parse_args(argv)

    setup_logging(log_to_file=False)
    logger.info("App env=%s timezone=%s", settings.app_env, settings.timezone)

    results = {
        "postgres": await check_postgres(),
        "redis": await check_redis(),
    }
    if args.external:
        results["llm"] = await check_llm()
        results["telegram"] = await check_telegram()

    if all(results.values()):
        logger.info("Environment is ready: %s", ", ".join(results))
        return 0
    logger.error("Environment is NOT ready: %s", results)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
