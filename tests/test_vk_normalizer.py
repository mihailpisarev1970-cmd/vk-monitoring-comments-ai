"""Тесты нормализации публикаций и определения рекламных признаков (ТЗ п.6.2, п.8)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db.enums import AdFlag
from app.vk.models import MarkerQuery, NormalizedPost, SearchWindow
from app.vk.normalizer import (
    build_post_url,
    clean_text,
    collect_authors,
    compute_text_hash,
    detect_ad_flag,
    normalize_post,
    normalize_response,
)

RAW_ITEM = {
    "inner_type": "wall_wallpost",
    "type": "post",
    "id": 6304,
    "owner_id": -238001933,
    "from_id": -238001933,
    "date": 1794908089,
    "text": "Очередь постов для Telegram на Python",
    "marked_as_ads": 0,
    "post_type": "post",
    "comments": {"count": 3},
    "likes": {"count": 12, "can_like": 1},
    "reposts": {"count": 1},
    "attachments": [],
    "donut": {"is_donut": False},
}


# ---------------- ссылки и текст ----------------
def test_build_post_url_for_community() -> None:
    assert build_post_url(-238001933, 6304) == "https://vk.com/wall-238001933_6304"


def test_build_post_url_with_screen_name() -> None:
    url = build_post_url(-238001933, 6304, {-238001933: "habr"})
    assert url == "https://vk.com/habr?w=wall-238001933_6304"


def test_build_post_url_for_user() -> None:
    assert build_post_url(454932690, 322) == "https://vk.com/wall454932690_322"


def test_clean_text_expands_mentions_and_strips_read_more() -> None:
    raw = "[club238001933|Python Daily]   полезный   пост\n\nЧитать далее https://habr.com/x"
    assert clean_text(raw) == "Python Daily полезный пост"


def test_clean_text_empty() -> None:
    assert clean_text("") == ""
    assert clean_text("   \n\n ") == ""


def test_text_hash_ignores_case_and_whitespace() -> None:
    """Регистр и лишние пробелы не влияют на хеш (ТЗ п.11.4)."""
    assert compute_text_hash("привет   МИР") == compute_text_hash("Привет мир")


def test_text_hash_accounts_for_punctuation_and_length() -> None:
    """Хеш фиксирует только точные дубли.

    Пунктуация и длина участвуют в хеше, поэтому близкие, но не идентичные
    тексты получают разные значения, а непохожесть выявляет duplicate_filter.
    """
    assert compute_text_hash("Привет, МИР!") != compute_text_hash("привет мир")
    assert compute_text_hash("Привет мир") != compute_text_hash("Привет мир!!")


def test_text_hash_differs_for_different_text() -> None:
    """Разные тексты не должны считаться точными дубликатами (ТЗ п.11.4)."""
    assert compute_text_hash("Привет мир") != compute_text_hash("Привет мир! Дополнение")
    assert compute_text_hash("Текст") != compute_text_hash("Текст https://example.com")


def test_text_hash_none_for_empty_text() -> None:
    assert compute_text_hash("   ") is None


# ---------------- рекламные признаки (ТЗ п.8) ----------------
@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"marked_as_ads": 1}, AdFlag.YES),
        ({"marked_as_ads": True}, AdFlag.YES),
        ({"marked_as_ads": 0}, AdFlag.NO),
        ({"post_type": "post_ads"}, AdFlag.YES),
        ({"marked_as_ads": 0, "author_ad": {"ad_id": 1}}, AdFlag.YES),
        ({"marked_as_ads": 0, "marked_as_author_ad": 1}, AdFlag.YES),
        ({}, AdFlag.UNKNOWN),
        ({"post_type": "post"}, AdFlag.UNKNOWN),
    ],
)
def test_detect_ad_flag(item: dict, expected: AdFlag) -> None:
    flag, marked, details = detect_ad_flag(item)
    assert flag is expected
    assert isinstance(details, dict)


def test_detect_ad_flag_absent_is_unknown_not_no() -> None:
    """Отсутствие поля нельзя трактовать как «не реклама» (ТЗ п.8)."""
    flag, marked, details = detect_ad_flag({"post_type": "post"})
    assert flag is AdFlag.UNKNOWN
    assert marked is None
    assert details["marked_as_ads"] == "absent"



# ---------------- нормализация публикации ----------------
def test_normalize_post_maps_all_fields() -> None:
    post = normalize_post(RAW_ITEM)
    assert post is not None
    assert post.key == "-238001933_6304"
    assert post.post_url == "https://vk.com/wall-238001933_6304"
    assert post.published_at == datetime.fromtimestamp(1794908089, tz=UTC)
    assert post.comments_count == 3
    assert post.likes_count == 12
    assert post.reposts_count == 1
    assert post.views_count is None
    assert post.ad_flag is AdFlag.NO
    assert post.is_official_ad is False
    assert post.text_hash is not None


def test_normalize_post_requires_core_fields() -> None:
    assert normalize_post({"id": 1, "date": 100}) is None
    assert normalize_post({"owner_id": -1, "date": 100}) is None
    assert normalize_post({"owner_id": -1, "id": 2}) is None


def test_normalize_post_flags_official_ad() -> None:
    post = normalize_post(RAW_ITEM | {"marked_as_ads": 1})
    assert post is not None
    assert post.is_official_ad is True


def test_normalize_response_resolves_authors() -> None:
    payload = {
        "items": [RAW_ITEM, RAW_ITEM | {"id": 6305}],
        "profiles": [
            {"id": 454932690, "first_name": "Иван", "last_name": "Петров", "screen_name": "ivan"}
        ],
        "groups": [{"id": 238001933, "name": "Python Daily", "screen_name": "pydaily"}],
    }
    posts = normalize_response(payload)
    assert len(posts) == 2
    assert posts[0].author_name == "Python Daily"
    assert posts[0].post_url == "https://vk.com/pydaily?w=wall-238001933_6304"


def test_normalize_response_handles_broken_items() -> None:
    payload = {"items": [RAW_ITEM, {"broken": True}, "not-a-dict"]}
    posts = normalize_response(payload)
    assert len(posts) == 1


def test_marker_attached_and_deduplicated() -> None:
    marker = MarkerQuery(marker_name="Python", query="Python")
    post = normalize_post(RAW_ITEM, marker=marker)
    assert post is not None
    post.add_marker(marker)
    post.add_marker(MarkerQuery(marker_name="IT", query="#IT"))
    assert len(post.matched_markers) == 2
    assert post.marker_names == ["Python", "IT"]


def test_collect_authors_both_kinds() -> None:
    authors = collect_authors(
        {
            "profiles": [{"id": 1, "first_name": "A", "last_name": "B"}],
            "groups": [{"id": 2, "name": "Group", "screen_name": "grp"}],
        }
    )
    assert authors[1].name == "A B"
    assert authors[1].is_group is False
    assert authors[-2].name == "Group"
    assert authors[-2].is_group is True


def test_search_window_validation() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        SearchWindow(start=now, end=now)


def test_search_window_ts() -> None:
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    window = SearchWindow(start=start, end=end)
    assert window.start_ts == int(start.timestamp())
    assert window.end_ts == int(end.timestamp())
    assert window.duration_seconds == 7200


def test_normalized_post_key_and_markers() -> None:
    post = NormalizedPost(
        owner_id=-1,
        post_id=5,
        published_at=datetime.now(UTC),
        post_url="https://vk.com/wall-1_5",
    )
    assert post.key == "-1_5"
    assert post.marker_names == []
    assert post.ad_flag is AdFlag.UNKNOWN
