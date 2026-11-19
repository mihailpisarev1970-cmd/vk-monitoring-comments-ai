"""Сборка архива для деплоя на сервер (Windows-совместимо).

Исключения применяются к ОТНОСИТЕЛЬНОМУ пути внутри проекта, поэтому
служебные файлы вида app/__init__.py не отбрасываются. Именно из-за
Windows-`tar --exclude='_*.py'` контейнер падал с
`No module named app.__main__`.

Запуск:  .\\.venv\\Scripts\\python.exe scripts/pack_deploy.py
"""

from __future__ import annotations

import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "vk_deploy.tar.gz"

INCLUDE = [
    "app",
    "config",
    "migrations",
    "scripts",
    "alembic.ini",
    "pyproject.toml",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.prod.yml",
]

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log"}
SKIP_ROOT_FILES = {"prod.env", "vk_deploy.tar.gz"}


def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if any(part in SKIP_DIRS for part in parts):
        return None
    if Path(info.name).suffix in SKIP_SUFFIXES:
        return None
    if len(parts) == 1 and parts[0] in SKIP_ROOT_FILES:
        return None
    return info


def main() -> None:
    with tarfile.open(ARCHIVE, "w:gz") as tar:
        for name in INCLUDE:
            path = ROOT / name
            if path.exists():
                tar.add(path, arcname=name, filter=_filter)
    print(f"{ARCHIVE.name}: {ARCHIVE.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
