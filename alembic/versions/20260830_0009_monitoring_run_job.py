"""监测执行任务接线：放宽 Job 单版本引用（MONITORING_RUN 无 asset_version_id）。

job_type 为 String(32)（迁移 0002，未建 CHECK 约束），新增 MONITORING_RUN
取值无需类型 DDL；本迁移仅需放开 asset_version_id 的 NOT NULL——入库任务
仍恒有单版本引用（服务层已加不变量校验），监测执行的权威输入关联在
monitoring_run_input（迁移 0008，版本行 RESTRICT）。

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "job",
        "asset_version_id",
        existing_type=sa.Integer(),
        nullable=True,
        comment=(
            "入库任务（RASTER/VECTOR/ATTACHMENT_INGESTION）的唯一目标版本；"
            "MONITORING_RUN 为 NULL（多版本输入见 monitoring_run_input）"
        ),
    )


def downgrade() -> None:
    # 回滚前提：库中不存在 asset_version_id 为 NULL 的 MONITORING_RUN 任务残留
    op.alter_column("job", "asset_version_id", existing_type=sa.Integer(), nullable=False)
