"""卫星 / 无人机影像行：栅格字段平行，没有统一资产表。"""

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geometry, WKTElement
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin
from app.imagery.enums import ObjectCleanupKind, ObjectCleanupStatus, RecordStatus


class RasterRecordMixin:
    """卫星与无人机共用的栅格与生命周期字段。"""

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    data_source_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("data_source.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="产品型号",
    )
    status: Mapped[RecordStatus] = mapped_column(
        sa.Enum(RecordStatus, native_enum=False, length=16),
        nullable=False,
        default=RecordStatus.UPLOADING,
        index=True,
    )
    acquired_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    original_file_name: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    original_object_key: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)
    cog_object_key: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)
    thumbnail_object_key: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)

    crs: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    user_crs: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    width: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    band_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    bands: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    resolution_x: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 10), nullable=True)
    resolution_y: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 10), nullable=True)
    nodata: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    render_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    footprint: Mapped[WKTElement | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326), nullable=True
    )
    min_x: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    min_y: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    max_x: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    max_y: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    created_by: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    purge_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    purge_next_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    purge_last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class SatelliteData(Base, TimestampMixin, RasterRecordMixin):
    """一景卫星影像。"""

    __tablename__ = "satellite_data"
    __table_args__ = (
        sa.Index(
            "ix_satellite_data_purge_due", "deleted_at", "purge_after", "purge_next_attempt_at"
        ),
        sa.Index("ix_satellite_data_search", "status", "acquired_at", "created_at"),
        sa.Index("ix_satellite_data_footprint", "footprint", postgresql_using="gist"),
    )


class UavData(Base, TimestampMixin, RasterRecordMixin):
    """一景无人机影像。"""

    __tablename__ = "uav_data"
    __table_args__ = (
        sa.Index("ix_uav_data_purge_due", "deleted_at", "purge_after", "purge_next_attempt_at"),
        sa.Index("ix_uav_data_search", "status", "acquired_at", "created_at"),
        sa.Index("ix_uav_data_footprint", "footprint", postgresql_using="gist"),
    )


class ObjectCleanupTask(Base, TimestampMixin):
    """MinIO 对象删除任务。数据库先落任务，cleanup 进程再删对象。"""

    __tablename__ = "object_cleanup_task"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    kind: Mapped[ObjectCleanupKind] = mapped_column(
        sa.Enum(ObjectCleanupKind, native_enum=False, length=16), nullable=False
    )
    object_key: Mapped[str] = mapped_column(sa.String(1024), nullable=False, unique=True)
    status: Mapped[ObjectCleanupStatus] = mapped_column(
        sa.Enum(ObjectCleanupStatus, native_enum=False, length=16),
        nullable=False,
        default=ObjectCleanupStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (sa.Index("ix_object_cleanup_due", "status", "next_attempt_at", "created_at"),)
