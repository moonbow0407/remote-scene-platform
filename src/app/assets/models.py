"""资产持久化：一行资产即一份文件，派生对象键与空间字段同表。

不变量：
- 每次上传创建一条新资产，没有版本；传错则软删除后重传；
- Job 与监测快照引用 data_asset.id；
- 原件/COG/缩略图以对象键列存放，同一文件传两次在 MinIO 存两份；
- 栅格 COG 保留原始 CRS；检索 footprint 为 EPSG:4326。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geometry, WKTElement
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.assets.enums import AssetStatus, AssetType, ObjectCleanupKind, ObjectCleanupStatus
from app.db import Base, TimestampMixin


class DataAsset(Base, TimestampMixin):
    """一份上传数据：状态、分类、原件/派生对象与类型扩展字段。"""

    __tablename__ = "data_asset"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    asset_type: Mapped[AssetType] = mapped_column(
        sa.Enum(AssetType, native_enum=False, length=16),
        nullable=False,
        index=True,
        comment="物理类型：RASTER/VECTOR/ATTACHMENT",
    )
    status: Mapped[AssetStatus] = mapped_column(
        sa.Enum(AssetStatus, native_enum=False, length=16),
        nullable=False,
        default=AssetStatus.UPLOADING,
        index=True,
        comment="UPLOADING/VALIDATING/PROCESSING/NEEDS_INPUT/READY/FAILED",
    )
    category_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("category.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        comment="平铺分类；空表示未归类",
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

    geometry_type: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    feature_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    native_format: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    vector_property_schema: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    mime_type: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    detected_format: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

    owner_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    created_by: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    purge_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    purge_next_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    purge_last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (
        sa.Index("ix_data_asset_purge_due", "deleted_at", "purge_after", "purge_next_attempt_at"),
        sa.Index("ix_data_asset_search", "status", "acquired_at", "created_at"),
        sa.Index("ix_data_asset_footprint", "footprint", postgresql_using="gist"),
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
