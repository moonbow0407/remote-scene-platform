"""Stage 2 栅格纵向闭环数据模型

对象 blob、逻辑资产、不可变版本、栅格扩展、工件、上传会话、Job、JobEvent 与 Outbox。
data_asset.current_version_id 与 asset_version.asset_id 互为外键，用后置 ALTER 处理。

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = sa.dialects.postgresql.JSONB


def upgrade() -> None:
    op.create_table(
        "object_blob",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reference_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("sha256", name="uq_object_blob_sha256"),
    )

    op.create_table(
        "data_asset",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "asset_type",
            sa.String(16),
            nullable=False,
            comment="物理类型：RASTER/VECTOR/ATTACHMENT",
        ),
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            comment="来源：UPLOAD/SATELLITE/EXTERNAL_IMPORT",
        ),
        sa.Column("properties", JSONB, nullable=False, server_default="{}"),
        sa.Column("owner_id", sa.Integer(), nullable=True, comment="鉴权预留，首版为 NULL"),
        sa.Column("created_by", sa.Integer(), nullable=True, comment="鉴权预留，首版为 NULL"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_data_asset_asset_type", "data_asset", ["asset_type"])

    op.create_table(
        "asset_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.Integer(),
            sa.ForeignKey(
                "data_asset.id", ondelete="CASCADE", name="fk_asset_version_asset_id_data_asset"
            ),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="UPLOADING/VALIDATING/PROCESSING/NEEDS_INPUT/READY/FAILED/DELETED",
        ),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("original_file_name", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("properties", JSONB, nullable=False, server_default="{}"),
        sa.Column("diagnostics", JSONB, nullable=True),
        sa.Column(
            "blob_id",
            sa.Integer(),
            sa.ForeignKey(
                "object_blob.id", ondelete="RESTRICT", name="fk_asset_version_blob_id_object_blob"
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("asset_id", "version_number", name="uq_asset_version_number"),
    )
    op.create_index("ix_asset_version_asset_id", "asset_version", ["asset_id"])
    op.create_index("ix_asset_version_status", "asset_version", ["status"])
    op.create_index("ix_asset_version_blob_id", "asset_version", ["blob_id"])

    # 环形外键：当前版本指针
    op.add_column("data_asset", sa.Column("current_version_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_data_asset_current_version_id_asset_version",
        "data_asset",
        "asset_version",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "raster_asset_version",
        sa.Column(
            "asset_version_id",
            sa.Integer(),
            sa.ForeignKey(
                "asset_version.id",
                ondelete="CASCADE",
                name="fk_raster_ext_version_id_asset_version",
            ),
            primary_key=True,
        ),
        sa.Column("crs", sa.String(128), nullable=True),
        sa.Column(
            "user_crs", sa.String(128), nullable=True, comment="用户经 NEEDS_INPUT 流程补充的 CRS"
        ),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("band_count", sa.Integer(), nullable=True),
        sa.Column("bands", JSONB, nullable=True, comment="波段明细与统计"),
        sa.Column("resolution_x", sa.Numeric(18, 10), nullable=True),
        sa.Column("resolution_y", sa.Numeric(18, 10), nullable=True),
        sa.Column("nodata", sa.Float(), nullable=True),
        sa.Column("render_profile", JSONB, nullable=True, comment="渲染推断 {mode, bands}"),
        sa.Column("footprint", Geometry(geometry_type="POLYGON", srid=4326), nullable=True),
        sa.Column("min_x", sa.Float(), nullable=True),
        sa.Column("min_y", sa.Float(), nullable=True),
        sa.Column("max_x", sa.Float(), nullable=True),
        sa.Column("max_y", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_raster_footprint", "raster_asset_version", ["footprint"], postgresql_using="gist"
    )

    op.create_table(
        "asset_artifact",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "asset_version_id",
            sa.Integer(),
            sa.ForeignKey(
                "asset_version.id", ondelete="CASCADE", name="fk_artifact_version_id_asset_version"
            ),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False, comment="ORIGINAL/COG/THUMBNAIL"),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("asset_version_id", "kind", name="uq_artifact_version_kind"),
    )
    op.create_index("ix_asset_artifact_asset_version_id", "asset_artifact", ["asset_version_id"])

    op.create_table(
        "upload_session",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.Integer(),
            sa.ForeignKey(
                "data_asset.id", ondelete="CASCADE", name="fk_upload_session_asset_id_data_asset"
            ),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, comment="PENDING/COMPLETED/ABORTED"),
        sa.Column("minio_upload_id", sa.String(256), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("file_name", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("completed_version_id", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_upload_session_asset_id", "upload_session", ["asset_id"])

    op.create_table(
        "job",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_type", sa.String(32), nullable=False, comment="任务类型，首版 RASTER_INGESTION"
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            comment="PENDING/QUEUED/RUNNING/RETRYING/NEEDS_INPUT/SUCCEEDED/FAILED/CANCEL_REQUESTED/CANCELLED/MISSED",
        ),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.SmallInteger(), nullable=False, server_default="4"),
        sa.Column("last_error", JSONB, nullable=True),
        sa.Column(
            "asset_version_id",
            sa.Integer(),
            sa.ForeignKey(
                "asset_version.id", ondelete="CASCADE", name="fk_job_version_id_asset_version"
            ),
            nullable=False,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_job_status", "job", ["status"])
    op.create_index("ix_job_asset_version_id", "job", ["asset_version_id"])

    op.create_table(
        "job_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("job.id", ondelete="CASCADE", name="fk_job_event_job_id_job"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=True),
        sa.Column("detail", JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_job_event_job_created", "job_event", ["job_id", "created_at"])

    op.create_table(
        "outbox_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("aggregate_type", sa.String(32), nullable=False),
        sa.Column("aggregate_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, comment="PENDING/CLAIMED/PUBLISHED/FAILED"
        ),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_outbox_status_next_attempt", "outbox_event", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("outbox_event")
    op.drop_table("job_event")
    op.drop_table("job")
    op.drop_table("upload_session")
    op.drop_table("asset_artifact")
    op.drop_index("ix_raster_footprint", table_name="raster_asset_version")
    op.drop_table("raster_asset_version")
    op.drop_constraint(
        "fk_data_asset_current_version_id_asset_version", "data_asset", type_="foreignkey"
    )
    op.drop_column("data_asset", "current_version_id")
    op.drop_table("asset_version")
    op.drop_table("data_asset")
    op.drop_table("object_blob")
