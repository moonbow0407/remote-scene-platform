"""生态参数与数据源关系持久化模型。

不变量：
- 一行一条细项；大类是字段，没有 parent_id；
- `code` 为四位细项编号，`abbrev` 为英文缩写，二者均全局唯一；
- 与产品型号通过显式关系表关联，禁止 code/名称软引用；
- `(ecological_parameter_id, data_source_id, precision)` 唯一。
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.context import now_utc
from app.db import Base, TimestampMixin
from app.ecology.enums import EcologicalParameterStatus, Precision


class EcologicalParameter(Base, TimestampMixin):
    """生态参量细项；监测与检索按 id 引用。"""

    __tablename__ = "ecological_parameter"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, unique=True, comment="细项编号，四位数字，全局唯一"
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False, comment="中文名称")
    abbrev: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, unique=True, comment="英文缩写，全局唯一"
    )
    english_name: Mapped[str | None] = mapped_column(
        sa.String(255), nullable=True, comment="英文全称"
    )
    major_code: Mapped[str] = mapped_column(
        sa.String(8), nullable=False, index=True, comment="大类编号，等于 code 前两位"
    )
    major_name: Mapped[str] = mapped_column(sa.String(255), nullable=False, comment="大类名称")
    remark: Mapped[str | None] = mapped_column(sa.Text(), nullable=True, comment="管理员备注")
    status: Mapped[EcologicalParameterStatus] = mapped_column(
        sa.Enum(EcologicalParameterStatus, native_enum=False, length=16),
        nullable=False,
        default=EcologicalParameterStatus.ACTIVE,
        index=True,
        comment="ACTIVE/DISABLED",
    )
    sort_order: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, comment="同层排序，升序"
    )


class EcologicalParameterDataSource(Base):
    """生态细项 ↔ 产品型号 ↔ 精度。"""

    __tablename__ = "ecological_parameter_data_source"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    ecological_parameter_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("ecological_parameter.id", ondelete="RESTRICT"),
        nullable=False,
        comment="生态参数主键",
    )
    data_source_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("data_source.id", ondelete="RESTRICT"),
        nullable=False,
        comment="产品型号主键",
    )
    precision: Mapped[Precision] = mapped_column(
        sa.Enum(Precision, native_enum=False, length=8),
        nullable=False,
        comment="00 低精度 / 01 高精度",
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=now_utc, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "ecological_parameter_id",
            "data_source_id",
            "precision",
            name="uq_eco_param_data_source",
        ),
        sa.Index("ix_eco_ds_mapping_parameter_id", "ecological_parameter_id"),
        sa.Index("ix_eco_ds_mapping_data_source_id", "data_source_id"),
    )
