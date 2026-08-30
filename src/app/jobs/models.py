"""Job、JobEvent 与 OutboxEvent 持久化模型。

不变量：
- Job 与 Outbox 事件必须在同一数据库事务中创建（见 jobs.service.create_job_with_outbox）；
- PostgreSQL 是任务状态权威来源，RabbitMQ 只负责传递；
- 状态转换只能经 JobService（状态机校验 + 事件追加），禁止直接改状态字段。
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.context import now_utc
from app.db import Base, TimestampMixin
from app.jobs.enums import JobStatus, JobType, OutboxStatus


class Job(Base, TimestampMixin):
    """处理任务。引用具体 asset_version，不能只引用逻辑资产。"""

    __tablename__ = "job"

    id: Mapped[Any] = mapped_column(sa.Uuid, primary_key=True)
    job_type: Mapped[JobType] = mapped_column(
        sa.Enum(JobType, native_enum=False, length=32),
        nullable=False,
        comment="任务类型：RASTER_INGESTION/VECTOR_INGESTION/ATTACHMENT_INGESTION",
    )
    status: Mapped[JobStatus] = mapped_column(
        sa.Enum(JobStatus, native_enum=False, length=32),
        nullable=False,
        default=JobStatus.PENDING,
        index=True,
        comment=(
            "PENDING/QUEUED/RUNNING/RETRYING/NEEDS_INPUT/SUCCEEDED/FAILED/"
            "CANCEL_REQUESTED/CANCELLED/MISSED"
        ),
    )
    # 业务参数：asset_version_id、upload_session_id 等
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempt: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=4)
    # 最近一次错误的诊断（确定性/瞬时/缺输入），JSON 结构：{code, detail, transient}
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # 入库任务（RASTER/VECTOR/ATTACHMENT_INGESTION）的唯一目标版本；
    # MONITORING_RUN 为 NULL——监测执行是多版本输入快照，权威关联在
    # monitoring_run_input（Run 引用的版本行受 RESTRICT 保护，语义不弱化）
    asset_version_id: Mapped[Any] = mapped_column(
        sa.Uuid, ForeignKey("asset_version.id", ondelete="CASCADE"), nullable=True, index=True
    )
    queued_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # 执行租约：Worker 认领时取得 token 并持续心跳续约；租约过期即执行者失联，
    # 由独立恢复器回收重投。不能依赖"下一条 Broker 重投消息恰好到达"——Worker
    # 崩溃后消息已被 ACK，租约过期是唯一可靠的执行权回收信号。
    lease_token: Mapped[Any] = mapped_column(
        sa.Uuid, nullable=True, comment="执行租约令牌；心跳续约与恢复回收均按 token 校验归属"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True, comment="租约到期时间；超过即视为执行者失联"
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True, comment="执行者最近一次续约时间"
    )

    __table_args__ = (
        # 恢复器扫描：RUNNING 且租约过期
        sa.Index("ix_job_status_lease_expires", "status", "lease_expires_at"),
        sa.Index("ix_job_status_finished", "status", "finished_at"),
    )


class JobEvent(Base):
    """Job 事件流：状态转换与关键步骤的审计记录，为二期 SSE 预留边界。"""

    __tablename__ = "job_event"

    id: Mapped[Any] = mapped_column(sa.Uuid, primary_key=True)
    job_id: Mapped[Any] = mapped_column(
        sa.Uuid, ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    from_status: Mapped[JobStatus | None] = mapped_column(
        sa.Enum(JobStatus, native_enum=False, length=32), nullable=True
    )
    to_status: Mapped[JobStatus | None] = mapped_column(
        sa.Enum(JobStatus, native_enum=False, length=32), nullable=True
    )
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (sa.Index("ix_job_event_job_created", "job_id", "created_at"),)


class OutboxEvent(Base, TimestampMixin):
    """Transactional Outbox：与业务写入同事务提交，由 Dispatcher 至少一次投递。"""

    __tablename__ = "outbox_event"

    id: Mapped[Any] = mapped_column(sa.Uuid, primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    aggregate_id: Mapped[Any] = mapped_column(sa.Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # 投递载荷：{"task": 任务名, "args": [...], "job_id": ...}
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        sa.Enum(OutboxStatus, native_enum=False, length=16),
        nullable=False,
        default=OutboxStatus.PENDING,
        comment="PENDING/CLAIMED/PUBLISHED/FAILED",
    )
    attempts: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # 发布失败指数退避：早于该时间的事件不会被认领
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Dispatcher 认领查询：按状态 + 可认领时间扫描
        sa.Index("ix_outbox_status_next_attempt", "status", "next_attempt_at"),
    )
