"""Stage 4 资源目录、卫星/传感器、生态参数与映射，以及资产分类外键。

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_catalog",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, comment="稳定业务编码，全局唯一"),
        sa.Column("name", sa.String(255), nullable=False, comment="显示名称"),
        sa.Column(
            "parent_id",
            sa.Uuid(),
            sa.ForeignKey(
                "resource_catalog.id",
                ondelete="RESTRICT",
                name="fk_resource_catalog_parent_id_resource_catalog",
            ),
            nullable=True,
            comment="父目录；根节点为 NULL",
        ),
        sa.Column("status", sa.String(16), nullable=False, comment="ACTIVE/DISABLED"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Uuid(), nullable=True, comment="鉴权预留：创建者"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("code", name="uq_resource_catalog_code"),
    )
    op.create_index("ix_resource_catalog_parent_id", "resource_catalog", ["parent_id"])
    op.create_index("ix_resource_catalog_status", "resource_catalog", ["status"])
    op.create_index(
        "ix_resource_catalog_parent_sort", "resource_catalog", ["parent_id", "sort_order"]
    )

    op.create_table(
        "satellite",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, comment="稳定业务编码，全局唯一"),
        sa.Column("name", sa.String(255), nullable=False, comment="显示名称"),
        sa.Column("status", sa.String(16), nullable=False, comment="ACTIVE/DISABLED"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Uuid(), nullable=True, comment="鉴权预留：创建者"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("code", name="uq_satellite_code"),
    )
    op.create_index("ix_satellite_status", "satellite", ["status"])

    op.create_table(
        "sensor",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, comment="稳定业务编码，全局唯一"),
        sa.Column("name", sa.String(255), nullable=False, comment="显示名称"),
        sa.Column(
            "satellite_id",
            sa.Uuid(),
            sa.ForeignKey(
                "satellite.id", ondelete="RESTRICT", name="fk_sensor_satellite_id_satellite"
            ),
            nullable=False,
            comment="所属卫星；禁止悬空引用",
        ),
        sa.Column("status", sa.String(16), nullable=False, comment="ACTIVE/DISABLED"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Uuid(), nullable=True, comment="鉴权预留：创建者"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("code", name="uq_sensor_code"),
    )
    op.create_index("ix_sensor_satellite_id", "sensor", ["satellite_id"])
    op.create_index("ix_sensor_status", "sensor", ["status"])
    op.create_index("ix_sensor_satellite_sort", "sensor", ["satellite_id", "sort_order"])

    op.create_table(
        "ecological_parameter",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, comment="稳定业务编码，全局唯一"),
        sa.Column("name", sa.String(255), nullable=False, comment="显示名称"),
        sa.Column(
            "parent_id",
            sa.Uuid(),
            sa.ForeignKey(
                "ecological_parameter.id",
                ondelete="RESTRICT",
                name="fk_ecological_parameter_parent_id_ecological_parameter",
            ),
            nullable=True,
            comment="父参数；根节点为 NULL",
        ),
        sa.Column("status", sa.String(16), nullable=False, comment="ACTIVE/DISABLED"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Uuid(), nullable=True, comment="鉴权预留：创建者"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("code", name="uq_ecological_parameter_code"),
    )
    op.create_index("ix_ecological_parameter_parent_id", "ecological_parameter", ["parent_id"])
    op.create_index("ix_ecological_parameter_status", "ecological_parameter", ["status"])
    op.create_index(
        "ix_ecological_parameter_parent_sort",
        "ecological_parameter",
        ["parent_id", "sort_order"],
    )

    op.create_table(
        "ecological_parameter_resource_mapping",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "ecological_parameter_id",
            sa.Uuid(),
            sa.ForeignKey(
                "ecological_parameter.id",
                ondelete="RESTRICT",
                name="fk_eco_mapping_parameter_id_ecological_parameter",
            ),
            nullable=False,
            comment="生态参数主键",
        ),
        sa.Column(
            "resource_catalog_id",
            sa.Uuid(),
            sa.ForeignKey(
                "resource_catalog.id",
                ondelete="RESTRICT",
                name="fk_eco_mapping_resource_id_resource_catalog",
            ),
            nullable=False,
            comment="资源目录主键",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "ecological_parameter_id",
            "resource_catalog_id",
            name="uq_eco_param_resource_mapping",
        ),
    )
    op.create_index(
        "ix_eco_mapping_parameter_id",
        "ecological_parameter_resource_mapping",
        ["ecological_parameter_id"],
    )
    op.create_index(
        "ix_eco_mapping_resource_id",
        "ecological_parameter_resource_mapping",
        ["resource_catalog_id"],
    )

    op.add_column(
        "data_asset",
        sa.Column(
            "resource_catalog_id",
            sa.Uuid(),
            nullable=True,
            comment="业务分类：资源目录主键；禁止名称/code 软引用",
        ),
    )
    op.add_column(
        "data_asset",
        sa.Column("satellite_id", sa.Uuid(), nullable=True, comment="平台：卫星主键"),
    )
    op.add_column(
        "data_asset",
        sa.Column(
            "sensor_id",
            sa.Uuid(),
            nullable=True,
            comment="仪器：传感器主键；须属于 satellite_id",
        ),
    )
    op.create_foreign_key(
        "fk_data_asset_resource_catalog_id_resource_catalog",
        "data_asset",
        "resource_catalog",
        ["resource_catalog_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_data_asset_satellite_id_satellite",
        "data_asset",
        "satellite",
        ["satellite_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_data_asset_sensor_id_sensor",
        "data_asset",
        "sensor",
        ["sensor_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_data_asset_resource_catalog_id", "data_asset", ["resource_catalog_id"])
    op.create_index("ix_data_asset_satellite_id", "data_asset", ["satellite_id"])
    op.create_index("ix_data_asset_sensor_id", "data_asset", ["sensor_id"])


def downgrade() -> None:
    op.drop_index("ix_data_asset_sensor_id", table_name="data_asset")
    op.drop_index("ix_data_asset_satellite_id", table_name="data_asset")
    op.drop_index("ix_data_asset_resource_catalog_id", table_name="data_asset")
    op.drop_constraint("fk_data_asset_sensor_id_sensor", "data_asset", type_="foreignkey")
    op.drop_constraint("fk_data_asset_satellite_id_satellite", "data_asset", type_="foreignkey")
    op.drop_constraint(
        "fk_data_asset_resource_catalog_id_resource_catalog",
        "data_asset",
        type_="foreignkey",
    )
    op.drop_column("data_asset", "sensor_id")
    op.drop_column("data_asset", "satellite_id")
    op.drop_column("data_asset", "resource_catalog_id")

    op.drop_index("ix_eco_mapping_resource_id", table_name="ecological_parameter_resource_mapping")
    op.drop_index("ix_eco_mapping_parameter_id", table_name="ecological_parameter_resource_mapping")
    op.drop_table("ecological_parameter_resource_mapping")
    op.drop_index("ix_ecological_parameter_parent_sort", table_name="ecological_parameter")
    op.drop_index("ix_ecological_parameter_status", table_name="ecological_parameter")
    op.drop_index("ix_ecological_parameter_parent_id", table_name="ecological_parameter")
    op.drop_table("ecological_parameter")
    op.drop_index("ix_sensor_satellite_sort", table_name="sensor")
    op.drop_index("ix_sensor_status", table_name="sensor")
    op.drop_index("ix_sensor_satellite_id", table_name="sensor")
    op.drop_table("sensor")
    op.drop_index("ix_satellite_status", table_name="satellite")
    op.drop_table("satellite")
    op.drop_index("ix_resource_catalog_parent_sort", table_name="resource_catalog")
    op.drop_index("ix_resource_catalog_status", table_name="resource_catalog")
    op.drop_index("ix_resource_catalog_parent_id", table_name="resource_catalog")
    op.drop_table("resource_catalog")
