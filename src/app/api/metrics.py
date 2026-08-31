"""Prometheus 基线指标。

本模块暴露请求计数与延迟直方图；队列、Job、存储类指标由 operational_metrics 补充。
指标对象为模块级单例，避免进程内重复注册。
"""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUEST_COUNT = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method", "path"],
)

OPERATIONAL_COLLECTOR_UP = Gauge(
    "remote_scene_operational_collector_up", "运维指标采集状态", ["component"]
)
OUTBOX_BACKLOG = Gauge("remote_scene_outbox_backlog", "待投递或已认领的 Outbox 事件数")
RABBITMQ_QUEUE_DEPTH = Gauge(
    "remote_scene_rabbitmq_queue_depth", "RabbitMQ geo 队列消息数", ["state"]
)
JOBS_BY_STATUS = Gauge("remote_scene_jobs", "当前 Job 数", ["status"])
JOB_FAILURES_24H = Gauge("remote_scene_job_failures_24h", "最近 24 小时失败 Job 数")
JOB_DURATION = Gauge("remote_scene_job_duration_seconds", "已完成 Job 处理时长", ["aggregation"])
WORKER_UTILIZATION = Gauge(
    "remote_scene_worker_utilization_ratio", "运行中 Job 数 / RabbitMQ geo 消费者数"
)
WORKER_CONSUMERS = Gauge("remote_scene_worker_consumers", "RabbitMQ geo 队列消费者数")
STORAGE_BYTES = Gauge("remote_scene_storage_bytes", "数据库登记的 MinIO 对象字节数", ["kind"])
CLEANUP_BACKLOG = Gauge("remote_scene_cleanup_backlog", "待执行或重试中的对象清理任务数")
