"""Stage 6：资产软删除、恢复期与可重试对象清理。

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_asset",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
    )
    op.add_column(
        "data_asset",
        sa.Column(
            "purge_after", sa.DateTime(timezone=True), nullable=True, comment="恢复期结束时间"
        ),
    )
    op.add_column("data_asset", sa.Column("deleted_by", sa.Integer(), nullable=True))
    op.add_column(
        "data_asset",
        sa.Column("purge_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "data_asset", sa.Column("purge_next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("data_asset", sa.Column("purge_last_error", sa.Text(), nullable=True))
    op.create_index(
        "ix_data_asset_purge_due",
        "data_asset",
        ["deleted_at", "purge_after", "purge_next_attempt_at"],
    )

    op.create_table(
        "object_cleanup_task",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False, comment="BLOB/ARTIFACT"),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("blob_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("object_key", name="uq_object_cleanup_task_object_key"),
    )
    op.create_index(
        "ix_object_cleanup_due",
        "object_cleanup_task",
        ["status", "next_attempt_at", "created_at"],
    )

    # 高频检索/运维扫描的复合索引；空间列已有 GiST，Scheduler 已有 due-time 索引。
    op.create_index(
        "ix_asset_version_search",
        "asset_version",
        ["status", "acquired_at", "created_at"],
    )
    op.create_index("ix_job_status_finished", "job", ["status", "finished_at"])


def downgrade() -> None:
    op.drop_index("ix_job_status_finished", table_name="job")
    op.drop_index("ix_asset_version_search", table_name="asset_version")
    op.drop_index("ix_object_cleanup_due", table_name="object_cleanup_task")
    op.drop_table("object_cleanup_task")
    op.drop_index("ix_data_asset_purge_due", table_name="data_asset")
    op.drop_column("data_asset", "purge_last_error")
    op.drop_column("data_asset", "purge_next_attempt_at")
    op.drop_column("data_asset", "purge_attempts")
    op.drop_column("data_asset", "deleted_by")
    op.drop_column("data_asset", "purge_after")
    op.drop_column("data_asset", "deleted_at")
