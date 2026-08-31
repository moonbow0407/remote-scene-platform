"""Job 领域枚举。

状态语义（与《阶段迁移实施方案》§7 一致）：
- PENDING：已与 Outbox 同事务落库，等待 Dispatcher 投递；
- QUEUED：消息已发布到 RabbitMQ，等待 Worker 认领；
- RUNNING：Worker 执行中；
- RETRYING：瞬时错误，按指数退避等待重新投递；
- NEEDS_INPUT：缺少 CRS 等可人工补充信息，暂停等待用户输入；
- SUCCEEDED / FAILED / CANCELLED：终态；MISSED 仅由 Scheduler（Stage 5）产生。
"""

from enum import StrEnum


class JobType(StrEnum):
    """任务类型：栅格入库、矢量入库、附件入库、监测执行。"""

    RASTER_INGESTION = "RASTER_INGESTION"
    VECTOR_INGESTION = "VECTOR_INGESTION"
    ATTACHMENT_INGESTION = "ATTACHMENT_INGESTION"
    # 监测执行：无单版本引用（多版本输入快照见 monitoring_run_input），
    # 由监测模块经 RunDispatcher 接缝同事务创建，Geo Worker 中的
    # monitoring.execute_run 任务认领执行
    MONITORING_RUN = "MONITORING_RUN"


TASK_BY_JOB_TYPE: dict[JobType, str] = {
    JobType.RASTER_INGESTION: "processing.ingest_raster",
    JobType.VECTOR_INGESTION: "processing.ingest_vector",
    JobType.ATTACHMENT_INGESTION: "processing.ingest_attachment",
    JobType.MONITORING_RUN: "monitoring.execute_run",
}


class JobStatus(StrEnum):
    """任务状态。

    PENDING 待投递、QUEUED 已入队、RUNNING 执行中、RETRYING 退避重试、
    NEEDS_INPUT 待补输入、SUCCEEDED 成功、FAILED 失败、
    CANCEL_REQUESTED 取消中、CANCELLED 已取消、MISSED 错过周期。
    """

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    NEEDS_INPUT = "NEEDS_INPUT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    MISSED = "MISSED"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
