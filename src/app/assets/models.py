"""资产持久化模型：逻辑资产、不可变版本、类型扩展、工件与内容寻址对象。

不变量：
- 版本一经写入不可变更业务输入；新文件产生新版本，不覆盖历史；
- Job 与监测执行必须引用具体 asset_version；
- object_blob 按 SHA-256 内容寻址，多个逻辑资产/版本可共享同一 blob，
  物理清理前引用计数必须归零；
- 栅格 COG 保留原始 CRS；检索与 API 的 footprint 统一为 EPSG:4326。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geometry, WKTElement
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.assets.enums import ArtifactKind, AssetSource, AssetType, AssetVersionStatus
from app.db import Base, TimestampMixin


class DataAsset(Base, TimestampMixin):
    """逻辑资产：名称、来源、类型与当前版本指针。"""

    __tablename__ = "data_asset"

    id: Mapped[Any] = mapped_column(sa.Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    asset_type: Mapped[AssetType] = mapped_column(
        sa.Enum(AssetType, native_enum=False, length=16),
        nullable=False,
        index=True,
        comment="物理类型：RASTER/VECTOR/ATTACHMENT",
    )
    source: Mapped[AssetSource] = mapped_column(
        sa.Enum(AssetSource, native_enum=False, length=16),
        nullable=False,
        comment="来源：UPLOAD/SATELLITE/EXTERNAL_IMPORT",
    )
    # 业务元数据（扩展属性），Stage 3 起按 JSON Schema 校验
    properties: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # 当前版本指针；与 asset_version.asset_id 互引，用 use_alter 处理建表顺序
    current_version_id: Mapped[Any | None] = mapped_column(
        sa.Uuid, ForeignKey("asset_version.id", use_alter=True, ondelete="SET NULL"), nullable=True
    )
    # 鉴权预留：首版为 NULL（匿名系统操作者）
    owner_id: Mapped[Any | None] = mapped_column(
        sa.Uuid, nullable=True, comment="鉴权预留，首版为 NULL"
    )
    created_by: Mapped[Any | None] = mapped_column(
        sa.Uuid, nullable=True, comment="鉴权预留，首版为 NULL"
    )

    versions: Mapped[list["AssetVersion"]] = relationship(
        back_populates="asset", foreign_keys="AssetVersion.asset_id"
    )


class AssetVersion(Base, TimestampMixin):
    """不可变版本：文件、状态、时间与空间元数据的载体。"""

    __tablename__ = "asset_version"

    id: Mapped[Any] = mapped_column(sa.Uuid, primary_key=True)
    asset_id: Mapped[Any] = mapped_column(
        ForeignKey("data_asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[AssetVersionStatus] = mapped_column(
        sa.Enum(AssetVersionStatus, native_enum=False, length=16),
        nullable=False,
        default=AssetVersionStatus.UPLOADING,
        index=True,
        comment="UPLOADING/VALIDATING/PROCESSING/NEEDS_INPUT/READY/FAILED/DELETED",
    )
    # STAC 核心时间语义：采集/生产时间（typed column 供高频检索）
    acquired_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    original_file_name: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # 业务属性快照（本版本的业务元数据）
    properties: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # 诊断报告：失败原因或 NEEDS_INPUT 缺失项，JSON：{reason, detail, missing[]}
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    blob_id: Mapped[Any | None] = mapped_column(
        ForeignKey("object_blob.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    asset: Mapped[DataAsset] = relationship(back_populates="versions", foreign_keys=[asset_id])
    blob: Mapped["ObjectBlob | None"] = relationship()
    raster: Mapped["RasterAssetVersion | None"] = relationship(
        back_populates="version", uselist=False
    )
    artifacts: Mapped[list["AssetArtifact"]] = relationship(back_populates="version")

    __table_args__ = (
        UniqueConstraint("asset_id", "version_number", name="uq_asset_version_number"),
    )


class ObjectBlob(Base, TimestampMixin):
    """内容寻址对象：SHA-256 去重与引用计数，物理清理只看引用计数。"""

    __tablename__ = "object_blob"

    id: Mapped[Any] = mapped_column(sa.Uuid, primary_key=True)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    object_key: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    reference_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)


class RasterAssetVersion(Base):
    """栅格类型扩展：CRS、尺寸、波段、分辨率、NoData、footprint 与渲染推断。"""

    __tablename__ = "raster_asset_version"

    asset_version_id: Mapped[Any] = mapped_column(
        ForeignKey("asset_version.id", ondelete="CASCADE"), primary_key=True
    )
    crs: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    # 用户通过 NEEDS_INPUT 流程补充的 CRS（源文件缺失地理参考时）
    user_crs: Mapped[str | None] = mapped_column(
        sa.String(128), nullable=True, comment="用户经 NEEDS_INPUT 流程补充的 CRS"
    )
    width: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    band_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # 波段明细：[{index, name, dtype, min, max, mean}]
    bands: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True, comment="波段明细与统计")
    resolution_x: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 10), nullable=True)
    resolution_y: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 10), nullable=True)
    nodata: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    # 渲染推断：{"mode": "rgb"|"grayscale", "bands": [...]}
    render_profile: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="渲染推断 {mode, bands}"
    )
    # EPSG:4326 footprint 与结构化 bbox（高频检索列）
    footprint: Mapped[WKTElement | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326), nullable=True
    )
    min_x: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    min_y: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    max_x: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    max_y: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    version: Mapped[AssetVersion] = relationship(back_populates="raster")

    __table_args__ = (sa.Index("ix_raster_footprint", "footprint", postgresql_using="gist"),)


class AssetArtifact(Base):
    """工件：原文件、COG、缩略图与未来算法成果。按 (版本, 种类) 幂等。"""

    __tablename__ = "asset_artifact"

    id: Mapped[Any] = mapped_column(sa.Uuid, primary_key=True)
    asset_version_id: Mapped[Any] = mapped_column(
        ForeignKey("asset_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[ArtifactKind] = mapped_column(
        sa.Enum(ArtifactKind, native_enum=False, length=16),
        nullable=False,
        comment="ORIGINAL/COG/THUMBNAIL",
    )
    object_key: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    content_type: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=sa.func.now(), nullable=False
    )

    version: Mapped[AssetVersion] = relationship(back_populates="artifacts")

    __table_args__ = (
        UniqueConstraint("asset_version_id", "kind", name="uq_artifact_version_kind"),
    )
