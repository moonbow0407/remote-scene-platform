"""应用配置。

所有配置来自环境变量（`APP_` 前缀）；机密一律经环境注入，不写入代码。
配置缺失或不合法时在进程启动期快速失败，并给出可定位的中文诊断。
"""

from functools import lru_cache
from typing import Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行配置。字段名即环境变量名（去掉 APP_ 前缀、不区分大小写）。"""

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    env: str = "local"
    log_level: str = "INFO"

    # PostgreSQL：唯一关系与空间数据库
    database_url: str = "postgresql+psycopg://app:app@db:5432/remote_scene"
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # MinIO：不可变二进制对象存储
    minio_endpoint: str = "minio:9000"
    # 预签名 URL 面向浏览器等外部客户端的端点（本机开发经端口映射为 localhost:9000）；
    # 签名覆盖主机名，服务端内部操作仍使用 minio_endpoint
    minio_public_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "remote-scene"
    minio_secure: bool = False

    # RabbitMQ：仅负责消息传递；任务状态权威在 PostgreSQL
    rabbitmq_url: str = "amqp://app:app@rabbitmq:5672/"
    rabbitmq_management_url: str = "http://rabbitmq:15672"
    rabbitmq_management_user: str = "app"
    rabbitmq_management_password: str = "app"

    # TiTiler：内部瓦片服务，不直接暴露给客户端
    titiler_url: str = "http://titiler:8000"

    readiness_timeout_seconds: float = 3.0

    # 预签名/令牌有效期（秒）
    presign_expiry_seconds: int = 86400
    download_expiry_seconds: int = 3600
    tile_token_ttl_seconds: int = 3600

    # 对外基地址：用于拼接预签名与瓦片模板 URL
    public_base_url: str = "http://localhost:8080"

    # 瓦片令牌签名密钥：生产环境必须经环境注入；未配置时瓦片签发接口拒绝服务
    tile_token_secret: str = ""

    # JWT：Access/Refresh 签名密钥与 TTL。密钥禁止写入源码；生产环境不允许为空。
    jwt_secret: str = ""
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 604800

    # Geo Worker 临时目录（每任务独立子目录，开始前检查可用空间）
    worker_tmp_dir: str = "/data/tmp"
    # 临时空间预检：低于源文件预计占用的该倍数时任务早期失败
    worker_tmp_min_ratio: float = 2.0
    worker_concurrency: int = 2
    # 软时限用于落明确诊断，硬时限兜底杀死失控子进程；后者必须留出清理窗口。
    worker_task_soft_timeout_seconds: int = 21600
    worker_task_hard_timeout_seconds: int = 22200

    # 资产删除后 7 天内可恢复；cleanup 进程分批清理过期资产与 MinIO 对象。
    asset_retention_days: int = 7
    cleanup_poll_seconds: float = 5.0
    cleanup_batch_size: int = 20

    # Job 执行租约：Worker 认领时取得，运行期间由心跳续约；租约过期由独立恢复器
    # 回收重投（不依赖 Broker 重投消息恰好到达）
    job_lease_ttl_seconds: int = 600
    job_heartbeat_interval_seconds: int = 60

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        # 配置错误必须 fail fast 且中文可定位，而不是等到进程启动连接数据库时才暴露
        if not value.startswith(("postgresql+psycopg://", "postgresql://")):
            raise ValueError(
                f"APP_DATABASE_URL 不合法：{value!r}，"
                "应为 postgresql+psycopg://用户名:口令@主机:端口/库名"
            )
        return value

    @field_validator("access_token_ttl_seconds", "refresh_token_ttl_seconds")
    @classmethod
    def _validate_token_ttl(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                "JWT TTL 必须为正整数秒"
                "（APP_ACCESS_TOKEN_TTL_SECONDS / APP_REFRESH_TOKEN_TTL_SECONDS）"
            )
        return value

    @field_validator("job_heartbeat_interval_seconds", "job_lease_ttl_seconds")
    @classmethod
    def _validate_lease_fields(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                "Job 租约配置必须为正整数秒"
                "（APP_JOB_HEARTBEAT_INTERVAL_SECONDS / APP_JOB_LEASE_TTL_SECONDS）"
            )
        return value

    @field_validator(
        "worker_concurrency",
        "worker_task_soft_timeout_seconds",
        "worker_task_hard_timeout_seconds",
        "asset_retention_days",
        "cleanup_batch_size",
    )
    @classmethod
    def _validate_positive_operational_ints(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Worker/生命周期配置必须为正整数")
        return value

    @field_validator("worker_tmp_min_ratio", "cleanup_poll_seconds")
    @classmethod
    def _validate_positive_operational_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("临时空间倍率与清理轮询间隔必须为正数")
        return value

    @model_validator(mode="after")
    def _validate_lease_ttl_beats_heartbeat(self) -> Self:
        # TTL 必须≥若干个心跳周期，否则一次心跳抖动就会导致任务被误回收
        if self.job_lease_ttl_seconds <= self.job_heartbeat_interval_seconds:
            raise ValueError(
                f"APP_JOB_LEASE_TTL_SECONDS（{self.job_lease_ttl_seconds}s）必须大于 "
                f"APP_JOB_HEARTBEAT_INTERVAL_SECONDS（{self.job_heartbeat_interval_seconds}s），"
                "否则心跳抖动会造成任务被误回收"
            )
        return self

    @model_validator(mode="after")
    def _validate_task_timeouts(self) -> Self:
        if self.worker_task_hard_timeout_seconds <= self.worker_task_soft_timeout_seconds:
            raise ValueError(
                "APP_WORKER_TASK_HARD_TIMEOUT_SECONDS 必须大于 "
                "APP_WORKER_TASK_SOFT_TIMEOUT_SECONDS，以便软超时落库和清理临时目录"
            )
        return self

    @model_validator(mode="after")
    def _validate_jwt_secret_in_production(self) -> Self:
        # 生产环境空密钥会导致所有令牌可被伪造或无法签发，必须在启动期失败
        if self.env.lower() in {"prod", "production"} and not self.jwt_secret.strip():
            raise ValueError("生产环境必须配置 APP_JWT_SECRET，禁止使用空密钥")
        return self


@lru_cache
def get_settings() -> Settings:
    """返回进程级单例配置；环境变量不合法时此处抛出带字段名的异常。"""
    return Settings()


def minio_public_endpoint(settings: Settings) -> str:
    """预签名 URL 使用的外部端点；未配置时回退内部端点（仅内网客户端可直传）。"""
    return settings.minio_public_endpoint or settings.minio_endpoint
