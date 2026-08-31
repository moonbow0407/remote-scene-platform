"""Stage 5 监测计划、occurrence、执行与输入快照。

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monitoring_plan",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, comment="ACTIVE/PAUSED"),
        sa.Column(
            "boundary",
            Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
            nullable=False,
            comment="计划边界，EPSG:4326 MULTIPOLYGON",
        ),
        sa.Column(
            "boundary_wkt",
            sa.Text(),
            nullable=False,
            comment="boundary 的 EPSG:4326 WKT 文本（MULTIPOLYGON）",
        ),
        sa.Column("schedule_type", sa.String(16), nullable=False),
        sa.Column(
            "schedule_expression",
            sa.String(256),
            nullable=False,
            comment="INTERVAL：ISO 8601 duration 子集；RRULE：RFC 5545 表达式",
        ),
        sa.Column("timezone", sa.String(64), nullable=False, comment="IANA 时区名"),
        sa.Column("resource_catalog_id", sa.Integer(), nullable=True),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="下一次计划触发时刻（UTC 网格点）",
        ),
        sa.Column(
            "last_successful_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次成功执行的 scheduled_for",
        ),
        sa.Column("created_by", sa.Integer(), nullable=True, comment="鉴权预留：创建者"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["resource_catalog_id"],
            ["resource_catalog.id"],
            ondelete="RESTRICT",
            name="fk_monitoring_plan_resource_catalog_id_resource_catalog",
        ),
    )
    op.create_index("ix_monitoring_plan_status", "monitoring_plan", ["status"])
    op.create_index(
        "ix_monitoring_plan_status_next_run",
        "monitoring_plan",
        ["status", "next_run_at"],
    )

    op.create_table(
        "monitoring_plan_parameter",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), nullable=False, comment="所属监测计划"),
        sa.Column("ecological_parameter_id", sa.Integer(), nullable=False, comment="生态参数主键"),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["monitoring_plan.id"],
            ondelete="CASCADE",
            name="fk_monitoring_plan_parameter_plan_id_monitoring_plan",
        ),
        sa.ForeignKeyConstraint(
            ["ecological_parameter_id"],
            ["ecological_parameter.id"],
            ondelete="RESTRICT",
            name="fk_monitoring_plan_parameter_parameter_id_ecological_parameter",
        ),
        sa.UniqueConstraint(
            "plan_id", "ecological_parameter_id", name="uq_monitoring_plan_parameter"
        ),
    )
    op.create_index("ix_monitoring_plan_parameter_plan", "monitoring_plan_parameter", ["plan_id"])
    op.create_index(
        "ix_monitoring_plan_parameter_parameter",
        "monitoring_plan_parameter",
        ["ecological_parameter_id"],
    )

    op.create_table(
        "monitoring_occurrence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), nullable=False, comment="所属监测计划"),
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="本周期计划时刻（UTC）",
        ),
        sa.Column("trigger", sa.String(16), nullable=False, comment="SCHEDULED/MANUAL"),
        sa.Column("status", sa.String(16), nullable=False, comment="DISPATCHED/MISSED"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["monitoring_plan.id"],
            ondelete="CASCADE",
            name="fk_monitoring_occurrence_plan_id_monitoring_plan",
        ),
        # Scheduler 多实例/重复扫描/重启/手动触发的幂等兜底：同一计划时刻至多一条
        sa.UniqueConstraint("plan_id", "scheduled_for", name="uq_monitoring_occurrence_scheduled"),
    )

    op.create_table(
        "monitoring_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), nullable=False, comment="所属监测计划"),
        sa.Column("occurrence_id", sa.Integer(), nullable=False, comment="触发的 occurrence；1:1"),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="PENDING/RUNNING/SUCCEEDED/FAILED",
        ),
        sa.Column(
            "window_anchor",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="增量窗口上界（选择时刻，UTC）",
        ),
        sa.Column(
            "job_id",
            sa.Integer(),
            nullable=True,
            comment="派发的 Job 主键；首版 dispatch adapter 未接线时为 NULL",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("diagnostics", JSONB, nullable=True, comment="失败诊断 {code, detail}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["monitoring_plan.id"],
            ondelete="CASCADE",
            name="fk_monitoring_run_plan_id_monitoring_plan",
        ),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["monitoring_occurrence.id"],
            ondelete="CASCADE",
            name="fk_monitoring_run_occurrence_id_monitoring_occurrence",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            ondelete="SET NULL",
            name="fk_monitoring_run_job_id_job",
        ),
        sa.UniqueConstraint("occurrence_id", name="uq_monitoring_run_occurrence"),
    )
    op.create_index("ix_monitoring_run_status", "monitoring_run", ["status"])
    op.create_index(
        "ix_monitoring_run_plan_status_anchor",
        "monitoring_run",
        ["plan_id", "status", "window_anchor"],
    )

    op.create_table(
        "monitoring_run_input",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False, comment="所属监测执行"),
        sa.Column(
            "asset_id",
            sa.Integer(),
            nullable=False,
            comment="逻辑资产主键（与 version.asset_id 同源，便于直接查询）",
        ),
        sa.Column(
            "asset_version_id",
            sa.Integer(),
            nullable=False,
            comment="冻结的资产版本主键；Run 创建后不可变",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["monitoring_run.id"],
            ondelete="CASCADE",
            name="fk_monitoring_run_input_run_id_monitoring_run",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["data_asset.id"],
            ondelete="RESTRICT",
            name="fk_monitoring_run_input_asset_id_data_asset",
        ),
        sa.ForeignKeyConstraint(
            ["asset_version_id"],
            ["asset_version.id"],
            ondelete="RESTRICT",
            name="fk_monitoring_run_input_asset_version_id_asset_version",
        ),
        sa.UniqueConstraint("run_id", "asset_version_id", name="uq_monitoring_run_input_version"),
    )
    op.create_index("ix_monitoring_run_input_run", "monitoring_run_input", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_monitoring_run_input_run", table_name="monitoring_run_input")
    op.drop_table("monitoring_run_input")
    op.drop_index("ix_monitoring_run_plan_status_anchor", table_name="monitoring_run")
    op.drop_index("ix_monitoring_run_status", table_name="monitoring_run")
    op.drop_table("monitoring_run")
    op.drop_table("monitoring_occurrence")
    op.drop_index("ix_monitoring_plan_parameter_parameter", table_name="monitoring_plan_parameter")
    op.drop_index("ix_monitoring_plan_parameter_plan", table_name="monitoring_plan_parameter")
    op.drop_table("monitoring_plan_parameter")
    op.drop_index("ix_monitoring_plan_status_next_run", table_name="monitoring_plan")
    op.drop_index("ix_monitoring_plan_status", table_name="monitoring_plan")
    op.drop_table("monitoring_plan")
