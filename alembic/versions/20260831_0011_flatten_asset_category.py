"""压平资产模型：去掉版本/卫星/对象引用计数，分类改为平铺表。

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS asset_artifact CASCADE")
    op.execute("DROP TABLE IF EXISTS raster_asset_version CASCADE")
    op.execute("DROP TABLE IF EXISTS vector_asset_version CASCADE")
    op.execute("DROP TABLE IF EXISTS attachment_asset_version CASCADE")
    op.execute("DROP TABLE IF EXISTS property_schema CASCADE")
    op.execute("DROP TABLE IF EXISTS asset_version CASCADE")
    op.execute("DROP TABLE IF EXISTS object_blob CASCADE")
    op.execute("DROP TABLE IF EXISTS sensor CASCADE")
    op.execute("DROP TABLE IF EXISTS satellite CASCADE")
    op.execute("DROP TABLE IF EXISTS resource_catalog CASCADE")

    op.create_table(
        "category",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False, comment="显示名称，全局唯一"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("name", name="uq_category_name"),
    )

    op.add_column(
        "data_asset",
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="UPLOADING",
            comment="UPLOADING/VALIDATING/PROCESSING/NEEDS_INPUT/READY/FAILED",
        ),
    )
    op.add_column("data_asset", sa.Column("category_id", sa.Integer(), nullable=True))
    op.add_column("data_asset", sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "data_asset",
        sa.Column("original_file_name", sa.String(512), nullable=False, server_default=""),
    )
    op.add_column(
        "data_asset",
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("data_asset", sa.Column("diagnostics", JSONB, nullable=True))
    op.add_column("data_asset", sa.Column("original_object_key", sa.String(1024), nullable=True))
    op.add_column("data_asset", sa.Column("cog_object_key", sa.String(1024), nullable=True))
    op.add_column("data_asset", sa.Column("thumbnail_object_key", sa.String(1024), nullable=True))
    op.add_column("data_asset", sa.Column("crs", sa.String(128), nullable=True))
    op.add_column("data_asset", sa.Column("user_crs", sa.String(128), nullable=True))
    op.add_column("data_asset", sa.Column("width", sa.Integer(), nullable=True))
    op.add_column("data_asset", sa.Column("height", sa.Integer(), nullable=True))
    op.add_column("data_asset", sa.Column("band_count", sa.Integer(), nullable=True))
    op.add_column("data_asset", sa.Column("bands", JSONB, nullable=True))
    op.add_column("data_asset", sa.Column("resolution_x", sa.Numeric(18, 10), nullable=True))
    op.add_column("data_asset", sa.Column("resolution_y", sa.Numeric(18, 10), nullable=True))
    op.add_column("data_asset", sa.Column("nodata", sa.Float(), nullable=True))
    op.add_column("data_asset", sa.Column("render_profile", JSONB, nullable=True))
    op.add_column(
        "data_asset",
        sa.Column("footprint", Geometry(geometry_type="POLYGON", srid=4326), nullable=True),
    )
    op.add_column("data_asset", sa.Column("min_x", sa.Float(), nullable=True))
    op.add_column("data_asset", sa.Column("min_y", sa.Float(), nullable=True))
    op.add_column("data_asset", sa.Column("max_x", sa.Float(), nullable=True))
    op.add_column("data_asset", sa.Column("max_y", sa.Float(), nullable=True))
    op.add_column("data_asset", sa.Column("geometry_type", sa.String(32), nullable=True))
    op.add_column("data_asset", sa.Column("feature_count", sa.Integer(), nullable=True))
    op.add_column("data_asset", sa.Column("native_format", sa.String(32), nullable=True))
    op.add_column("data_asset", sa.Column("vector_property_schema", JSONB, nullable=True))
    op.add_column("data_asset", sa.Column("mime_type", sa.String(128), nullable=True))
    op.add_column("data_asset", sa.Column("detected_format", sa.String(32), nullable=True))

    op.create_foreign_key(
        "fk_data_asset_category_id_category",
        "data_asset",
        "category",
        ["category_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_data_asset_category_id", "data_asset", ["category_id"])
    op.create_index("ix_data_asset_status", "data_asset", ["status"])
    op.create_index("ix_data_asset_search", "data_asset", ["status", "acquired_at", "created_at"])
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_data_asset_footprint ON data_asset USING gist (footprint)"
    )

    for col in (
        "source",
        "properties",
        "current_version_id",
        "resource_catalog_id",
        "satellite_id",
        "sensor_id",
    ):
        op.execute(f"ALTER TABLE data_asset DROP COLUMN IF EXISTS {col}")

    op.alter_column("data_asset", "status", server_default=None)
    op.alter_column("data_asset", "original_file_name", server_default=None)
    op.alter_column("data_asset", "size_bytes", server_default=None)

    op.execute("ALTER TABLE job DROP CONSTRAINT IF EXISTS fk_job_asset_version_id_asset_version")
    op.execute("ALTER TABLE job DROP CONSTRAINT IF EXISTS fk_job_version_id_asset_version")
    op.execute("DROP INDEX IF EXISTS ix_job_asset_version_id")
    op.execute("ALTER TABLE job RENAME COLUMN asset_version_id TO asset_id")
    op.create_foreign_key(
        "fk_job_asset_id_data_asset",
        "job",
        "data_asset",
        ["asset_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_job_asset_id", "job", ["asset_id"])

    op.execute(
        "ALTER TABLE vector_feature DROP CONSTRAINT IF EXISTS "
        "fk_vector_feature_asset_version_id_asset_version"
    )
    op.execute("DROP INDEX IF EXISTS ix_vector_feature_asset_version_id")
    op.execute("ALTER TABLE vector_feature RENAME COLUMN asset_version_id TO asset_id")
    op.create_foreign_key(
        "fk_vector_feature_asset_id_data_asset",
        "vector_feature",
        "data_asset",
        ["asset_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_vector_feature_asset_id", "vector_feature", ["asset_id"])

    op.execute(
        "ALTER TABLE ecological_parameter_resource_mapping DROP CONSTRAINT IF EXISTS "
        "fk_eco_mapping_resource_id_resource_catalog"
    )
    op.execute("DROP INDEX IF EXISTS ix_eco_mapping_resource_id")
    op.execute(
        "ALTER TABLE ecological_parameter_resource_mapping "
        "RENAME COLUMN resource_catalog_id TO category_id"
    )
    op.create_foreign_key(
        "fk_eco_mapping_category_id_category",
        "ecological_parameter_resource_mapping",
        "category",
        ["category_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_eco_mapping_resource_id", "ecological_parameter_resource_mapping", ["category_id"]
    )

    op.execute(
        "ALTER TABLE monitoring_plan DROP CONSTRAINT IF EXISTS "
        "fk_monitoring_plan_resource_catalog_id_resource_catalog"
    )
    op.execute("ALTER TABLE monitoring_plan RENAME COLUMN resource_catalog_id TO category_id")
    op.create_foreign_key(
        "fk_monitoring_plan_category_id_category",
        "monitoring_plan",
        "category",
        ["category_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(
        "ALTER TABLE monitoring_run_input DROP CONSTRAINT IF EXISTS "
        "fk_monitoring_run_input_asset_version_id_asset_version"
    )
    # PostgreSQL 把 UniqueConstraint 做成约束+同名唯一索引，必须先删约束；
    # 直接 DROP INDEX 会报 DependentObjectsStillExist。
    op.execute(
        "ALTER TABLE monitoring_run_input DROP CONSTRAINT IF EXISTS uq_monitoring_run_input_version"
    )
    op.execute("ALTER TABLE monitoring_run_input DROP COLUMN IF EXISTS asset_version_id")
    op.create_unique_constraint(
        "uq_monitoring_run_input_asset", "monitoring_run_input", ["run_id", "asset_id"]
    )

    op.execute("ALTER TABLE upload_session DROP COLUMN IF EXISTS properties")
    op.execute("ALTER TABLE upload_session DROP COLUMN IF EXISTS completed_version_id")
    op.execute("ALTER TABLE object_cleanup_task DROP COLUMN IF EXISTS blob_id")


def downgrade() -> None:
    raise RuntimeError("0011 为结构压平，不支持自动降级；请从空库按 0001–0010 重建")
