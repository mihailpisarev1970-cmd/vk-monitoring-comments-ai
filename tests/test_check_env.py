"""Тесты скрипта проверки окружения (без сетевых запросов)."""

from __future__ import annotations

import argparse

import pytest

from scripts import check_env


def test_external_flag_registered(monkeypatch) -> None:
    """Флаг --external распознаётся парсером и включает внешние проверки."""
    captured: dict[str, bool] = {}

    async def fake_pg() -> bool:
        return True

    async def fake_redis() -> bool:
        return True

    async def fake_llm() -> bool:
        captured["llm"] = True
        return True

    async def fake_tg() -> bool:
        captured["telegram"] = True
        return True

    def fake_setup(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(check_env, "check_postgres", fake_pg)
    monkeypatch.setattr(check_env, "check_redis", fake_redis)
    monkeypatch.setattr(check_env, "check_llm", fake_llm)
    monkeypatch.setattr(check_env, "check_telegram", fake_tg)
    monkeypatch.setattr(check_env, "setup_logging", fake_setup)

    assert asyncio_run(check_env.main([])) == 0
    assert captured == {}

    assert asyncio_run(check_env.main(["--external"])) == 0
    assert captured == {"llm": True, "telegram": True}


def asyncio_run(coro) -> int:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_parser_rejects_unknown_flag() -> None:
    parser = argparse.ArgumentParser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--nope"])


async def test_check_llm_fails_without_key(monkeypatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(check_env.settings, "aidostup_api_key", SecretStr(""))
    assert await check_env.check_llm() is False


async def test_check_telegram_fails_without_chat(monkeypatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(check_env.settings, "telegram_chat_id", "")
    monkeypatch.setattr(check_env.settings, "telegram_bot_token", SecretStr("123:abc"))
    assert await check_env.check_telegram() is False
