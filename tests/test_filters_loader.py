"""Тесты загрузчика конфигурации фильтров (ТЗ п.4.1, п.11.5)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.core.config import settings
from app.filters.loader import (
    ConfigError,
    ConfigLoader,
    MarkersConfig,
    load_config,
    parse_spam_words,
)

CONFIG_DIR = Path(settings.config_dir)


def make_workspace(tmp_path: Path) -> Path:
    """Копирует рабочие конфиги во временную папку."""
    target = tmp_path / "config"
    target.mkdir()
    for name in ("markers.json", "filters.json", "spam_words.json"):
        shutil.copy(CONFIG_DIR / name, target / name)
    return target


def test_real_config_loads() -> None:
    bundle = load_config(force=True)
    assert len(bundle.queries) > 0
    assert bundle.markers.enabled is True
    assert bundle.filters.monitoring.count_per_query <= 200


def test_active_queries_respect_switches(tmp_path: Path) -> None:
    cfg_dir = make_workspace(tmp_path)
    data = json.loads((cfg_dir / "markers.json").read_text(encoding="utf-8"))
    data["markers"][0]["enabled"] = False
    (cfg_dir / "markers.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    loader = ConfigLoader(cfg_dir)
    bundle = loader.load()
    names = {q.marker_name for q in bundle.queries}
    assert "IT" not in names
    assert "Python" in names


def test_disabled_globally_returns_no_queries(tmp_path: Path) -> None:
    cfg_dir = make_workspace(tmp_path)
    data = json.loads((cfg_dir / "markers.json").read_text(encoding="utf-8"))
    data["enabled"] = False
    (cfg_dir / "markers.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    loader = ConfigLoader(cfg_dir)
    assert loader.load().queries == []


def test_invalid_json_raises_clear_error(tmp_path: Path) -> None:
    cfg_dir = make_workspace(tmp_path)
    (cfg_dir / "markers.json").write_text('{"enabled": true, "markers": [', encoding="utf-8")

    loader = ConfigLoader(cfg_dir)
    with pytest.raises(ConfigError) as exc:
        loader.load()
    assert "markers.json" in str(exc.value)
    assert "некорректный JSON" in str(exc.value)


def test_missing_file_raises(tmp_path: Path) -> None:
    cfg_dir = make_workspace(tmp_path)
    (cfg_dir / "spam_words.json").unlink()
    loader = ConfigLoader(cfg_dir)
    with pytest.raises(ConfigError) as exc:
        loader.load()
    assert "spam_words.json" in str(exc.value)


def test_no_active_markers_raises(tmp_path: Path) -> None:
    cfg_dir = make_workspace(tmp_path)
    data = json.loads((cfg_dir / "markers.json").read_text(encoding="utf-8"))
    for marker in data["markers"]:
        marker["enabled"] = False
    (cfg_dir / "markers.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        ConfigLoader(cfg_dir).load()
    assert "нет активных тематик" in str(exc.value)


def test_duplicate_marker_names_rejected() -> None:
    with pytest.raises(ValueError):
        MarkersConfig.model_validate(
            {
                "markers": [
                    {"name": "IT", "queries": ["IT"], "enabled": True},
                    {"name": "IT", "queries": ["#IT"], "enabled": True},
                ]
            }
        )


def test_cache_reused_until_file_changes(tmp_path: Path) -> None:
    cfg_dir = make_workspace(tmp_path)
    loader = ConfigLoader(cfg_dir)
    first = loader.load()
    second = loader.load()
    assert first is second  # кеш

    data = json.loads((cfg_dir / "filters.json").read_text(encoding="utf-8"))
    data["monitoring"]["window_minutes"] = 90
    (cfg_dir / "filters.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    third = loader.load(force=True)
    assert third is not first
    assert third.filters.monitoring.window_minutes == 90


def test_try_reload_returns_none_on_error(tmp_path: Path) -> None:
    cfg_dir = make_workspace(tmp_path)
    loader = ConfigLoader(cfg_dir)
    loader.load()
    (cfg_dir / "filters.json").write_text("not json", encoding="utf-8")
    assert loader.try_reload() is None
    assert loader.last_error is not None


def test_invalid_regex_rejected() -> None:
    with pytest.raises(ConfigError):
        parse_spam_words({"course": {"strong": ["(незакрытая скобка"]}})


def test_wordlist_matching() -> None:
    words = parse_spam_words({"course": {"strong": ["набираю группу на курс"]}})
    assert words.course["strong"].count("Набираю группу на курс Python") == 1
    assert words.course["strong"].count("Обсуждаем asyncio") == 0
