"""Нормализация публикаций VK API (ТЗ п.6.2) и определение рекламных признаков (ТЗ п.8)."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from app.db.enums import AdFlag
from app.vk.models import AuthorInfo, MarkerQuery, NormalizedPost

#: Служебная строка «Читать далее <url>» и подобные хвосты RSS/репостов
_READ_MORE_RE = re.compile(r"\s*(Читать далее|Подробнее|Источник)\s*:?\s*https?://\S+\s*$", re.I)
#: Ссылки VK на внутренние сущности (упоминания) заменяются на читаемый текст
_VK_MENTION_RE = re.compile(r"\[(id|club|public|event)(\d+)\|([^\]]+)\]")
_HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

ADS_POST_TYPES = {"post_ads", "ads"}


def build_post_url(owner_id: int, post_id: int, screen_names: dict[int, str] | None = None) -> str:
    """Каноническая ссылка на публикацию (ТЗ п.6.2).

    Для сообществ owner_id отрицательный — ссылка вида
    https://vk.com/wall-123_456 работает корректно. Если известен
    screen_name, используется более читаемая форма.
    """
    if screen_names and owner_id in screen_names and owner_id < 0:
        return f"https://vk.com/{screen_names[owner_id]}?w=wall{owner_id}_{post_id}"
    return f"https://vk.com/wall{owner_id}_{post_id}"


def clean_text(text: str) -> str:
    """Нормализует текст публикации для анализа и хеширования.

    * разворачивает упоминания `[club1|Название]` в «Название»;
    * убирает хвост «Читать далее <ссылка>» (RSS-импорт);
    * схлопывает пробелы, убирает пустые строки в начале/конце.
    Исходный текст при этом сохраняется отдельно (raw_payload).
    """
    if not text:
        return ""
    cleaned = _VK_MENTION_RE.sub(lambda m: m.group(3), text)
    cleaned = _READ_MORE_RE.sub("", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "\n".join(_WHITESPACE_RE.sub(" ", line).strip() for line in cleaned.split("\n"))
    return cleaned.strip()


def compute_text_hash(text: str) -> str | None:
    """Хеш нормализованного текста для поиска точных дубликатов (ТЗ п.11.4).

    Нормализация: регистр, пробелы, пунктуация; ссылки отбрасываются.
    Длина исходного текста включается в хеш: это отсекает ложные совпадения
    коротких текстов, состоящих только из ссылок, и гарантирует, что
    сообщения разной длины не считаются точными дубликатами.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return None
    normalized = cleaned.lower()
    normalized = re.sub(r"https?://\S+", " ", normalized)
    normalized = re.sub(r"[^\w\s#]", " ", normalized, flags=re.UNICODE)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    if not normalized:
        return None
    payload = f"{normalized}|len={len(cleaned)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def detect_ad_flag(item: dict[str, Any]) -> tuple[AdFlag, bool | None, dict[str, Any]]:
    """Определяет статус официальной рекламы VK (ТЗ п.8).

    Возвращает (ad_flag, marked_as_ads, details).

    Правила:
    * `marked_as_ads == 1` / `post_type == post_ads` / непустой `author_ad`
      / `marked_as_author_ad == 1` → YES (безусловное исключение);
    * поля присутствуют и признаков нет → NO;
    * ключевые поля **отсутствуют** → UNKNOWN (нельзя трактовать как «не реклама»).
    """
    details: dict[str, Any] = {}
    marked_as_ads: bool | None = None
    positive = False

    if "marked_as_ads" in item:
        marked_as_ads = bool(item.get("marked_as_ads"))
        details["marked_as_ads"] = item.get("marked_as_ads")
        positive = positive or marked_as_ads
    else:
        details["marked_as_ads"] = "absent"

    if "marked_as_author_ad" in item:
        details["marked_as_author_ad"] = item.get("marked_as_author_ad")
        positive = positive or bool(item.get("marked_as_author_ad"))
    else:
        details["marked_as_author_ad"] = "absent"

    if item.get("author_ad"):
        details["author_ad"] = True
        positive = True
    else:
        details["author_ad"] = "absent" if "author_ad" not in item else "empty"

    post_type = item.get("post_type")
    if post_type is not None:
        details["post_type"] = post_type
        if str(post_type).lower() in ADS_POST_TYPES:
            positive = True
    else:
        details["post_type"] = "absent"

    if positive:
        return AdFlag.YES, marked_as_ads, details

    # Нет ни одного признака: достоверно «нет рекламы» только если поле
    # marked_as_ads реально присутствовало в ответе
    if marked_as_ads is not None:
        return AdFlag.NO, marked_as_ads, details
    return AdFlag.UNKNOWN, None, details



def collect_authors(payload: dict[str, Any]) -> dict[int, AuthorInfo]:
    """Собирает сведения об авторах из `profiles` и `groups` (extended=1)."""
    authors: dict[int, AuthorInfo] = {}
    for profile in payload.get("profiles") or []:
        uid = profile.get("id")
        if uid is None:
            continue
        name = " ".join(
            part for part in (profile.get("first_name"), profile.get("last_name")) if part
        ).strip()
        authors[int(uid)] = AuthorInfo(
            id=int(uid),
            name=name or profile.get("screen_name"),
            screen_name=profile.get("screen_name"),
            is_group=False,
        )
    for group in payload.get("groups") or []:
        gid = group.get("id")
        if gid is None:
            continue
        authors[-int(gid)] = AuthorInfo(
            id=-int(gid),
            name=group.get("name"),
            screen_name=group.get("screen_name"),
            is_group=True,
        )
    return authors


def _metric(item: dict[str, Any], field: str) -> int | None:
    block = item.get(field)
    if isinstance(block, dict):
        value = block.get("count")
        return int(value) if isinstance(value, int | float) else None
    return None


def normalize_post(
    item: dict[str, Any],
    *,
    authors: dict[int, AuthorInfo] | None = None,
    marker: MarkerQuery | None = None,
) -> NormalizedPost | None:
    """Приводит элемент ответа `newsfeed.search` к `NormalizedPost`.

    Возвращает None, если обязательных полей нет (публикация непригодна).
    """
    owner_id = item.get("owner_id")
    post_id = item.get("id")
    date = item.get("date")
    if owner_id is None or post_id is None or date is None:
        return None

    owner_id, post_id = int(owner_id), int(post_id)
    authors = authors or {}
    author_id = item.get("from_id")
    author = authors.get(int(author_id)) if author_id is not None else authors.get(owner_id)

    ad_flag, marked_as_ads, ad_details = detect_ad_flag(item)
    text = item.get("text") or ""
    screen_names = {aid: a.screen_name for aid, a in authors.items() if a.screen_name}

    post = NormalizedPost(
        owner_id=owner_id,
        post_id=post_id,
        author_id=int(author_id) if author_id is not None else None,
        author_name=author.name if author else None,
        text=text,
        published_at=datetime.fromtimestamp(int(date), tz=UTC),
        post_url=build_post_url(owner_id, post_id, screen_names),
        comments_count=_metric(item, "comments"),
        likes_count=_metric(item, "likes"),
        reposts_count=_metric(item, "reposts"),
        views_count=_metric(item, "views"),
        marked_as_ads=marked_as_ads,
        ad_flag=ad_flag,
        post_type=item.get("post_type"),
        ad_details=ad_details,
        text_hash=compute_text_hash(text),
        raw_payload=dict(item),
    )
    if marker is not None:
        post.add_marker(marker)
    return post


def normalize_response(
    payload: dict[str, Any], marker: MarkerQuery | None = None
) -> list[NormalizedPost]:
    """Нормализует весь ответ `newsfeed.search`."""
    authors = collect_authors(payload)
    posts: list[NormalizedPost] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        post = normalize_post(item, authors=authors, marker=marker)
        if post is not None:
            posts.append(post)
    return posts
