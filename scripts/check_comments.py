"""Проверка доступности комментирования через VK API.

Запуск: python -m scripts.check_comments

Проверяет контрольные публикации: одну с закрытыми комментариями (автор
ограничил возможность комментирования) и несколько открытых. Используется
при приёмке и после обновлений, чтобы убедиться, что VK по-прежнему отдаёт
поле ``can_post`` в ответе ``wall.getComments``.
"""

import asyncio

from app.core.logging import setup_logging
from app.vk.comments_check import CommentsAccess, check_many

#: (owner_id, post_id, ожидаемый статус, пояснение)
CASES: list[tuple[int, int, CommentsAccess, str]] = [
    (-65967716, 947, CommentsAccess.CLOSED, "автор ограничил комментарии"),
    (-238001933, 6418, CommentsAccess.OPEN, "открыто"),
    (-33872127, 32845, CommentsAccess.OPEN, "открыто"),
]


async def main() -> int:
    setup_logging(log_to_file=False)
    results = await check_many([(owner, post) for owner, post, _, _ in CASES])

    matched = 0
    for owner, post, expected, note in CASES:
        key = f"{owner}_{post}"
        actual = results[key].access
        ok = actual is expected
        matched += ok
        print(
            f"  [{'OK' if ok else 'FAIL'}] {key:20} "
            f"ожидали={expected.value:6} получили={actual.value:6} ({note})"
        )

    print(f"\nСовпало: {matched}/{len(CASES)}")
    return 0 if matched == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
