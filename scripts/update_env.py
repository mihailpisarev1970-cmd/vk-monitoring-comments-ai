"""Обновление .env на основе локального, с сохранением пароля БД.

Этот скрипт НЕ генерирует новый пароль БД: `gen_prod_env.py` предназначен
для первой установки, а при обновлении он сломал бы доступ к уже
инициализированной базе (пароль хранится в volume PostgreSQL).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gen_prod_env import OUT_PATH, build_env, read_local_env  # noqa: E402


def main(keep_password: str) -> None:
    local = read_local_env()
    missing = [
        key
        for key in (
            "VK_APP_ID",
            "VK_ACCESS_TOKEN",
            "AIDOSTUP_BASE_URL",
            "AIDOSTUP_API_KEY",
            "AIDOSTUP_MODEL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        )
        if not local.get(key)
    ]
    if missing:
        raise SystemExit(f"нет обязательных значений в .env: {missing}")

    OUT_PATH.write_text(build_env(local, keep_password), encoding="utf-8", newline="\n")
    print(f"{OUT_PATH.name} written, AIDOSTUP_MODEL={local['AIDOSTUP_MODEL']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python scripts/update_env.py <existing_postgres_password>")
    main(sys.argv[1])
