"""废弃资产：卫星/无人机分表 + 数据源字典。

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from app.data_sources.seed_data import seed_data_sources

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _raster_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("data_source_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_file_name", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("diagnostics", JSONB(), nullable=True),
        sa.Column("original_object_key", sa.String(1024), nullable=True),
        sa.Column("cog_object_key", sa.String(1024), nullable=True),
        sa.Column("thumbnail_object_key", sa.String(1024), nullable=True),
        sa.Column("crs", sa.String(128), nullable=True),
        sa.Column("user_crs", sa.String(128), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("band_count", sa.Integer(), nullable=True),
        sa.Column("bands", JSONB(), nullable=True),
        sa.Column("resolution_x", sa.Numeric(18, 10), nullable=True),
        sa.Column("resolution_y", sa.Numeric(18, 10), nullable=True),
        sa.Column("nodata", sa.Float(), nullable=True),
        sa.Column("render_profile", JSONB(), nullable=True),
        sa.Column("footprint", Geometry(geometry_type="POLYGON", srid=4326), nullable=True),
        sa.Column("min_x", sa.Float(), nullable=True),
        sa.Column("min_y", sa.Float(), nullable=True),
        sa.Column("max_x", sa.Float(), nullable=True),
        sa.Column("max_y", sa.Float(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Integer(), nullable=True),
        sa.Column("purge_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("purge_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "data_source",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("code", name="uq_data_source_code"),
    )
    op.create_index("ix_data_source_kind", "data_source", ["kind"])
    op.create_index("ix_data_source_status", "data_source", ["status"])

    bind = op.get_bind()
    insert_sql = sa.text(
        "INSERT INTO data_source (code, name, kind, status) "
        "VALUES (:code, :name, :kind, 'ACTIVE') ON CONFLICT (code) DO NOTHING"
    )
    for item in seed_data_sources():
        bind.execute(insert_sql, item)

    for table in ("satellite_data", "uav_data"):
        op.create_table(
            table,
            *_raster_columns(),
            sa.ForeignKeyConstraint(
                ["data_source_id"], ["data_source.id"], ondelete="RESTRICT"
            ),
        )
        op.create_index(f"ix_{table}_data_source_id", table, ["data_source_id"])
        op.create_index(f"ix_{table}_status", table, ["status"])
        op.create_index(
            f"ix_{table}_purge_due", table, ["deleted_at", "purge_after", "purge_next_attempt_at"]
        )
        op.create_index(f"ix_{table}_search", table, ["status", "acquired_at", "created_at"])
        op.create_index(f"ix_{table}_footprint", table, ["footprint"], postgresql_using="gist")

    op.create_table(
        "ecological_parameter_data_source",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ecological_parameter_id", sa.Integer(), nullable=False),
        sa.Column("data_source_id", sa.Integer(), nullable=False),
        sa.Column("precision", sa.String(8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ecological_parameter_id"], ["ecological_parameter.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["data_source_id"], ["data_source.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "ecological_parameter_id",
            "data_source_id",
            "precision",
            name="uq_eco_param_data_source",
        ),
    )
    op.create_index(
        "ix_eco_ds_mapping_parameter_id",
        "ecological_parameter_data_source",
        ["ecological_parameter_id"],
    )
    op.create_index(
        "ix_eco_ds_mapping_data_source_id",
        "ecological_parameter_data_source",
        ["data_source_id"],
    )

    op.add_column("job", sa.Column("owner_kind", sa.String(16), nullable=True))
    op.add_column("job", sa.Column("owner_id", sa.Integer(), nullable=True))
    op.create_index("ix_job_owner_id", "job", ["owner_id"])
    op.drop_constraint("fk_job_asset_id_data_asset", "job", type_="foreignkey")
    op.drop_index("ix_job_asset_id", table_name="job")
    op.drop_column("job", "asset_id")

    op.add_column("upload_session", sa.Column("owner_kind", sa.String(16), nullable=True))
    op.add_column("upload_session", sa.Column("owner_id", sa.Integer(), nullable=True))
    op.execute("DELETE FROM upload_session")
    op.alter_column("upload_session", "owner_kind", nullable=False)
    op.alter_column("upload_session", "owner_id", nullable=False)
    op.drop_constraint(
        "fk_upload_session_asset_id_data_asset", "upload_session", type_="foreignkey"
    )
    op.drop_index("ix_upload_session_asset_id", table_name="upload_session")
    op.drop_column("upload_session", "asset_id")
    op.create_index("ix_upload_session_owner_id", "upload_session", ["owner_id"])

    op.add_column(
        "monitoring_plan",
        sa.Column("precision", sa.String(8), nullable=False, server_default="00"),
    )
    op.drop_constraint(
        "fk_monitoring_plan_category_id_category", "monitoring_plan", type_="foreignkey"
    )
    op.drop_column("monitoring_plan", "category_id")

    op.drop_constraint(
        "uq_monitoring_run_input_asset", "monitoring_run_input", type_="unique"
    )
    op.drop_constraint(
        "fk_monitoring_run_input_asset_id_data_asset",
        "monitoring_run_input",
        type_="foreignkey",
    )
    op.add_column(
        "monitoring_run_input",
        sa.Column("owner_kind", sa.String(16), nullable=True),
    )
    op.add_column("monitoring_run_input", sa.Column("record_id", sa.Integer(), nullable=True))
    op.execute("DELETE FROM monitoring_run_input")
    op.alter_column("monitoring_run_input", "owner_kind", nullable=False)
    op.alter_column("monitoring_run_input", "record_id", nullable=False)
    op.drop_column("monitoring_run_input", "asset_id")
    op.create_unique_constraint(
        "uq_monitoring_run_input_record",
        "monitoring_run_input",
        ["run_id", "owner_kind", "record_id"],
    )

    op.drop_table("vector_feature")
    op.drop_table("ecological_parameter_resource_mapping")
    op.drop_table("data_asset")
    op.drop_table("category")


def downgrade() -> None:
    raise NotImplementedError("0014 为破坏性迁移，不支持自动降级")
