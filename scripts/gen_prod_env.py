"""Генерация продового .env на основе локального .env.

Секреты читаются из файла, а не перепечатываются вручную: ручной ввод
уже приводил к искажению токенов (лишний символ → HTTP 401 от LLM и
Telegram, диагностика заняла время, потому что значения выглядели
правдоподобно).

Запуск:  .\\.venv\\Scripts\\python.exe scripts/gen_prod_env.py
"""

from __future__ import annotations

import secrets
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
OUT_PATH = ROOT / "prod.env"

REQUIRED = [
    "VK_APP_ID",
    "VK_ACCESS_TOKEN",
    "AIDOSTUP_BASE_URL",
    "AIDOSTUP_API_KEY",
    "AIDOSTUP_MODEL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


def read_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def build_env(local: dict[str, str], password: str) -> str:
    return "\n".join(
        [
            "# ===== VK =====",
            f"VK_APP_ID={local['VK_APP_ID']}",
            f"VK_ACCESS_TOKEN={local['VK_ACCESS_TOKEN']}",
            f"VK_API_VERSION={local.get('VK_API_VERSION', '5.199')}",
            "",
            "# ===== LLM (AiDostup, OpenAI-compatible) =====",
            f"AIDOSTUP_BASE_URL={local['AIDOSTUP_BASE_URL']}",
            f"AIDOSTUP_API_KEY={local['AIDOSTUP_API_KEY']}",
            f"AIDOSTUP_MODEL={local['AIDOSTUP_MODEL']}",
            f"AIDOSTUP_COMMENT_MODEL={local.get('AIDOSTUP_COMMENT_MODEL', '')}",
            "",
            "# ===== Telegram =====",
            f"TELEGRAM_BOT_TOKEN={local['TELEGRAM_BOT_TOKEN']}",
            f"TELEGRAM_CHAT_ID={local['TELEGRAM_CHAT_ID']}",
            "",
            "# ===== PostgreSQL (имена сервисов внутри compose) =====",
            "POSTGRES_DB=vk_monitor",
            "POSTGRES_USER=vk_monitor",
            f"POSTGRES_PASSWORD={password}",
            "",
            "# ===== Redis =====",
            "REDIS_URL=redis://vk_comments_redis:6379/0",
            "",
            "# ===== Application =====",
            "APP_ENV=production",
            "LOG_LEVEL=INFO",
            "TIMEZONE=Europe/Moscow",
            "",
        ]
    )


def main() -> None:
    local = read_local_env()
    missing = [key for key in REQUIRED if not local.get(key)]
    if missing:
        raise SystemExit(f"нет обязательных значений в .env: {missing}")

    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(32))

    OUT_PATH.write_text(build_env(local, password), encoding="utf-8", newline="\n")
    print(f"{OUT_PATH.name} written")
    print(f"  AIDOSTUP_API_KEY    len={len(local['AIDOSTUP_API_KEY'])}")
    print(f"  TELEGRAM_BOT_TOKEN  len={len(local['TELEGRAM_BOT_TOKEN'])}")
    print(f"  VK_ACCESS_TOKEN     len={len(local['VK_ACCESS_TOKEN'])}")


if __name__ == "__main__":
    main()
