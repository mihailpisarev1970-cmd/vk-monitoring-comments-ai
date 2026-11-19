"""Декларативная база SQLAlchemy 2.x и общие миксины."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Именование ограничений — чтобы Alembic генерировал стабильные имена
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс моделей."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Текущее время в UTC (timezone-aware)."""
    return datetime.now(UTC)


def enum_values(enum_cls: type[PyEnum]) -> list[str]:
    """Значения enum (нижний регистр) — используются в native enum PostgreSQL."""
    return [member.value for member in enum_cls]


def enum_column(enum_cls: type[PyEnum], name: str, **kwargs) -> SAEnum:
    """Колонка с PostgreSQL ENUM, значения которого = value Python-enum.

    В БД хранятся значения (new, rejected_ad, ...), а не имена членов.
    """
    enum_type = SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        validate_strings=True,
        values_callable=enum_values,
    )
    return enum_type


class TimestampMixin:
    """Колонки created_at / updated_at с автообновлением."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
