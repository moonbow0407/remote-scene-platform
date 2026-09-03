"""与参考项目矿山实体字段对齐的矿区表。"""

from datetime import datetime

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Mine(Base):
    """矿区基础信息；字段名保持与参考监管系统一致，便于迁移存量数据。"""

    __tablename__ = "mining_area"
    __table_args__ = (
        sa.Index(
            "ix_mining_area_boundary_polygon", "boundary_polygon", postgresql_using="gist"
        ),
        sa.Index("ix_mining_area_province", "mine_province"),
        sa.Index("ix_mining_area_status", "mine_status"),
    )

    mine_id: Mapped[str] = mapped_column(sa.String(255), primary_key=True, comment="矿山编号")
    mine_name: Mapped[str] = mapped_column(sa.String(255), nullable=False, comment="矿山名称")
    mine_type: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, comment="矿山类型")
    mine_province: Mapped[str | None] = mapped_column(
        sa.String(255), nullable=True, comment="省份"
    )
    mine_market: Mapped[str | None] = mapped_column(
        sa.String(255), nullable=True, comment="市/地区"
    )
    mine_county: Mapped[str | None] = mapped_column(
        sa.String(255), nullable=True, comment="区县"
    )
    mine_elevation_lower: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, comment="最低海拔"
    )
    mine_elevation_upper: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, comment="最高海拔"
    )
    mine_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True, comment="矿山状态")
    primary_contact_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    primary_contact_phone: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    dispatch_office_phone: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    boundary_polygon: Mapped[object] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326), nullable=False, comment="EPSG:4326 矿区边界"
    )
    green_mine_level: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    reclamation_rate: Mapped[float | None] = mapped_column(
        sa.Float, nullable=True, comment="复垦率"
    )
    ecological_quality: Mapped[float | None] = mapped_column(
        sa.Float, nullable=True, comment="生态质量"
    )
    create_time: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    update_time: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )
