"""Проверка доступности комментирования публикации (требование заказчика).

**Решение: VK API, метод ``wall.getComments``.** В ответе этого метода есть
поле ``can_post`` — именно то, что нужно, и оно доступно сервисному ключу.

Ранее использовался headless-браузер (Playwright) и поиск класса
``closed_comments`` в HTML — этот подход оказался **неверным**:

* ``closed_comments`` присутствует в разметке ``<div class="_post post ...
  closed_comments deep_active ...">`` у **всех** публикаций — это служебный
  флаг VK, а не признак закрытых комментариев;
* мобильная версия (``m.vk.com``) для анонима вообще не рендерит блок
  комментариев, поэтому «нет ``wall_replies``» ничего не означает;
* в результате проверка всегда возвращала ``OPEN`` — то есть была фиктивной.

Проверено на реальных публикациях (``wall.getComments``):

* ``-65967716_947`` (автор ограничил комментарии) → ``can_post = False``
* ``-238001933_6418``, ``-33872127_32845`` (открыто) → ``can_post = True``
* ``-1_400529`` (открыто, 28 комментариев), ``-1_400458`` (69) → ``True``

**Когда проверяем.** Только публикации, уже одобренные LLM, непосредственно
перед постановкой в очередь Telegram — это один дешёвый API-запрос на пост.

**Отказоустойчивость.** Любая ошибка (сеть, лимит, неожиданный ответ)
трактуется как «неизвестно»: публикация **не** отбрасывается, потому что
потеря полезного уведомления хуже лишнего сообщения.
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass

from app.core.logging import get_logger
from app.vk.client import VKClient, VKError

logger = get_logger(__name__)

#: Метод VK API, в ответе которого есть признак can_post
COMMENTS_METHOD = "wall.getComments"


class CommentsAccess(str, enum.Enum):
    """Результат проверки доступности комментирования."""

    OPEN = "open"          # комментарии открыты — публикация годна
    CLOSED = "closed"      # комментарии закрыты — публикация отклоняется
    UNKNOWN = "unknown"    # проверить не удалось — публикация не отбрасывается


@dataclass(slots=True)
class CommentsCheckResult:
    """Итог проверки одной публикации."""

    access: CommentsAccess
    post_key: str
    reason: str | None = None
    elapsed_seconds: float = 0.0

    @property
    def closed(self) -> bool:
        return self.access is CommentsAccess.CLOSED

    @property
    def known(self) -> bool:
        return self.access is not CommentsAccess.UNKNOWN


def build_post_url(owner_id: int, post_id: int) -> str:
    """Ссылка на публикацию (для логов и уведомлений)."""
    return f"https://vk.com/wall{owner_id}_{post_id}"


def interpret_response(payload: object) -> CommentsAccess:
    """Преобразует ответ ``wall.getComments`` в статус комментирования.

    ``can_post`` — единственный достоверный признак. Если поле отсутствует
    или имеет неожидаемый тип, возвращается ``UNKNOWN``: лучше пропустить
    публикацию, чем отклонить её из-за изменения формата ответа.
    """
    if not isinstance(payload, dict):
        return CommentsAccess.UNKNOWN
    can_post = payload.get("can_post")
    if can_post is True:
        return CommentsAccess.OPEN
    if can_post is False:
        return CommentsAccess.CLOSED
    return CommentsAccess.UNKNOWN


async def check_comments_access(
    owner_id: int,
    post_id: int,
    *,
    client: VKClient | None = None,
    timeout_seconds: int = 20,
) -> CommentsCheckResult:
    """Проверяет, можно ли комментировать публикацию.

    Функция никогда не бросает исключений: любая проблема превращается в
    :attr:`CommentsAccess.UNKNOWN`.
    """
    post_key = f"{owner_id}_{post_id}"
    loop = asyncio.get_running_loop()
    start = loop.time()
    params = {"owner_id": owner_id, "post_id": post_id, "count": 1}

    try:
        if client is None:
            async with VKClient() as vk:
                payload = await vk.call(COMMENTS_METHOD, params)
        else:
            payload = await client.call(COMMENTS_METHOD, params)
    except VKError as exc:
        logger.warning("Не удалось проверить комментарии %s: %s", post_key, exc)
        return CommentsCheckResult(
            access=CommentsAccess.UNKNOWN,
            post_key=post_key,
            reason=f"VKError: {exc}",
            elapsed_seconds=round(loop.time() - start, 2),
        )
    except Exception as exc:  # noqa: BLE001 - проверка не должна ронять цикл
        logger.warning("Не удалось проверить комментарии %s: %s", post_key, exc)
        return CommentsCheckResult(
            access=CommentsAccess.UNKNOWN,
            post_key=post_key,
            reason=f"{exc.__class__.__name__}: {exc}",
            elapsed_seconds=round(loop.time() - start, 2),
        )

    access = interpret_response(payload)
    elapsed = round(loop.time() - start, 2)
    if access is CommentsAccess.CLOSED:
        logger.info("Комментарии закрыты: %s (%.2fs)", post_key, elapsed)
    elif access is CommentsAccess.UNKNOWN:
        logger.warning("Статус комментариев неизвестен: %s (нет can_post в ответе)", post_key)

    return CommentsCheckResult(
        access=access,
        post_key=post_key,
        reason=None if access is not CommentsAccess.UNKNOWN else "can_post is missing",
        elapsed_seconds=elapsed,
    )


async def check_many(
    posts: list[tuple[int, int]],
    *,
    timeout_seconds: int = 20,
    limit: int = 25,
    concurrency: int = 3,
    client: VKClient | None = None,
) -> dict[str, CommentsCheckResult]:
    """Проверяет несколько публикаций параллельно (ограниченно).

    ``posts`` — список пар ``(owner_id, post_id)``. Возвращает словарь
    ``post_key -> результат``. Проверяется не больше ``limit`` публикаций
    за вызов.

    Один общий :class:`VKClient` переиспользуется для всех запросов: это
    лёгкие вызовы API, а не запуск браузера, поэтому проверка дешёвая.
    """
    selected = posts[:limit]
    if not selected:
        return {}

    semaphore = asyncio.Semaphore(max(1, concurrency))
    owns_client = client is None
    vk = client

    async def _one(owner_id: int, post_id: int) -> CommentsCheckResult:
        async with semaphore:
            return await check_comments_access(
                owner_id,
                post_id,
                client=vk,
                timeout_seconds=timeout_seconds,
            )

    try:
        if vk is None:
            vk = VKClient()
        results = await asyncio.gather(
            *(_one(owner_id, post_id) for owner_id, post_id in selected),
            return_exceptions=True,
        )
    finally:
        if owns_client and vk is not None:
            await vk.close()

    output: dict[str, CommentsCheckResult] = {}
    for (owner_id, post_id), result in zip(selected, results, strict=True):
        key = f"{owner_id}_{post_id}"
        if isinstance(result, BaseException):
            logger.warning("Проверка комментариев %s завершилась ошибкой: %s", key, result)
            output[key] = CommentsCheckResult(
                access=CommentsAccess.UNKNOWN,
                post_key=key,
                reason=str(result),
            )
        else:
            output[key] = result
    return output
