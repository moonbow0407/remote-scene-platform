"""监测计划、occurrence、执行与输入快照持久化模型。

不变量：
- MonitoringPlan 是长期配置（边界 + 目录/生态约束 + 调度规则）；MonitoringRun 是
  一次 occurrence 的执行实例，二者分离，不把 last_result/current_job 塞回计划；
- (plan_id, scheduled_for) 在 monitoring_occurrence 上数据库唯一：多实例
  Scheduler、重复扫描、进程重启、手动触发与调度竞争，都不能为同一计划时刻
  重复创建执行——幂等靠数据库约束，不靠 Python if；
- MonitoringRunInput 冻结具体 asset_version_id，Run 创建后输入集合不可变；
  后续资产新增版本不改变历史 Run 的输入（对版本/资产行用 RESTRICT，
  物理清理必须先确认无 Run 快照引用，与 blob 引用计数同一原则）；
- 全部时刻 UTC 持久化（timestamptz）。
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, TimestampMixin
from app.monitoring.enums import (
    OccurrenceStatus,
    OccurrenceTrigger,
    PlanStatus,
    RunStatus,
    ScheduleType,
)


class MonitoringPlan(Base, TimestampMixin):
    """监测计划：一个空间范围 + 资源目录约束 + 生态参数约束 + 调度周期。"""

    __tablename__ = "monitoring_plan"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[PlanStatus] = mapped_column(
        sa.Enum(PlanStatus, native_enum=False, length=16),
        nullable=False,
        default=PlanStatus.ACTIVE,
        index=True,
        comment="ACTIVE 参与调度；PAUSED 暂停（计划删除为物理删除，软删除仅用于资产）",
    )
    # 边界 PostGIS 几何（EPSG:4326，统一归一化为 MULTIPOLYGON），供 ST_Intersects
    # 选择资产；spatial_index=False：计划数量级小且从不按边界反查计划
    boundary: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=False,
        comment="计划边界，EPSG:4326 MULTIPOLYGON",
    )
    # 边界 WKT 文本：选择查询的可移植读取来源（geometry 列回读为 WKB，需 ST_AsText
    # 才能取回文本）；由 Service 在写入时与 boundary 同源生成，两者恒一致
    boundary_wkt: Mapped[str] = mapped_column(
        Text, nullable=False, comment="boundary 的 EPSG:4326 WKT 文本（MULTIPOLYGON）"
    )
    schedule_type: Mapped[ScheduleType] = mapped_column(
        sa.Enum(ScheduleType, native_enum=False, length=16), nullable=False
    )
    # INTERVAL 存 ISO 8601 duration 子集（如 PT6H/P1D）；RRULE 存 RFC 5545 表达式
    # （不含 DTSTART，网格锚点由系统控制）
    schedule_expression: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    # IANA 时区名（如 Asia/Shanghai）；RRULE 的时刻在此时区生成后转 UTC
    timezone: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    category_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("category.id", ondelete="RESTRICT"),
        nullable=True,
        comment="分类约束；空表示不限",
    )
    # 下一次计划触发时刻（UTC 网格点）；PAUSED 保留旧值但不参与调度，
    # RRULE 周期耗尽为 NULL（计划自然停摆）
    next_run_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # 最近一次成功执行的 scheduled_for（展示用）；增量窗口锚点以成功 Run 的
    # window_anchor 为准（见 MonitoringRun）
    last_successful_run_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, comment="鉴权预留：创建者"
    )

    parameters: Mapped[list["MonitoringPlanParameter"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Scheduler 到期扫描：ACTIVE 且 next_run_at <= now
        sa.Index("ix_monitoring_plan_status_next_run", "status", "next_run_at"),
    )


class MonitoringPlanParameter(Base):
    """计划 ↔ 生态参数 关联表；禁止逗号分隔 ID 或名称软引用。"""

    __tablename__ = "monitoring_plan_parameter"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("monitoring_plan.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属监测计划",
    )
    ecological_parameter_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("ecological_parameter.id", ondelete="RESTRICT"),
        nullable=False,
        comment="生态参数主键",
    )

    plan: Mapped[MonitoringPlan] = relationship(back_populates="parameters")

    __table_args__ = (
        UniqueConstraint("plan_id", "ecological_parameter_id", name="uq_monitoring_plan_parameter"),
        sa.Index("ix_monitoring_plan_parameter_plan", "plan_id"),
        sa.Index("ix_monitoring_plan_parameter_parameter", "ecological_parameter_id"),
    )


class MonitoringOccurrence(Base):
    """一次计划触发的稳定唯一标识：(plan_id, scheduled_for) 数据库唯一。

    Scheduler 多实例/重复扫描/重启、手动触发与调度竞争最终都收敛到本表的唯一
    约束上——同一计划时刻至多产生一条 occurrence，至多派发一次执行。
    """

    __tablename__ = "monitoring_occurrence"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("monitoring_plan.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属监测计划",
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, comment="本周期计划时刻（UTC）"
    )
    trigger: Mapped[OccurrenceTrigger] = mapped_column(
        sa.Enum(OccurrenceTrigger, native_enum=False, length=16), nullable=False
    )
    status: Mapped[OccurrenceStatus] = mapped_column(
        sa.Enum(OccurrenceStatus, native_enum=False, length=16),
        nullable=False,
        comment="DISPATCHED 已生成执行；MISSED 停机错过仅留审计",
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "scheduled_for", name="uq_monitoring_occurrence_scheduled"),
    )


class MonitoringRun(Base, TimestampMixin):
    """监测执行：某次 occurrence 的执行实例，引用不可变输入快照。"""

    __tablename__ = "monitoring_run"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("monitoring_plan.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属监测计划",
    )
    occurrence_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("monitoring_occurrence.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        comment="触发本执行的 occurrence；1:1",
    )
    status: Mapped[RunStatus] = mapped_column(
        sa.Enum(RunStatus, native_enum=False, length=16),
        nullable=False,
        default=RunStatus.PENDING,
        index=True,
        comment="PENDING/RUNNING/SUCCEEDED/FAILED；转换只能经 MonitoringService",
    )
    # 增量窗口锚点 = 本次执行的选择时刻（UTC）。下一次执行的选择窗口以此为下界，
    # 因此"创建于选择时刻之后的版本"必然落入下一窗口，任何版本至多被选中一次；
    # 失败的 Run 不推进锚点，其数据会被下一次执行重选
    window_anchor: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, comment="增量窗口上界（选择时刻，UTC）"
    )
    # 派发的执行任务主键；Job 生命周期归 jobs 模块，删除时置空不连带删除快照。
    # 派发由 JobRunDispatcher 在 Run 创建的同一事务中完成，正常恒非空
    job_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("job.id", ondelete="SET NULL"),
        nullable=True,
        comment="派发的 MONITORING_RUN Job 主键（与 Run 同事务创建）",
    )
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # 失败诊断：{code, detail}，结构对齐 Job.last_error
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    inputs: Mapped[list["MonitoringRunInput"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # 增量锚点查询：某计划最近一次 SUCCEEDED Run 的 window_anchor
        sa.Index("ix_monitoring_run_plan_status_anchor", "plan_id", "status", "window_anchor"),
    )


class MonitoringRunInput(Base):
    """输入快照明细：Run 引用的具体资产集合，创建后不可变。"""

    __tablename__ = "monitoring_run_input"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("monitoring_run.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属监测执行",
    )
    asset_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("data_asset.id", ondelete="RESTRICT"),
        nullable=False,
        comment="冻结的资产主键；Run 创建后不可变",
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    run: Mapped[MonitoringRun] = relationship(back_populates="inputs")

    __table_args__ = (
        UniqueConstraint("run_id", "asset_id", name="uq_monitoring_run_input_asset"),
        sa.Index("ix_monitoring_run_input_run", "run_id"),
    )
