"""生态参数与资源目录映射持久化模型。

不变量：
- 生态参数与资源目录通过显式多对多映射表关联，禁止 code/名称软引用；
- 废弃旧双列 `data_code_medium` / `data_code_high` 设计；
- `(ecological_parameter_id, resource_catalog_id)` 唯一；
- 关系引用一律走 UUID 外键，改 code 不断链。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.context import now_utc
from app.db import Base, TimestampMixin
from app.ecology.enums import EcologicalParameterStatus


class EcologicalParameter(Base, TimestampMixin):
    """生态参数：可层级组织；监测与资产过滤按稳定 code / id 引用。"""

    __tablename__ = "ecological_parameter"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, unique=True, comment="稳定业务编码，全局唯一"
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False, comment="显示名称")
    parent_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid,
        ForeignKey("ecological_parameter.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        comment="父参数；根节点为 NULL",
    )
    status: Mapped[EcologicalParameterStatus] = mapped_column(
        sa.Enum(EcologicalParameterStatus, native_enum=False, length=16),
        nullable=False,
        default=EcologicalParameterStatus.ACTIVE,
        index=True,
        comment="ACTIVE/DISABLED",
    )
    sort_order: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, comment="同级排序，升序"
    )
    created_by: Mapped[Any | None] = mapped_column(
        sa.Uuid, nullable=True, comment="鉴权预留：创建者"
    )

    __table_args__ = (
        sa.Index("ix_ecological_parameter_parent_sort", "parent_id", "sort_order"),
    )


class EcologicalParameterResourceMapping(Base):
    """生态参数 ↔ 资源目录 显式多对多；无可变业务属性，不提供 PUT。"""

    __tablename__ = "ecological_parameter_resource_mapping"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    ecological_parameter_id: Mapped[UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("ecological_parameter.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="生态参数主键",
    )
    resource_catalog_id: Mapped[UUID] = mapped_column(
        sa.Uuid,
        ForeignKey("resource_catalog.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="资源目录主键",
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=now_utc, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "ecological_parameter_id",
            "resource_catalog_id",
            name="uq_eco_param_resource_mapping",
        ),
    )
