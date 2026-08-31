"""任务查询 API 模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.jobs.enums import JobStatus, JobType


class JobEventItem(BaseModel):
    event_type: str = Field(description="事件类型，如状态迁移、重试、取消")
    from_status: JobStatus | None = Field(description="变更前状态；创建事件可为空")
    to_status: JobStatus | None = Field(description="变更后状态")
    detail: dict[str, Any] | None = Field(description="事件附加信息，如失败诊断 JSON")
    created_at: datetime = Field(description="事件时间（UTC，带时区）")


class JobResponse(BaseModel):
    job_id: UUID = Field(description="任务 ID")
    job_type: JobType = Field(
        description="任务类型：RASTER_INGESTION 栅格入库、VECTOR_INGESTION 矢量入库、"
        "ATTACHMENT_INGESTION 附件入库、MONITORING_RUN 监测执行"
    )
    status: JobStatus = Field(
        description="任务状态：PENDING 待投递、QUEUED 已入队、RUNNING 执行中、RETRYING 退避重试、"
        "NEEDS_INPUT 待补输入、SUCCEEDED 成功、FAILED 失败、CANCEL_REQUESTED 取消中、"
        "CANCELLED 已取消、MISSED 错过周期"
    )
    attempt: int = Field(description="当前尝试次数，首次为 0")
    max_attempts: int = Field(description="最大尝试次数")
    payload: dict[str, Any] = Field(description="任务参数快照，如资产版本 ID")
    last_error: dict[str, Any] | None = Field(
        description="最近一次失败诊断 JSON，含 code / detail / transient；成功时为空"
    )
    queued_at: datetime | None = Field(description="进入队列时间（UTC，带时区）")
    started_at: datetime | None = Field(description="开始执行时间（UTC，带时区）")
    finished_at: datetime | None = Field(description="结束时间（UTC，带时区）")
    events: list[JobEventItem] = Field(description="状态事件时间线，最多 200 条")
    poll_hint: str = Field(description="建议的轮询地址，客户端可忽略")


class CancelJobResponse(BaseModel):
    job_id: UUID = Field(description="任务 ID")
    status: JobStatus = Field(
        description=(
            "取消后的状态：排队任务为 CANCELLED；运行中为 CANCEL_REQUESTED，稍后在检查点收敛"
        ),
    )
