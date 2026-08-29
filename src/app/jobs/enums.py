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
    RASTER_INGESTION = "RASTER_INGESTION"


class JobStatus(StrEnum):
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
