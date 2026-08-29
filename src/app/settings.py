"""应用配置。

所有配置来自环境变量（`APP_` 前缀）；机密一律经环境注入，不写入代码。
配置缺失或不合法时在进程启动期快速失败，并给出可定位的中文诊断。
"""

from functools import lru_cache

from pydantic import field_validator
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

    # Geo Worker 临时目录（每任务独立子目录，开始前检查可用空间）
    worker_tmp_dir: str = "/data/tmp"
    # 临时空间预检：低于源文件预计占用的该倍数时任务早期失败
    worker_tmp_min_ratio: float = 2.0

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


@lru_cache
def get_settings() -> Settings:
    """返回进程级单例配置；环境变量不合法时此处抛出带字段名的异常。"""
    return Settings()


def minio_public_endpoint(settings: Settings) -> str:
    """预签名 URL 使用的外部端点；未配置时回退内部端点（仅内网客户端可直传）。"""
    return settings.minio_public_endpoint or settings.minio_endpoint
