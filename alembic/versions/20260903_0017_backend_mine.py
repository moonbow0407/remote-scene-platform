"""引入与参考项目字段对齐的 mining_area 矿区表。

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mining_area",
        sa.Column("mine_id", sa.String(length=255), primary_key=True, comment="矿山编号"),
        sa.Column("mine_name", sa.String(length=255), nullable=False, comment="矿山名称"),
        sa.Column("mine_type", sa.Integer(), nullable=True, comment="矿山类型"),
        sa.Column("mine_province", sa.String(length=255), nullable=True, comment="省份"),
        sa.Column("mine_market", sa.String(length=255), nullable=True, comment="市/地区"),
        sa.Column("mine_county", sa.String(length=255), nullable=True, comment="区县"),
        sa.Column("mine_elevation_lower", sa.Integer(), nullable=True, comment="最低海拔"),
        sa.Column("mine_elevation_upper", sa.Integer(), nullable=True, comment="最高海拔"),
        sa.Column("mine_status", sa.Integer(), nullable=True, comment="矿山状态"),
        sa.Column("primary_contact_name", sa.String(length=255), nullable=True),
        sa.Column("primary_contact_phone", sa.String(length=255), nullable=True),
        sa.Column("dispatch_office_phone", sa.String(length=255), nullable=True),
        sa.Column(
            "boundary_polygon",
            Geometry(geometry_type="GEOMETRY", srid=4326),
            nullable=False,
            comment="EPSG:4326 矿区边界",
        ),
        sa.Column("green_mine_level", sa.String(length=255), nullable=True),
        sa.Column("reclamation_rate", sa.Float(), nullable=True, comment="复垦率"),
        sa.Column("ecological_quality", sa.Float(), nullable=True, comment="生态质量"),
        sa.Column(
            "create_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "update_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_mining_area_boundary_polygon",
        "mining_area",
        ["boundary_polygon"],
        postgresql_using="gist",
    )
    op.create_index("ix_mining_area_province", "mining_area", ["mine_province"])
    op.create_index("ix_mining_area_status", "mining_area", ["mine_status"])


def downgrade() -> None:
    op.drop_index("ix_mining_area_status", table_name="mining_area")
    op.drop_index("ix_mining_area_province", table_name="mining_area")
    op.drop_index("ix_mining_area_boundary_polygon", table_name="mining_area")
    op.drop_table("mining_area")
