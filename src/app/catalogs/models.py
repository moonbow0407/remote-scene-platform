"""资源目录、卫星、传感器持久化模型。

不变量：
- 层级关系使用 UUID 外键（parent_id / satellite_id），禁止 code 字符串弱关联；
- code 全局唯一且稳定；关系引用一律走主键，改 code 不会断链；
- 有子节点或被引用时禁止删除（应用层校验 + DB RESTRICT）。
"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.catalogs.enums import CatalogStatus
from app.db import Base, TimestampMixin


class ResourceCatalog(Base, TimestampMixin):
    """层级资源目录：业务分类树，供资产与生态映射引用。"""

    __tablename__ = "resource_catalog"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, unique=True, comment="稳定业务编码，全局唯一"
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False, comment="显示名称")
    parent_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid,
        ForeignKey("resource_catalog.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        comment="父目录；根节点为 NULL",
    )
    status: Mapped[CatalogStatus] = mapped_column(
        sa.Enum(CatalogStatus, native_enum=False, length=16),
        nullable=False,
        default=CatalogStatus.ACTIVE,
        index=True,
        comment="ACTIVE/DISABLED",
    )
    sort_order: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, comment="同级排序，升序"
    )
    created_by: Mapped[Any | None] = mapped_column(
        sa.Uuid, nullable=True, comment="鉴权预留：创建者"
    )

    __table_args__ = (sa.Index("ix_resource_catalog_parent_sort", "parent_id", "sort_order"),)


class Satellite(Base, TimestampMixin):
    """卫星目录条目。"""

    __tablename__ = "satellite"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, unique=True, comment="稳定业务编码，全局唯一"
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False, comment="显示名称")
    status: Mapped[CatalogStatus] = mapped_column(
        sa.Enum(CatalogStatus, native_enum=False, length=16),
        nullable=False,
        default=CatalogStatus.ACTIVE,
        index=True,
        comment="ACTIVE/DISABLED",
    )
    sort_order: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, comment="列表排序，升序"
    )
    created_by: Mapped[Any | None] = mapped_column(
        sa.Uuid, nullable=True, comment="鉴权预留：创建者"
    )


class Sensor(Base, TimestampMixin):
    """传感器目录条目；明确归属一颗卫星（一对多，非旧系统混合树）。"""

    __tablename__ = "sensor"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, unique=True, comment="稳定业务编码，全局唯一"
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False, comment="显示名称")
    satellite_id: Mapped[UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("satellite.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="所属卫星；禁止悬空引用",
    )
    status: Mapped[CatalogStatus] = mapped_column(
        sa.Enum(CatalogStatus, native_enum=False, length=16),
        nullable=False,
        default=CatalogStatus.ACTIVE,
        index=True,
        comment="ACTIVE/DISABLED",
    )
    sort_order: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, comment="同卫星内排序，升序"
    )
    created_by: Mapped[Any | None] = mapped_column(
        sa.Uuid, nullable=True, comment="鉴权预留：创建者"
    )

    __table_args__ = (sa.Index("ix_sensor_satellite_sort", "satellite_id", "sort_order"),)
