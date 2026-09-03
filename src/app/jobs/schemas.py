"""后台任务查询。管理页面请轮询资产状态，不必使用本接口。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.jobs.enums import JobStatus, JobType


class JobEventItem(BaseModel):
    """任务状态变化的一条记录。"""

    model_config = ConfigDict(title="任务事件")

    event_type: str = Field(description="事件种类，例如改状态、重试、取消")
    from_status: JobStatus | None = Field(description="变化前的状态；刚创建时可为空")
    to_status: JobStatus | None = Field(description="变化后的状态")
    detail: dict[str, Any] | None = Field(description="附加说明，失败时可能含错误信息")
    created_at: datetime = Field(description="发生时间，UTC 且带时区")


class JobResponse(BaseModel):
    """一条后台任务的进度。"""

    model_config = ConfigDict(title="任务详情")

    job_id: int = Field(description="任务编号")
    job_type: JobType = Field(description="任务种类")
    status: JobStatus = Field(description="当前状态")
    attempt: int = Field(description="已经尝试的次数，第一次为 0")
    max_attempts: int = Field(description="最多尝试几次")
    payload: dict[str, Any] = Field(description="任务参数。入库任务里会有 owner_kind / owner_id")
    last_error: dict[str, Any] | None = Field(description="最近一次失败说明；成功时为空")
    queued_at: datetime | None = Field(description="进入队列的时间；尚未排队为空")
    started_at: datetime | None = Field(description="开始执行的时间；尚未开始为空")
    finished_at: datetime | None = Field(description="结束时间；尚未结束为空")
    events: list[JobEventItem] = Field(description="状态变化记录，最多 200 条")
    poll_hint: str = Field(description="建议的再次查询地址，可以忽略")


class CancelJobResponse(BaseModel):
    """取消任务后的状态。"""

    model_config = ConfigDict(title="取消任务结果")

    job_id: int = Field(description="任务编号")
    status: JobStatus = Field(
        description="取消后的状态。还在排队的会变成 CANCELLED；已经在跑的先变成 CANCEL_REQUESTED"
    )
